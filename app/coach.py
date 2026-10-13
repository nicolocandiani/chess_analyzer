"""The coach: turns engine output into human explanations, hints and lessons.

Explanations are built from concrete tactical facts about the position (what a move attacks,
forks, pins, defends or leaves hanging, and how much material the engine line wins), so the
coach can say *why* a move is good or bad instead of only quoting an evaluation.
"""
from __future__ import annotations

import queue
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import chess
import chess.engine

from .analysis import (PIECE_NAMES, PIECE_VALUES, describe_position, format_eval, hanging_pieces,
                       material_balance, pv_to_san, score_to_dict, stockfish_path, terminal_eval)

GOOD = {"brilliant", "great", "best", "excellent", "good", "book", "forced"}
CORRECT = {"brilliant", "great", "best", "excellent", "book", "forced"}
BAD = {"inaccuracy", "mistake", "miss", "blunder"}


# ---------------------------------------------------------------- wording helpers

def material_words(n: int) -> str:
    return {1: "a pawn", 2: "two pawns' worth of material", 3: "a piece", 4: "a piece and a pawn",
            5: "a rook", 6: "a rook and a pawn", 7: "nearly a queen's worth of material",
            8: "nearly a queen's worth of material"}.get(n, "the queen" if n >= 9 else f"{n} points of material")


