"""Stockfish-powered game review: evaluations, move classification, accuracy and explanations.

Classification follows the chess.com "expected points" model: every evaluation is turned into
a win probability for the side to move, and a move is judged by how much win probability it
throws away compared to the engine's best move.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
from pathlib import Path
from typing import Callable

import chess
import chess.engine
import chess.pgn

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook",
               chess.QUEEN: "queen", chess.KING: "king"}
MATE_CP = 10000

CLASSIFICATIONS = ["brilliant", "great", "best", "excellent", "good", "book", "forced",
                   "inaccuracy", "mistake", "miss", "blunder"]

_OPENINGS: dict | None = None


def openings() -> dict:
    global _OPENINGS
    if _OPENINGS is None:
        path = Path(__file__).with_name("openings.json")
        try:
            _OPENINGS = json.loads(path.read_text())
        except (OSError, ValueError):
            _OPENINGS = {}
    return _OPENINGS


def stockfish_path() -> str:
    env = os.environ.get("STOCKFISH_PATH")
    if env and Path(env).exists():
        return env
    found = shutil.which("stockfish") or shutil.which("stockfish", path="/usr/games:/usr/local/bin:/opt/homebrew/bin")
    if not found:
        raise RuntimeError("Stockfish binary not found; set STOCKFISH_PATH")
    return found


# ---------------------------------------------------------------- evaluation helpers

def score_to_dict(score: chess.engine.PovScore) -> dict:
    w = score.white()
    mate = w.mate()
    return {"cp": w.score(mate_score=MATE_CP), "mate": mate}


def terminal_eval(board: chess.Board) -> dict:
    if board.is_checkmate():
        # Side to move is mated.
        return {"cp": -MATE_CP if board.turn == chess.WHITE else MATE_CP, "mate": 0}
    return {"cp": 0, "mate": None}


def win_pct(cp: float) -> float:
    """Win probability (0-100) for a centipawn eval from the side's own POV (lichess model)."""
    cp = max(-MATE_CP, min(MATE_CP, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(win_before: float, win_after: float) -> float:
    if win_after >= win_before:
        return 100.0
    acc = 103.1668 * math.exp(-0.04354 * (win_before - win_after)) - 3.1669
    return max(0.0, min(100.0, acc))


def game_accuracy(accs: list[float]) -> float | None:
    if not accs:
        return None
    mean = sum(accs) / len(accs)
    harmonic = len(accs) / sum(1 / max(a, 1.0) for a in accs)
    return round((mean + harmonic) / 2, 1)


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(PIECE_VALUES[p.piece_type] for p in board.piece_map().values() if p.color == color)


def material_balance(board: chess.Board, color: chess.Color) -> int:
    return material(board, color) - material(board, not color)


def game_phase(board: chess.Board, ply: int, in_book: bool) -> str:
    non_pawn = sum(PIECE_VALUES[p.piece_type] for p in board.piece_map().values()
                   if p.piece_type not in (chess.PAWN, chess.KING))
    if non_pawn <= 26:
        return "endgame"
    if in_book or ply < 20:
        return "opening"
    return "middlegame"


def hanging_pieces(board: chess.Board, color: chess.Color, min_value: int = 3) -> list[tuple[int, int]]:
    """Pieces of `color` that the opponent (to move) can win: undefended, or attacked by something cheaper."""
    out = []
    for sq, piece in board.piece_map().items():
        if piece.color != color or PIECE_VALUES[piece.piece_type] < min_value:
            continue
        value = PIECE_VALUES[piece.piece_type]
        attackers = [a for a in board.attackers(not color, sq)
                     if board.turn != color and _legal_capture(board, a, sq)]
        if not attackers:
            continue
        cheapest = min(PIECE_VALUES[board.piece_type_at(a)] or 100 for a in attackers)
        defended = bool(board.attackers(color, sq))
        if not defended or cheapest < value:
            out.append((sq, value))
    return out


def _legal_capture(board: chess.Board, frm: int, to: int) -> bool:
    piece = board.piece_at(frm)
    promo = chess.QUEEN if piece and piece.piece_type == chess.PAWN and chess.square_rank(to) in (0, 7) else None
    return board.is_legal(chess.Move(frm, to, promotion=promo))


def pv_to_san(board: chess.Board, pv: list[chess.Move], limit: int = 10) -> list[str]:
    b = board.copy(stack=False)
    out = []
    for m in pv[:limit]:
        if not b.is_legal(m):
            break
        out.append(b.san(m))
        b.push(m)
    return out


def format_eval(e: dict) -> str:
    if e["mate"] is not None:
        if e["mate"] == 0:
            return "1-0" if e["cp"] > 0 else "0-1"
        return f"M{abs(e['mate'])}" if e["mate"] > 0 else f"-M{abs(e['mate'])}"
    return f"{e['cp'] / 100:+.2f}"


def describe_position(cp_white: float) -> str:
    a = abs(cp_white)
    side = "White" if cp_white > 0 else "Black"
    if a >= MATE_CP - 500:
        return f"{side} has a forced mate"
    if a < 40:
        return "The position is equal"
    if a < 120:
        return f"{side} is slightly better"
    if a < 300:
        return f"{side} is clearly better"
    return f"{side} is winning"


# ---------------------------------------------------------------- the analysis

class Engine:
    def __init__(self, threads: int, hash_mb: int):
        self.engine = chess.engine.SimpleEngine.popen_uci(stockfish_path())
        self.engine.configure({"Threads": threads, "Hash": hash_mb})

    def analyse(self, board: chess.Board, depth: int, multipv: int) -> list[dict]:
        infos = self.engine.analyse(board, chess.engine.Limit(depth=depth, time=8), multipv=multipv)
        out = []
        for info in infos:
            if "score" not in info or not info.get("pv"):
                continue
            out.append({"eval": score_to_dict(info["score"]), "pv": info["pv"], "depth": info.get("depth")})
        return out

    def close(self):
        try:
            self.engine.quit()
        except Exception:
            pass


def analyse_pgn(pgn_text: str, depth: int, threads: int, hash_mb: int,
                progress: Callable[[int, int], None] | None = None) -> dict:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None or game.errors:
        raise ValueError("Could not parse PGN" + (f": {game.errors[0]}" if game and game.errors else ""))

    board = game.board()
    nodes = list(game.mainline())
    positions = [board.copy()]
    for node in nodes:
        board.push(node.move)
        positions.append(board.copy())

    total = len(positions)
    engine = Engine(threads, hash_mb)
    lines: list[list[dict]] = []
    try:
        for i, pos in enumerate(positions):
            if pos.is_game_over(claim_draw=False):
                lines.append([{"eval": terminal_eval(pos), "pv": [], "depth": 0}])
            else:
                multipv = 1 if i == total - 1 else min(2, pos.legal_moves.count())
                res = engine.analyse(pos, depth, multipv)
                lines.append(res or [{"eval": {"cp": 0, "mate": None}, "pv": [], "depth": 0}])
            if progress:
                progress(i + 1, total)
    finally:
        engine.close()

    return build_review(game, positions, nodes, lines, depth)


def classify(before: chess.Board, move: chess.Move, best_line: dict, second_line: dict | None,
             after_eval: dict, reply_pv: list[chess.Move], prev_class: str | None = None,
             book_entry: dict | None = None, obvious_recapture: bool = False) -> dict:
    """Grade a move against the engine's view of the position before it."""
    mover = before.turn
    sign = 1 if mover == chess.WHITE else -1
    best_move = best_line["pv"][0] if best_line["pv"] else None
    win_before = win_pct(best_line["eval"]["cp"] * sign)
    win_after = win_pct(after_eval["cp"] * sign)
    is_best = best_move == move
    # If the played move *is* the engine move, any eval difference is just search noise.
    loss = 0.0 if is_best else max(0.0, win_before - win_after)
    sac = sacrifice_info(before, move, reply_pv)
    second_win = win_pct(second_line["eval"]["cp"] * sign) if second_line else None

    if before.legal_moves.count() == 1:
        cls = "forced"
    elif book_entry:
        cls = "book"
    elif sac and loss <= 2 and win_after >= 50 and win_before < 97 and not obvious_recapture:
        cls = "brilliant"
    elif (is_best and second_win is not None and win_before - second_win >= 15
          and win_after >= 35 and win_before < 95 and not obvious_recapture
          and not (prev_class in ("blunder", "mistake") and before.is_capture(move))):
        cls = "great"
    elif is_best:
        cls = "best"
    elif loss < 2:
        cls = "excellent"
    elif loss < 5:
        cls = "good"
    elif loss < 10:
        cls = "inaccuracy"
    elif prev_class in ("mistake", "blunder", "inaccuracy") and win_before >= 60 and loss >= 10:
        cls = "miss"
    elif loss < 20:
        cls = "mistake"
    else:
        cls = "blunder"

    # Missing a forced mate while staying "fine" is still a missed opportunity.
    if best_line["eval"]["mate"] is not None and best_line["eval"]["mate"] * sign > 0 \
            and not (after_eval["mate"] is not None and after_eval["mate"] * sign >= 0) \
            and cls in ("excellent", "good", "inaccuracy") and win_after < 90:
        cls = "miss"
    return {"cls": cls, "loss": loss, "win_before": win_before, "win_after": win_after, "sac": sac}


def build_review(game: chess.pgn.Game, positions: list[chess.Board], nodes: list, lines: list[list[dict]],
                 depth: int) -> dict:
    from . import coach

    book = openings()
    headers = dict(game.headers)
    evals = [l[0]["eval"] for l in lines]

    moves_out = []
    in_book = True
    opening_name = None
    opening_eco = None
    prev_class = None

    for i, node in enumerate(nodes):
        before, after = positions[i], positions[i + 1]
        move = node.move
        mover = before.turn
        sign = 1 if mover == chess.WHITE else -1
        san = before.san(move)

        best_line = lines[i][0]
        best_move = best_line["pv"][0] if best_line["pv"] else None
        second_line = lines[i][1] if len(lines[i]) > 1 else None

        # Opening book
        book_entry = book.get(after.epd()) if in_book else None
        if book_entry:
            opening_name, opening_eco = book_entry["name"], book_entry["eco"]
        else:
            in_book = False

        obvious_recapture = bool(i > 0 and before.is_capture(move) and positions[i].move_stack
                                 and move.to_square == positions[i].peek().to_square)
        reply_pv = lines[i + 1][0]["pv"] if lines[i + 1] else []
        c = classify(before, move, best_line, second_line, evals[i + 1], reply_pv,
                     prev_class=prev_class, book_entry=book_entry, obvious_recapture=obvious_recapture)
        cls, loss, win_before, win_after, sac = c["cls"], c["loss"], c["win_before"], c["win_after"], c["sac"]
        accuracy = move_accuracy(win_before, win_before - loss)

        clock = node.clock()
        moves_out.append({
            "ply": i + 1,
            "move_number": before.fullmove_number,
            "color": "white" if mover == chess.WHITE else "black",
            "san": san,
            "uci": move.uci(),
            "from": chess.square_name(move.from_square),
            "to": chess.square_name(move.to_square),
            "fen_before": before.fen(),
            "fen": after.fen(),
            "eval": evals[i + 1],
            "eval_text": format_eval(evals[i + 1]),
            "eval_before": best_line["eval"],
            "win_before": round(win_before, 1),
            "win_after": round(win_after, 1),
            "win_loss": round(loss, 1),
            "accuracy": round(accuracy, 1),
            "classification": cls,
            "best_move": best_move.uci() if best_move else None,
            "best_san": before.san(best_move) if best_move else None,
            "best_line": pv_to_san(before, best_line["pv"]),
            "best_eval_text": format_eval(best_line["eval"]),
            "second_san": before.san(second_line["pv"][0]) if second_line else None,
            "second_eval_text": format_eval(second_line["eval"]) if second_line else None,
            "next_best": lines[i + 1][0]["pv"][0].uci() if lines[i + 1] and lines[i + 1][0]["pv"] else None,
            "next_line": pv_to_san(after, lines[i + 1][0]["pv"]) if lines[i + 1] else [],
            "clock": clock,
            "phase": game_phase(before, i, bool(book_entry)),
            "opening": opening_name,
            "check": after.is_check(),
            "capture": before.is_capture(move),
        })
        notes = coach.explain_move(before, move, cls, best_line, evals[i + 1], reply_pv, opening=opening_name,
                                   second_san=moves_out[-1]["second_san"],
                                   second_eval_text=moves_out[-1]["second_eval_text"], sac=sac)
        moves_out[-1].update(notes)
        moves_out[-1]["best_pv"] = [m.uci() for m in best_line["pv"][:10]]
        moves_out[-1]["hint"] = coach.hint_for(before, best_move, best_line["eval"]) if best_move else None
        prev_class = cls

    # Time spent per move from clocks.
    _time_spent(moves_out, headers.get("TimeControl"))

    summary = summarize(moves_out)
    return {
        "headers": headers,
        "white": headers.get("White"),
        "black": headers.get("Black"),
        "white_elo": headers.get("WhiteElo"),
        "black_elo": headers.get("BlackElo"),
        "result": headers.get("Result"),
        "termination": headers.get("Termination"),
        "opening": opening_name or headers.get("Opening") or _eco_name(headers.get("ECOUrl")),
        "eco": opening_eco or headers.get("ECO"),
        "start_fen": positions[0].fen(),
        "start_eval": evals[0],
        "start_eval_text": format_eval(evals[0]),
        "start_best": lines[0][0]["pv"][0].uci() if lines[0][0]["pv"] else None,
        "start_line": pv_to_san(positions[0], lines[0][0]["pv"]),
        "start_hint": coach.hint_for(positions[0], lines[0][0]["pv"][0], lines[0][0]["eval"]) if lines[0][0]["pv"] else None,
        "depth": depth,
        "moves": moves_out,
        "summary": summary,
    }


def _eco_name(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")


def _time_spent(moves: list[dict], tc: str | None) -> None:
    inc = 0.0
    base = None
    if tc and "/" not in tc and tc != "-":
        parts = tc.split("+")
        try:
            base = float(parts[0])
            inc = float(parts[1]) if len(parts) > 1 else 0.0
        except ValueError:
            base = None
    last = {"white": base, "black": base}
    for m in moves:
        prev = last[m["color"]]
        if m["clock"] is not None and prev is not None:
            m["time_spent"] = round(max(0.0, prev - m["clock"] + inc), 1)
        else:
            m["time_spent"] = None
        if m["clock"] is not None:
            last[m["color"]] = m["clock"]


def sacrifice_info(before: chess.Board, move: chess.Move, reply_pv: list[chess.Move]) -> dict | None:
    """Detect a material sacrifice: the move leaves material en prise (worth more than it captured),
    or the engine's best continuation leaves the mover down material."""
    mover = before.turn
    captured = before.piece_at(move.to_square)
    captured_value = PIECE_VALUES[captured.piece_type] if captured else (1 if before.is_en_passant(move) else 0)

    after = before.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        return None

    hanging_before = {sq for sq, _ in hanging_pieces(_flip_turn(before), mover)}
    newly = [(sq, v) for sq, v in hanging_pieces(after, mover)
             if sq not in hanging_before or sq == move.to_square]
    newly = [(sq, v) for sq, v in newly if v - captured_value >= 2 and not _regains(after, sq, v - captured_value)]

    # Material deficit along the engine line, measured after the mover has had the chance to
    # recapture (plies 2 and 4), so exchanges in progress don't look like sacrifices.
    bal_before = material_balance(before, mover)
    b = after.copy(stack=False)
    drops = []
    for k, m in enumerate(reply_pv[:4], start=1):
        if not b.is_legal(m):
            break
        b.push(m)
        if k % 2 == 0:
            drops.append(bal_before - material_balance(b, mover))
    pv_drop = min(drops) if len(drops) == 2 else 0

    if newly:
        sq, v = max(newly, key=lambda x: x[1])
        piece = after.piece_at(sq)
        return {"square": chess.square_name(sq), "piece": PIECE_NAMES[piece.piece_type], "value": v}
    if pv_drop >= 2:
        return {"square": None, "piece": None, "value": pv_drop}
    return None


def _regains(after: chess.Board, sq: int, net_loss: int) -> bool:
    """If the opponent takes on `sq` with their cheapest piece, can the mover immediately win back
    (nearly) as much material? Then it's a trade or a trap, not a sacrifice."""
    attackers = [a for a in after.attackers(after.turn, sq) if _legal_capture(after, a, sq)]
    if not attackers:
        return True
    att = min(attackers, key=lambda a: PIECE_VALUES[after.piece_type_at(a)])
    b = after.copy(stack=False)
    b.push(chess.Move(att, sq, promotion=chess.QUEEN if after.piece_type_at(att) == chess.PAWN
                      and chess.square_rank(sq) in (0, 7) else None))
    if b.is_checkmate():
        return False
    best_gain = 0
    for m in b.legal_moves:
        if not b.is_capture(m):
            continue
        victim = b.piece_at(m.to_square)
        victim_value = PIECE_VALUES[victim.piece_type] if victim else 1
        capturer_value = PIECE_VALUES[b.piece_type_at(m.from_square)]
        defended = bool(b.attackers(not b.turn, m.to_square))
        gain = victim_value if not defended or victim_value >= capturer_value else victim_value - capturer_value
        best_gain = max(best_gain, gain)
    return net_loss - best_gain < 2


def _flip_turn(board: chess.Board) -> chess.Board:
    b = board.copy(stack=False)
    b.turn = not b.turn
    b.ep_square = None
    return b


def summarize(moves: list[dict]) -> dict:
    out = {}
    for color in ("white", "black"):
        mine = [m for m in moves if m["color"] == color]
        counts = {c: 0 for c in CLASSIFICATIONS}
        for m in mine:
            counts[m["classification"]] += 1
        phases = {}
        for phase in ("opening", "middlegame", "endgame"):
            accs = [m["accuracy"] for m in mine if m["phase"] == phase]
            phases[phase] = game_accuracy(accs)
        accs = [m["accuracy"] for m in mine]
        out[color] = {
            "accuracy": game_accuracy(accs),
            "counts": counts,
            "phase_accuracy": phases,
            "avg_win_loss": round(sum(m["win_loss"] for m in mine) / len(mine), 2) if mine else None,
        }
    key = [m["ply"] for m in moves if m["classification"] in ("brilliant", "great", "miss", "blunder", "mistake")]
    out["key_moments"] = key
    return out