def side_name(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"


def piece_at(board: chess.Board, sq: int) -> str:
    p = board.piece_at(sq)
    return f"{PIECE_NAMES[p.piece_type]} on {chess.square_name(sq)}" if p else chess.square_name(sq)


def join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------- tactical facts

def line_material(board: chess.Board, pv: list[chess.Move], plies: int = 8) -> int:
    """Material gained by the side to move along `pv`, measured at the last quiet point
    (so a line cut off mid-exchange doesn't count a half-finished trade)."""
    return line_material_at(board, pv, plies)[0]


def line_material_at(board: chess.Board, pv: list[chess.Move], plies: int = 8) -> tuple[int, int]:
    """(material gained, number of plies of `pv` that the count covers)."""
    color = board.turn
    start = material_balance(board, color)
    b = board.copy(stack=False)
    values, moves = [], []
    for m in pv[:plies]:
        if not b.is_legal(m):
            break
        moves.append(m)
        b.push(m)
        values.append(material_balance(b, color) - start)
    if not values:
        return 0, 0
    # Index k is quiet if the next move in the line is not a capture.
    b = board.copy(stack=False)
    quiet = []
    for k, m in enumerate(moves):
        b.push(m)
        nxt = moves[k + 1] if k + 1 < len(moves) else None
        quiet.append(nxt is None or not b.is_capture(nxt))
    for k in range(len(values) - 1, -1, -1):
        if quiet[k]:
            return values[k], k + 1
    return values[-1], len(values)


def threatened_pieces(board: chess.Board, color: chess.Color) -> list[tuple[int, int]]:
    """Pieces of `color` that the opponent could win if it were their move."""
    b = board.copy(stack=False)
    b.turn = not color
    b.ep_square = None
    return hanging_pieces(b, color, min_value=1)


def mate_threats(board: chess.Board, color: chess.Color) -> list[str]:
    """Mate-in-one moves `color` would have if it were their turn (SAN)."""
    b = board.copy(stack=False)
    if b.turn != color:
        if b.is_check():
            return []
        b.turn = color
        b.ep_square = None
    out = []
    for m in b.legal_moves:
        b.push(m)
        mate = b.is_checkmate()
        b.pop()
        if mate:
            out.append(b.san(m))
    return out


def _last_capture_value(board: chess.Board) -> int:
    """Value of the piece taken by the last move played on `board` (0 if it wasn't a capture)."""
    b = board.copy()
    last = b.pop()
    if not b.is_capture(last):
        return 0
    victim = b.piece_at(last.to_square)
    return PIECE_VALUES[victim.piece_type] if victim else 1


def move_themes(before: chess.Board, move: chess.Move) -> list[dict]:
    """Concrete, explainable things a move does."""
    mover = before.turn
    after = before.copy(stack=False)
    after.push(move)
    piece = before.piece_at(move.from_square)
    moved_value = PIECE_VALUES[piece.piece_type] if piece else 0
    themes: list[dict] = []

    if after.is_checkmate():
        return [{"type": "mate"}]
    if before.is_castling(move):
        themes.append({"type": "castle"})
    if move.promotion:
        themes.append({"type": "promote", "piece": PIECE_NAMES[move.promotion]})

    # Captures: free material vs. trades.
    if before.is_capture(move):
        victim_sq = move.to_square if not before.is_en_passant(move) else chess.square(
            chess.square_file(move.to_square), chess.square_rank(move.from_square))
        victim = before.piece_at(victim_sq)
        loose = {sq for sq, _ in hanging_pieces(before, not mover, min_value=1)}
        name = PIECE_NAMES[victim.piece_type] if victim else "pawn"
        prev = before.peek() if before.move_stack else None
        victim_value = PIECE_VALUES[victim.piece_type] if victim else 1
        taken = _last_capture_value(before) if prev and prev.to_square == move.to_square else 0
        # Taking back is only a "recapture" if we win back no more than was just lost.
        if taken and victim_value <= taken + 1:
            themes.append({"type": "recapture", "piece": name})
        elif victim_sq in loose:
            themes.append({"type": "win_material", "piece": name, "square": chess.square_name(victim_sq)})
        elif victim and PIECE_VALUES[victim.piece_type] == moved_value:
            themes.append({"type": "trade", "piece": name})
        else:
            themes.append({"type": "capture", "piece": name})

    # What the moved piece now attacks: forks and threats.
    targets = []
    for sq in after.attacks(move.to_square):
        t = after.piece_at(sq)
        if not t or t.color == mover:
            continue
        if t.piece_type == chess.KING:
            targets.append((sq, 100))
            continue
        tv = PIECE_VALUES[t.piece_type]
        defended = bool(after.attackers(not mover, sq))
        if tv >= 3 and (tv > moved_value or not defended):
            targets.append((sq, tv))
    if after.is_check():
        themes.append({"type": "check"})
    if len(targets) >= 2:
        themes.append({"type": "fork", "targets": [piece_at(after, sq) for sq, _ in sorted(targets, key=lambda x: -x[1])]})
    elif len(targets) == 1 and targets[0][1] < 100:
        themes.append({"type": "attack", "target": piece_at(after, targets[0][0])})

    # Discovered attacks: another of our pieces now hits something valuable.
    for sq in after.pieces(chess.QUEEN, mover) | after.pieces(chess.ROOK, mover) | after.pieces(chess.BISHOP, mover):
        if sq == move.to_square:
            continue
        new = after.attacks(sq) & ~before.attacks(sq)
        for t_sq in new:
            t = after.piece_at(t_sq)
            if t and t.color != mover and (t.piece_type == chess.KING or PIECE_VALUES[t.piece_type] >= 5):
                themes.append({"type": "discovered", "by": piece_at(after, sq), "target": piece_at(after, t_sq)})
                break

    # Pins created against the enemy king.
    for sq in chess.SQUARES:
        t = after.piece_at(sq)
        if t and t.color != mover and t.piece_type != chess.KING and after.is_pinned(not mover, sq) \
                and not before.is_pinned(not mover, sq) and PIECE_VALUES[t.piece_type] >= 3:
            themes.append({"type": "pin", "target": piece_at(after, sq)})
            break

    # Rescuing pieces that were under attack.
    threatened_before = {sq for sq, _ in threatened_pieces(before, mover)}
    threatened_after = {sq for sq, _ in threatened_pieces(after, mover)}
    if move.from_square in threatened_before and move.to_square not in threatened_after:
        themes.append({"type": "escape", "piece": PIECE_NAMES[piece.piece_type]})
    saved = [sq for sq in threatened_before if sq != move.from_square and sq not in threatened_after
             and after.piece_at(sq) and after.piece_at(sq).color == mover]
    if saved and not before.is_capture(move):
        themes.append({"type": "defend", "target": piece_at(after, saved[0])})

    # Parrying a mate threat.
    threats = mate_threats(before, not mover)
    if threats and not mate_threats(after, not mover):
        themes.insert(0, {"type": "stop_mate", "threat": threats[0]})

    # Opening principles.
    if before.fullmove_number <= 12 and piece:
        home_rank = 0 if mover == chess.WHITE else 7
        if piece.piece_type in (chess.KNIGHT, chess.BISHOP) and chess.square_rank(move.from_square) == home_rank:
            themes.append({"type": "develop", "piece": PIECE_NAMES[piece.piece_type]})
        if piece.piece_type == chess.PAWN and chess.square_name(move.to_square) in ("e4", "d4", "e5", "d5", "c4", "c5"):
            themes.append({"type": "center"})
        if piece.piece_type == chess.QUEEN and before.fullmove_number <= 6:
            themes.append({"type": "early_queen"})
    return themes


def positional_ideas(before: chess.Board, move: chess.Move, reply_pv: list[chess.Move]) -> list[str]:
    """Quiet-move explanations: activity, files, space, king activity and the follow-up plan."""
    mover = before.turn
    piece = before.piece_at(move.from_square)
    after = before.copy(stack=False)
    after.push(move)
    ideas = []
    if piece is None:
        return ideas
    name = PIECE_NAMES[piece.piece_type]
    pt = piece.piece_type
    if pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        m0, m1 = len(before.attacks(move.from_square)), len(after.attacks(move.to_square))
        if m1 - m0 >= 3:
            ideas.append(f"It activates your {name}, which now controls {m1} squares instead of {m0}.")
    if pt in (chess.ROOK, chess.QUEEN) and chess.square_file(move.to_square) != chess.square_file(move.from_square):
        f = chess.square_file(move.to_square)
        pawns = [sq for sq in after.pieces(chess.PAWN, chess.WHITE) | after.pieces(chess.PAWN, chess.BLACK)
                 if chess.square_file(sq) == f]
        own = [sq for sq in pawns if after.piece_at(sq).color == mover]
        if not pawns:
            ideas.append(f"It takes the open {chess.FILE_NAMES[f]}-file.")
        elif not own and pt == chess.ROOK:
            ideas.append(f"It puts pressure down the half-open {chess.FILE_NAMES[f]}-file.")
    if pt == chess.PAWN:
        rank = chess.square_rank(move.to_square) if mover == chess.WHITE else 7 - chess.square_rank(move.to_square)
        if rank >= 4 and not before.is_capture(move):
            ideas.append("It gains space and restricts your opponent's pieces.")
    non_pawn = sum(PIECE_VALUES[p.piece_type] for p in before.piece_map().values()
                   if p.piece_type not in (chess.PAWN, chess.KING))
    if pt == chess.KING and non_pawn <= 14 and not before.is_castling(move):
        center = lambda sq: max(abs(chess.square_file(sq) - 3.5), abs(chess.square_rank(sq) - 3.5))
        if center(move.to_square) < center(move.from_square):
            ideas.append("It activates the king, a strong piece in the endgame.")
    if len(reply_pv) >= 2:
        b = after.copy(stack=False)
        if b.is_legal(reply_pv[0]):
            reply_san = b.san(reply_pv[0])
            b.push(reply_pv[0])
            if b.is_legal(reply_pv[1]):
                ideas.append(f"The plan: after {reply_san}, you continue with {b.san(reply_pv[1])}.")
    return ideas[:2]


def theme_phrases(themes: list[dict], whose: str) -> list[str]:
    """Verb phrases ('forks the king on e8 and the rook on a8') for a list of themes.
    `whose` is the owner of the target pieces from the reader's perspective: 'your' or 'their'/'the'."""
    out = []
    for t in themes:
        k = t["type"]
        if k == "mate":
            out.append("delivers checkmate")
        elif k == "stop_mate":
            out.append(f"stops the mate threat {t['threat']}")
        elif k == "fork":
            out.append(f"forks {whose} " + join_words(t["targets"]))
        elif k == "win_material":
            out.append(f"wins {whose} undefended {t['piece']} on {t['square']}")
        elif k == "discovered":
            out.append(f"uncovers an attack by the {t['by']} on {whose} {t['target']}")
        elif k == "pin":
            out.append(f"pins {whose} {t['target']} to the king")
        elif k == "attack":
            out.append(f"attacks {whose} {t['target']}")
        elif k == "check" and not any(x["type"] in ("fork", "mate") for x in themes):
            out.append("gives check")
        elif k == "promote":
            out.append(f"promotes to a {t['piece']}")
        elif k == "escape":
            out.append(f"moves the attacked {t['piece']} to safety")
        elif k == "defend":
            out.append(f"protects the {t['target']}")
        elif k == "castle":
            out.append("castles, tucking the king away and connecting the rooks")
        elif k == "develop":
            out.append(f"develops the {t['piece']}")
        elif k == "center":
            out.append("fights for the centre")
        elif k == "trade":
            out.append(f"trades {t['piece']}s")
        elif k == "recapture":
            out.append(f"recaptures the {t['piece']}")
    return out[:3]


def hint_for(before: chess.Board, best: chess.Move, best_eval: dict | None = None) -> dict:
    """Progressive hints for finding `best`: which piece, then what idea."""
    sign = 1 if before.turn == chess.WHITE else -1
    piece = before.piece_at(best.from_square)
    themes = {t["type"]: t for t in move_themes(before, best)}
    if "mate" in themes or (best_eval and best_eval.get("mate") is not None and best_eval["mate"] * sign > 0):
        idea = f"There's a forced checkmate here{' in ' + str(abs(best_eval['mate'])) if best_eval and best_eval.get('mate') else ''}. Look at every check."
    elif "fork" in themes:
        idea = "Look for a move that attacks two things at once."
    elif "win_material" in themes:
        idea = "One of your opponent's pieces is not defended well enough. Can you win it?"
    elif "discovered" in themes:
        idea = "Moving one piece can unleash another. Look for a discovered attack."
    elif "pin" in themes:
        idea = "Look for a way to pin an enemy piece to the king."
    elif "stop_mate" in themes:
        idea = f"Your opponent threatens checkmate ({themes['stop_mate']['threat']}). Stop it!"
    elif "escape" in themes or "defend" in themes:
        idea = "One of your pieces is in danger. Deal with the threat first."
    elif "check" in themes:
        idea = "A forcing move works best here. Consider your checks."
    elif "attack" in themes:
        idea = "Go on the offensive: one of their pieces can be attacked with gain of time."
    elif "castle" in themes:
        idea = "Think about the safety of your king."
    elif "develop" in themes or "center" in themes:
        idea = "Follow opening principles: develop your pieces and control the centre."
    elif before.is_capture(best):
        idea = "Look at the captures available to you."
    else:
        idea = "No tactic here. Find the move that improves your position the most."
    return {
        "piece": f"Think about your {PIECE_NAMES[piece.piece_type]} on {chess.square_name(best.from_square)}." if piece else None,
        "square": chess.square_name(best.from_square),
        "idea": idea,
    }


# ---------------------------------------------------------------- explanations

HEADLINES = {
    "brilliant": "{san} is brilliant!",
    "great": "{san} is a great find!",
    "best": "{san} is the best move.",
    "excellent": "{san} is excellent.",
    "good": "{san} is a good move.",
    "book": "{san} is a book move.",
    "forced": "{san} was the only legal move.",
    "inaccuracy": "{san} is an inaccuracy.",
    "mistake": "{san} is a mistake.",
    "miss": "{san} misses a chance.",
    "blunder": "Ouch, {san} is a blunder.",
}

TIPS = {
    "hanging": "Blunder check: before you move, ask which of your pieces will be attacked or undefended afterwards.",
    "overlooked": "After every opponent move, ask: what does that move threaten?",
    "fork": "Watch for squares where one enemy piece could attack two of yours, especially knight forks and queen checks.",
    "mate_allowed": "Check your king's safety: look at every check your opponent could give after your move.",
    "missed_mate": "Always look at checks first, then captures, then threats. Forced mates hide in checks.",
    "missed_tactic": "Scan for forcing moves (checks, captures, threats) before playing a quiet move.",
    "miss": "When your opponent errs, punish it: ask what their last move left undefended.",
    "positional": "Compare your candidate moves by their best reply, not just by what they do for you.",
    "early_queen": "Bringing the queen out early lets your opponent develop with tempo by attacking it.",
}


def explain_move(before: chess.Board, move: chess.Move, cls: str, best_line: dict, after_eval: dict,
                 reply_pv: list[chess.Move], opening: str | None = None, second_san: str | None = None,
                 second_eval_text: str | None = None, sac: dict | None = None) -> dict:
    """Coach notes for one move: {'explanation': [sentences], 'tip': str|None}."""
    mover = before.turn
    sign = 1 if mover == chess.WHITE else -1
    opp = side_name(not mover)
    san = before.san(move)
    after = before.copy(stack=False)
    after.push(move)
    notes = [HEADLINES[cls].format(san=san)]
    tip = None
    eval_said = False

    if cls == "book":
        if opening:
            notes.append(f"It's opening theory: {opening}.")
        phrases = theme_phrases(move_themes(before, move), "their")
        if phrases:
            notes.append(f"It {join_words(phrases)}.")
        return {"explanation": notes, "tip": None}
    if after.is_checkmate():
        notes.append("Checkmate. Game over!")
        return {"explanation": notes, "tip": None}

    best_eval = best_line["eval"]
    best_pv = best_line["pv"]
    best = best_pv[0] if best_pv else None
    best_san = before.san(best) if best else None
    themes = move_themes(before, move)
    phrases = theme_phrases(themes, "their")
    gained, gained_plies = line_material_at(before, [move] + list(reply_pv))
    mate_for_me = after_eval["mate"] is not None and after_eval["mate"] * sign > 0
    mate_against = after_eval["mate"] is not None and after_eval["mate"] * sign < 0
    had_mate = best_eval["mate"] is not None and best_eval["mate"] * sign > 0

    if cls in GOOD:
        if cls == "brilliant" and sac:
            what = f"the {sac['piece']} on {sac['square']}" if sac.get("piece") else "material"
            notes.append(f"It sacrifices {what}: taking it would cost {opp} more than it gains.")
        if phrases:
            notes.append(f"It {join_words(phrases)}.")
        if mate_for_me:
            notes.append(f"You now have a forced mate in {abs(after_eval['mate'])}.")
        elif gained >= 1 and cls != "brilliant" and not any(t["type"] == "recapture" for t in themes):
            line = " ".join(pv_to_san(before, [move] + list(reply_pv), gained_plies))
            notes.append(f"The point: after {line}, you come out {material_words(gained)} up.")
        elif not phrases and cls in ("great", "best", "excellent", "good"):
            notes.extend(positional_ideas(before, move, reply_pv))
        if cls == "great" and second_san:
            notes.append(f"It's the only move that keeps your advantage. The next best, {second_san} "
                         f"({second_eval_text}), is much weaker.")
        if cls in ("excellent", "good") and best_san and best != move:
            best_phr = theme_phrases(move_themes(before, best), "their")
            why = f", which {join_words(best_phr)}" if best_phr else ""
            notes.append(f"Slightly stronger was {best_san}{why} ({format_eval(best_eval)}).")
        if any(t["type"] == "early_queen" for t in themes):
            tip = TIPS["early_queen"]
    else:
        # What went wrong
        threatened_before = {sq: v for sq, v in threatened_pieces(before, mover)}
        reply = reply_pv[0] if reply_pv else None
        # Net material the mover ends up losing, counted from before their own move so that an
        # ordinary trade (we take, they take back) isn't reported as losing a piece.
        lost, lost_plies = line_material_at(before, [move] + list(reply_pv)) if reply_pv else (0, 0)
        lost = -lost
        if mate_against and not (best_eval["mate"] is not None and best_eval["mate"] * sign < 0):
            reply_san = after.san(reply) if reply else ""
            notes.append(f"It allows a forced mate in {abs(after_eval['mate'])}, starting with {reply_san}.")
            tip = TIPS["mate_allowed"]
        elif reply:
            reply_san = after.san(reply)
            reply_themes = move_themes(after, reply)
            reply_phr = theme_phrases(reply_themes, "your")
            overlooked = reply.to_square in threatened_before and reply.to_square != move.to_square \
                and after.piece_at(reply.to_square) is not None
            if overlooked:
                notes.append(f"Your {piece_at(after, reply.to_square)} was already under attack and is still "
                             f"unprotected. {opp} can take it with {reply_san}.")
                tip = TIPS["overlooked"]
            elif reply_phr:
                notes.append(f"{opp} answers {reply_san}, which {join_words(reply_phr)}.")
                if any(t["type"] == "fork" for t in reply_themes):
                    tip = TIPS["fork"]
                elif any(t["type"] == "win_material" for t in reply_themes):
                    tip = TIPS["hanging"]
            else:
                notes.append(f"{opp}'s best reply is {reply_san}.")
            if lost >= 1:
                line = " ".join(pv_to_san(after, reply_pv, max(1, lost_plies - 1)))
                notes.append(f"After {line}, {opp} wins {material_words(lost)}.")
                tip = tip or TIPS["hanging"]
            elif not overlooked:
                d = describe_position(after_eval["cp"])
                d = d if d.startswith(("White", "Black")) else d[0].lower() + d[1:]
                notes.append(f"No material is lost, but now {d} ({format_eval(after_eval)}) "
                             f"instead of {format_eval(best_eval)}.")
                eval_said = True

        # What was better
        if best_san:
            best_themes = move_themes(before, best)
            best_phr = theme_phrases(best_themes, "their")
            best_gain, best_plies = line_material_at(before, best_pv)
            if had_mate:
                notes.append(f"You had a forced mate in {abs(best_eval['mate'])} starting with {best_san}: "
                             f"{' '.join(pv_to_san(before, best_pv, 8))}.")
                tip = TIPS["missed_mate"]
            else:
                parts = []
                if best_phr:
                    parts.append(join_words(best_phr))
                if best_gain >= 1:
                    parts.append(f"wins {material_words(best_gain)} after {' '.join(pv_to_san(before, best_pv, best_plies))}")
                why = f", which {' and '.join(parts)}" if parts else ""
                notes.append(f"Better was {best_san}{why} ({format_eval(best_eval)}).")
                if best_gain >= 2 or any(t["type"] in ("fork", "pin", "discovered") for t in best_themes):
                    tip = tip or TIPS["missed_tactic"]
        if cls == "miss":
            notes.append(f"{opp}'s previous move was an error, and this was the moment to punish it.")
            tip = tip or TIPS["miss"]
        tip = tip or TIPS["positional"]

    if not mate_for_me and not mate_against and not eval_said:
        notes.append(f"{describe_position(after_eval['cp'])} ({format_eval(after_eval)}).")
    return {"explanation": notes, "tip": tip}


# ---------------------------------------------------------------- live coach engine

class CoachEngine:
    """A small pool of long-lived Stockfish processes for interactive coaching.

    - Results are cached per position; a cached multi-line result also answers single-line requests.
    - Concurrent requests for the same position share one search instead of running it twice.
    - Background (prefetch) searches only use an idle engine, so they never delay the user.
    """

    def __init__(self, depth: int, threads: int, hash_mb: int, engines: int = 2, time_limit: float = 1.5,
                 cache_size: int = 5000):
        self.depth, self.threads, self.hash_mb = depth, threads, hash_mb
        self.time_limit = time_limit
        self.size = max(1, engines)
        self.idle: queue.Queue[chess.engine.SimpleEngine] = queue.Queue()
        self.started = 0
        self.lock = threading.Lock()  # guards cache, inflight and started
        self.cache: OrderedDict[tuple, list[dict]] = OrderedDict()
        self.inflight: dict[tuple, threading.Event] = {}
        self.cache_size = cache_size
        self.prefetcher = ThreadPoolExecutor(max_workers=self.size)

    # -- engine pool
    def _acquire(self, block: bool) -> chess.engine.SimpleEngine | None:
        try:
            return self.idle.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            spawn = self.started < self.size
            if spawn:
                self.started += 1
        if spawn:
            try:
                eng = chess.engine.SimpleEngine.popen_uci(stockfish_path())
                eng.configure({"Threads": self.threads, "Hash": self.hash_mb})
                return eng
            except Exception:
                with self.lock:
                    self.started -= 1
                raise
        return self.idle.get() if block else None

    def _release(self, eng: chess.engine.SimpleEngine | None):
        if eng is not None:
            self.idle.put(eng)
        else:
            with self.lock:
                self.started -= 1

    # -- cache
    def _cached(self, fen: str, multipv: int) -> list[dict] | None:
        for m in range(multipv, 4):
            hit = self.cache.get((fen, m))
            if hit is not None:
                self.cache.move_to_end((fen, m))
                return hit[:multipv]
        return None

    def analyse(self, board: chess.Board, multipv: int = 2, background: bool = False) -> list[dict] | None:
        """Engine lines for `board`. In background mode returns None instead of waiting for an engine."""
        if board.is_game_over(claim_draw=False):
            return [{"eval": terminal_eval(board), "pv": []}]
        multipv = max(1, min(multipv, board.legal_moves.count()))
        fen = board.fen()
        key = (fen, multipv)
        while True:
            with self.lock:
                hit = self._cached(fen, multipv)
                if hit is not None:
                    return hit
                pending = self.inflight.get(key) or (self.inflight.get((fen, 2)) if multipv == 1 else None)
                if pending is None:
                    pending = self.inflight[key] = threading.Event()
                    break
            if background:
                return None
            pending.wait(timeout=30)  # someone else is searching this position: reuse their result

        try:
            eng = self._acquire(block=not background)
            if eng is None:
                return None
            try:
                infos = eng.analyse(board, chess.engine.Limit(depth=self.depth, time=self.time_limit), multipv=multipv)
            except (chess.engine.EngineError, chess.engine.EngineTerminatedError):
                try:
                    eng.quit()
                except Exception:
                    pass
                eng = None
                raise
            finally:
                self._release(eng)
            out = [{"eval": score_to_dict(i["score"]), "pv": i["pv"]} for i in infos if "score" in i and i.get("pv")]
            out = out or [{"eval": {"cp": 0, "mate": None}, "pv": []}]
            with self.lock:
                self.cache[key] = out
                while len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
            return out
        finally:
            with self.lock:
                self.inflight.pop(key, None)
            pending.set()

    def warm_up(self) -> None:
        """Start the engine processes ahead of the first request."""
        def run():
            engines = []
            try:
                for _ in range(self.size):
                    eng = self._acquire(block=False)
                    if eng is None:
                        break
                    eng.ping()
                    engines.append(eng)
            except Exception:
                pass
            for eng in engines:
                self._release(eng)
        self.prefetcher.submit(run)

    def prefetch(self, jobs: list[tuple[chess.Board, list[chess.Move]]]) -> None:
        """Warm the cache for positions the user is likely to ask about next (runs in the background)."""
        def background(board: chess.Board, multipv: int) -> None:
            # Yield to the user: only search on an idle engine, retrying for a few seconds.
            for _ in range(40):
                if self.analyse(board, multipv, background=True) is not None:
                    return
                threading.Event().wait(0.25)

        def run(board: chess.Board, moves: list[chess.Move]) -> None:
            try:
                for mv in moves:
                    after = board.copy(stack=False)
                    after.push(mv)
                    background(after, 1)
                background(board, 2)
            except Exception:
                pass

        for board, moves in jobs:
            self.prefetcher.submit(run, board, moves)
