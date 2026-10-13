"""Printable / shareable PDF version of a game review.

Boards and the eval graph are drawn as vector graphics with ReportLab; pieces use the chess
glyphs from DejaVu Sans (installed in the Docker image), falling back to letters without it.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import chess
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (CondPageBreak, Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from .analysis import win_pct

CLS = {
    "brilliant": ("!!", "Brilliant", "#1baca6"), "great": ("!", "Great", "#5c8bb0"),
    "best": ("★", "Best", "#81b64c"), "excellent": ("✓", "Excellent", "#96bc4b"),
    "good": ("✓", "Good", "#96af8b"), "book": ("≡", "Book", "#a88865"),
    "forced": ("→", "Forced", "#8f8f8f"), "inaccuracy": ("?!", "Inaccuracy", "#e6b422"),
    "mistake": ("?", "Mistake", "#e58f2a"), "miss": ("✗", "Miss", "#ee6b55"),
    "blunder": ("??", "Blunder", "#ca3431"),
}
TABLE_ORDER = ["brilliant", "great", "best", "excellent", "good", "book", "inaccuracy", "mistake", "miss", "blunder"]
BAD = {"inaccuracy", "mistake", "miss", "blunder"}
KEY = {"brilliant", "great", "miss", "blunder", "mistake"}

INK = HexColor("#262421")
MUTED = HexColor("#6f6c67")
ACCENT = HexColor("#5d8a32")
LIGHT_SQ, DARK_SQ = HexColor("#ebecd0"), HexColor("#779556")
LIGHT_HL, DARK_HL = HexColor("#f5f682"), HexColor("#b9ca43")
GLYPHS = {"k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟"}

FONT, BOLD, HAS_GLYPHS = "Helvetica", "Helvetica-Bold", False
_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts",
              str(Path.home() / "Library/Fonts"), "/opt/homebrew/share/fonts"]


def _register_fonts() -> None:
    global FONT, BOLD, HAS_GLYPHS
    for d in _FONT_DIRS:
        regular, bold = Path(d) / "DejaVuSans.ttf", Path(d) / "DejaVuSans-Bold.ttf"
        if regular.exists():
            pdfmetrics.registerFont(TTFont("DV", str(regular)))
            pdfmetrics.registerFont(TTFont("DV-Bold", str(bold if bold.exists() else regular)))
            pdfmetrics.registerFontFamily("DV", normal="DV", bold="DV-Bold", italic="DV", boldItalic="DV-Bold")
            FONT, BOLD, HAS_GLYPHS = "DV", "DV-Bold", True
            return


_register_fonts()


def style(name: str, **kw) -> ParagraphStyle:
    base = dict(fontName=FONT, fontSize=9.5, leading=13, textColor=INK)
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "title": style("title", fontName=BOLD, fontSize=20, leading=24),
    "sub": style("sub", fontSize=10, leading=14, textColor=MUTED),
    "h2": style("h2", fontName=BOLD, fontSize=13, leading=17, spaceBefore=10, spaceAfter=6),
    "body": style("body"),
    "small": style("small", fontSize=8, leading=10.5, textColor=MUTED),
    "note": style("note", fontSize=10, leading=14),
    "moment_head": style("mh", fontName=BOLD, fontSize=11, leading=15),
    "bullet": style("bullet", leftIndent=9, bulletIndent=0, spaceAfter=1.5),
    "tip": style("tip", fontSize=8.5, leading=11.5, textColor=HexColor("#3f5f22")),
    "cell": style("cell", fontSize=8.5, leading=10.5),
    "center": style("center", alignment=1),
}


def p(text: str, st: str = "body") -> Paragraph:
    return Paragraph(text, S[st])


def cls_tag(cls: str, with_label: bool = True) -> str:
    sym, label, color = CLS[cls]
    return f'<font color="{color}"><b>{escape(sym)}{" " + label if with_label else ""}</b></font>'


def move_label(m: dict) -> str:
    return f"{m['move_number']}{'.' if m['color'] == 'white' else '...'} {m['san']}"


def win_of(e: dict) -> float:
    if e.get("mate") is not None:
        return 100.0 if e["cp"] > 0 else 0.0
    return win_pct(e["cp"])


# ---------------------------------------------------------------- flowables

class BoardDiagram(Flowable):
    """A chess diagram with optional last-move highlight, arrows and a classification badge."""

    def __init__(self, fen: str, size: float, flipped: bool = False, last: tuple[str, str] | None = None,
                 arrows: list[tuple[str, str]] = (), badge: tuple[str, str] | None = None):
        super().__init__()
        self.fen, self.size, self.flipped = fen, size, flipped
        self.last, self.arrows, self.badge = last, list(arrows), badge

    def wrap(self, aw, ah):
        return self.size, self.size

    def _xy(self, sq: int) -> tuple[float, float]:
        s = self.size / 8
        f, r = chess.square_file(sq), chess.square_rank(sq)
        col, row = (7 - f, 7 - r) if self.flipped else (f, r)
        return col * s, row * s

    def draw(self):
        c, s = self.canv, self.size / 8
        board = chess.Board(self.fen)
        last = {chess.parse_square(x) for x in self.last} if self.last else set()
        for sq in chess.SQUARES:
            x, y = self._xy(sq)
            light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
            fill = (LIGHT_HL if light else DARK_HL) if sq in last else (LIGHT_SQ if light else DARK_SQ)
            c.setFillColor(fill)
            c.rect(x, y, s, s, fill=1, stroke=0)
            # Coordinates on the edge squares
            c.setFont(BOLD, s * 0.17)
            c.setFillColor(DARK_SQ if light else LIGHT_SQ)
            f, r = chess.square_file(sq), chess.square_rank(sq)
            if (f == 0 and not self.flipped) or (f == 7 and self.flipped):
                c.drawString(x + s * 0.05, y + s * 0.78, str(r + 1))
            if (r == 0 and not self.flipped) or (r == 7 and self.flipped):
                c.drawRightString(x + s * 0.95, y + s * 0.06, chess.FILE_NAMES[f])

        for sq, piece in board.piece_map().items():
            x, y = self._xy(sq)
            self._piece(x, y, s, piece)

        for uci, color in self.arrows:
            self._arrow(uci, color, s)

        if self.badge:
            sq, cls = self.badge
            x, y = self._xy(chess.parse_square(sq))
            sym, _, color = CLS[cls]
            r = s * 0.22
            cx, cy = x + s - r * 0.7, y + s - r * 0.7
            c.setFillColor(HexColor(color))
            c.setStrokeColor(colors.white)
            c.setLineWidth(r * 0.15)
            c.circle(cx, cy, r, fill=1, stroke=1)
            c.setFillColor(colors.white)
            fs = r * (1.0 if len(sym) == 1 else 0.85)
            c.setFont(BOLD, fs)
            c.drawCentredString(cx, cy - fs * 0.36, sym)

        c.setStrokeColor(HexColor("#4b4847"))
        c.setLineWidth(0.6)
        c.rect(0, 0, self.size, self.size, fill=0, stroke=1)

    def _piece(self, x: float, y: float, s: float, piece: chess.Piece):
        c = self.canv
        white = piece.color == chess.WHITE
        if HAS_GLYPHS:
            glyph, fs = GLYPHS[piece.symbol().lower()], s * 0.86
            t = c.beginText()
            t.setFont(FONT, fs)
            w = pdfmetrics.stringWidth(glyph, FONT, fs)
            t.setTextOrigin(x + (s - w) / 2, y + s * 0.13)
            if white:
                c.setFillColor(colors.white)
                c.setStrokeColor(HexColor("#222222"))
                c.setLineWidth(s * 0.028)
                t.setTextRenderMode(2)  # fill + stroke
            else:
                c.setFillColor(HexColor("#1e1d1b"))
                t.setTextRenderMode(0)
            t.textOut(glyph)
            c.drawText(t)
        else:
            c.setFillColor(colors.white if white else HexColor("#1e1d1b"))
            c.setStrokeColor(HexColor("#222222"))
            c.circle(x + s / 2, y + s / 2, s * 0.36, fill=1, stroke=1)
            c.setFillColor(HexColor("#1e1d1b") if white else colors.white)
            c.setFont(BOLD, s * 0.42)
            c.drawCentredString(x + s / 2, y + s * 0.35, piece.symbol().upper())

    def _arrow(self, uci: str, color: str, s: float):
        c = self.canv
        a, b = chess.parse_square(uci[:2]), chess.parse_square(uci[2:4])
        (x1, y1), (x2, y2) = self._xy(a), self._xy(b)
        x1, y1, x2, y2 = x1 + s / 2, y1 + s / 2, x2 + s / 2, y2 + s / 2
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5 or 1
        ux, uy = dx / length, dy / length
        head = s * 0.42
        bx, by = x2 - ux * head, y2 - uy * head
        col = HexColor(color)
        c.saveState()
        c.setStrokeColor(col)
        c.setFillColor(col)
        c.setStrokeAlpha(0.82)
        c.setFillAlpha(0.82)
        c.setLineWidth(s * 0.17)
        c.setLineCap(1)
        c.line(x1, y1, bx, by)
        path = c.beginPath()
        path.moveTo(x2, y2)
        path.lineTo(bx - uy * head * 0.55, by + ux * head * 0.55)
        path.lineTo(bx + uy * head * 0.55, by - ux * head * 0.55)
        path.close()
        c.drawPath(path, fill=1, stroke=0)
        c.restoreState()


class EvalGraph(Flowable):
    def __init__(self, review: dict, width: float, height: float):
        super().__init__()
        self.review, self.width, self.height = review, width, height

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c, w, h = self.canv, self.width, self.height
        r = self.review
        pts = [win_of(r["start_eval"])] + [win_of(m["eval"]) for m in r["moves"]]
        n = max(1, len(pts) - 1)
        c.setFillColor(HexColor("#403d39"))
        c.roundRect(0, 0, w, h, 3, fill=1, stroke=0)
        path = c.beginPath()
        path.moveTo(0, 0)
        for i, v in enumerate(pts):
            path.lineTo(i / n * w, v / 100 * h)
        path.lineTo(w, 0)
        path.close()
        c.setFillColor(colors.white)
        c.drawPath(path, fill=1, stroke=0)
        c.setStrokeColor(HexColor("#4b4847"))
        c.setLineWidth(0.6)
        c.roundRect(0, 0, w, h, 3, fill=0, stroke=1)
        c.setStrokeColor(HexColor("#999999"))
        c.setDash(2, 2)
        c.setLineWidth(0.5)
        c.line(0, h / 2, w, h / 2)
        c.setDash()
        for i, m in enumerate(r["moves"], start=1):
            if m["classification"] in KEY:
                c.setFillColor(HexColor(CLS[m["classification"]][2]))
                c.setStrokeColor(HexColor("#1f1e1b"))
                c.setLineWidth(0.6)
                c.circle(i / n * w, pts[i] / 100 * h, 2.4, fill=1, stroke=1)


# ---------------------------------------------------------------- content

def key_moments(review: dict, limit: int = 12) -> list[dict]:
    moves = review["moves"]
    picked = [m for m in moves if m["classification"] in KEY and m.get("best_move")]
    if len(picked) < 4:
        extra = sorted((m for m in moves if m["classification"] == "inaccuracy"), key=lambda m: -m["win_loss"])
        picked += extra[:4 - len(picked)]
    if len(picked) > limit:
        # Keep the most consequential ones, but always the brilliant/great moves.
        star = [m for m in picked if m["classification"] in ("brilliant", "great")]
        rest = sorted((m for m in picked if m not in star), key=lambda m: -m["win_loss"])
        picked = star[:4] + rest[:limit - len(star[:4])]
    return sorted(picked, key=lambda m: m["ply"])


def puzzles(review: dict, limit: int = 6) -> list[dict]:
    """Positions where one side missed a strong move: good 'find the best move' exercises."""
    cands = [m for m in review["moves"] if m["classification"] in ("blunder", "mistake", "miss")
             and m.get("best_move") and m["best_move"] != m["uci"]]
    cands.sort(key=lambda m: -m["win_loss"])
    return sorted(cands[:limit], key=lambda m: m["ply"])


def narrative(review: dict) -> list[str]:
    r, moves, s = review, review["moves"], review["summary"]
    out = []
    book = sum(1 for m in moves if m["classification"] == "book")
    if r.get("opening"):
        out.append(f"The game opened with the <b>{escape(r['opening'])}</b>"
                   + (f", and both sides followed theory for {book} half-moves." if book > 1 else "."))
    wa, ba = s["white"]["accuracy"], s["black"]["accuracy"]
    if wa is not None and ba is not None:
        if abs(wa - ba) < 2:
            out.append(f"Both players were about equally accurate ({wa}% vs {ba}%).")
        else:
            better, worse = ("White", "Black") if wa > ba else ("Black", "White")
            name = escape(r["white"] if better == "White" else r["black"])
            out.append(f"<b>{name}</b> ({better}) played more accurately: {max(wa, ba)}% vs {min(wa, ba)}%.")
    bad = [m for m in moves if m["classification"] in BAD]
    if bad:
        turning = max(bad, key=lambda m: m["win_loss"])
        out.append(f"The turning point was <b>{escape(move_label(turning))}</b> ({cls_tag(turning['classification'])}), "
                   f"after which the evaluation went from {escape(turning['best_eval_text'])} to {escape(turning['eval_text'])}.")
    stars = [m for m in moves if m["classification"] in ("brilliant", "great")]
    if stars:
        top = next((m for m in stars if m["classification"] == "brilliant"), stars[0])
        out.append(f"The highlight: <b>{escape(move_label(top))}</b>, a {CLS[top['classification']][1].lower()} move.")
    if r.get("termination"):
        out.append(escape(r["termination"]) + ".")
    return out


def _date(headers: dict) -> str:
    raw = headers.get("UTCDate") or headers.get("Date") or ""
    try:
        return datetime.strptime(raw, "%Y.%m.%d").strftime("%d %B %Y")
    except ValueError:
        return ""


def _time_control(tc: str | None) -> str:
    if not tc or tc == "-":
        return ""
    if "/" in tc:
        return "Daily"
    base, _, inc = tc.partition("+")
    try:
        mins = int(base) / 60
    except ValueError:
        return tc
    return f"{mins:g} min" + (f" + {inc}s" if inc and inc != "0" else "")


def filename(review: dict) -> str:
    def clean(x):
        return re.sub(r"[^A-Za-z0-9_-]+", "", x or "")[:30] or "player"
    d = (review["headers"].get("UTCDate") or review["headers"].get("Date") or "").replace(".", "-").replace("?", "")
    return f"review-{clean(review['white'])}-vs-{clean(review['black'])}{'-' + d if d.strip('-') else ''}.pdf"


def build_pdf(review: dict, perspective: str | None = None, note: str = "", sender: str = "",
              link: str | None = None, include_puzzles: bool = True, include_moves: bool = True) -> bytes:
    buf = io.BytesIO()
    W, H = A4
    margin = 16 * mm
    content_w = W - 2 * margin
    flipped = perspective == "black"
    title = f"{review['white'] or 'White'} vs {review['black'] or 'Black'}"
    headers = review["headers"]

    def footer(canv, doc):
        canv.saveState()
        canv.setFont(FONT, 7.5)
        canv.setFillColor(MUTED)
        canv.drawString(margin, 9 * mm, f"♞ Chess Game Review · {title}" if HAS_GLYPHS else f"Chess Game Review - {title}")
        canv.drawRightString(W - margin, 9 * mm, f"Page {doc.page}")
        if link:
            canv.linkURL(link, (margin, 7 * mm, W - margin, 12 * mm), relative=0)
        canv.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Game review: {title}", author=sender or "Chess Game Review",
                            subject="Chess game analysis")
    story: list = []

    # --- header
    meta = " · ".join(x for x in [escape(review.get("result") or ""), escape(_time_control(headers.get("TimeControl"))),
                                   escape(_date(headers)), escape(headers.get("Event", "") if headers.get("Event", "?") != "?" else "")] if x)
    story += [p(escape(title), "title"), Spacer(1, 2), p(meta, "sub")]
    if review.get("opening"):
        story.append(p(f"{escape(review.get('eco') or '')} {escape(review['opening'])}", "sub"))
    story.append(Spacer(1, 8))

    if note.strip() or sender.strip():
        who = f"<b>{escape(sender.strip())}</b> shared this game review with you" if sender.strip() else "A note for you"
        body = escape(note.strip()).replace("\n", "<br/>")
        box = Table([[p(who + (":" if body else "."), "note")]] + ([[p(body, "note")]] if body else []),
                    colWidths=[content_w])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), HexColor("#f1f6ea")),
                                 ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story += [box, Spacer(1, 10)]

    # --- accuracy + classification table next to the final position
    s = review["summary"]

    def acc_cell(color):
        name = review[color] or color.title()
        elo = review.get(f"{color}_elo")
        acc = s[color]["accuracy"]
        t = Table([[p(f"<b>{escape(name)}</b>" + (f" ({escape(elo)})" if elo else ""), "center")],
                   [Paragraph(f"<b>{acc if acc is not None else '–'}</b>", style("acc", fontName=BOLD, fontSize=22,
                              leading=26, alignment=1, textColor=INK if color == "white" else colors.white))],
                   [Paragraph("Accuracy", style("accl", fontSize=8, textColor=MUTED, alignment=1))]],
                  colWidths=[40 * mm])
        t.setStyle(TableStyle([("BACKGROUND", (0, 1), (0, 1), colors.white if color == "white" else HexColor("#403d39")),
                               ("BOX", (0, 1), (0, 1), 0.6, HexColor("#cfcdc9")), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                               ("TOPPADDING", (0, 1), (0, 1), 4), ("BOTTOMPADDING", (0, 1), (0, 1), 6)]))
        return t

    rows = [[p(f"<b>{s['white']['counts'][c]}</b>", "center"), p(cls_tag(c)), p(f"<b>{s['black']['counts'][c]}</b>", "center")]
            for c in TABLE_ORDER]
    cls_table = Table(rows, colWidths=[13 * mm, 30 * mm, 13 * mm], rowHeights=5.6 * mm)
    cls_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    phases = [[p(f"{s['white']['phase_accuracy'][ph] or '–'}", "center"), p(ph.title(), "small"),
               p(f"{s['black']['phase_accuracy'][ph] or '–'}", "center")] for ph in ("opening", "middlegame", "endgame")]
    phase_table = Table(phases, colWidths=[13 * mm, 30 * mm, 13 * mm], rowHeights=5 * mm)
    left = Table([[acc_cell("white"), acc_cell("black")], [cls_table, ""], [phase_table, ""]],
                 colWidths=[43 * mm, 43 * mm])
    left.setStyle(TableStyle([("SPAN", (0, 1), (1, 1)), ("SPAN", (0, 2), (1, 2)), ("ALIGN", (0, 1), (-1, -1), "CENTER"),
                              ("TOPPADDING", (0, 1), (-1, 1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    final = review["moves"][-1] if review["moves"] else None
    board_size = content_w - 90 * mm
    final_board = BoardDiagram(final["fen"] if final else review["start_fen"], board_size, flipped,
                               last=(final["from"], final["to"]) if final else None,
                               badge=(final["to"], final["classification"]) if final else None)
    top = Table([[left, [final_board, Spacer(1, 2), p("Final position", "small")]]], colWidths=[90 * mm, board_size])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [top, Spacer(1, 10)]

    story.append(p("How the game went", "h2"))
    for line in narrative(review):
        story.append(Paragraph(line, S["bullet"], bulletText="•"))
    story += [Spacer(1, 6), EvalGraph(review, content_w, 24 * mm), Spacer(1, 3),
              p("Evaluation over the game (white area = White's winning chances)", "small")]

    # --- puzzles for the reader
    pz = puzzles(review) if include_puzzles else []
    if pz:
        story += [PageBreak(), p("Your turn: puzzles from this game", "h2"),
                  p("In each position a player went wrong. Can you find the move they missed? Try them before reading "
                    "the key moments. Solutions are at the end.", "small"), Spacer(1, 8)]
        size = (content_w - 12 * mm) / 2
        cells = []
        for i, m in enumerate(pz, start=1):
            prev = review["moves"][m["ply"] - 2] if m["ply"] >= 2 else None
            cells.append([BoardDiagram(m["fen_before"], size, m["color"] == "black",
                                       last=(prev["from"], prev["to"]) if prev else None),
                          Spacer(1, 3), p(f"<b>Puzzle {i}.</b> {m['color'].title()} to move (move {m['move_number']}).", "body")])
        grid = [cells[i:i + 2] + ([""] if len(cells[i:i + 2]) == 1 else []) for i in range(0, len(cells), 2)]
        t = Table(grid, colWidths=[size + 6 * mm] * 2)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(t)

    # --- key moments
    moments = key_moments(review)
    if moments:
        story += [PageBreak(), p("Key moments", "h2"),
                  p("Each diagram shows the position <b>before</b> the move. The coloured arrow is the move played, "
                    "the green arrow the engine's best move.", "small"), Spacer(1, 6)]
        diag = 62 * mm
        for m in moments:
            cls = m["classification"]
            arrows = []
            if m.get("best_move") and m["best_move"] != m["uci"]:
                arrows.append((m["best_move"], "#5d9e2f"))
            arrows.append((m["uci"], CLS[cls][2]))
            idx = m["ply"] - 2
            prev = review["moves"][idx] if idx >= 0 else None
            board = BoardDiagram(m["fen_before"], diag, flipped, last=(prev["from"], prev["to"]) if prev else None,
                                 arrows=arrows)
            text = [p(f"{escape(move_label(m))} &nbsp; {cls_tag(cls)} &nbsp; "
                      f"<font size=8 color='#6f6c67'>{escape(m['best_eval_text'])} → {escape(m['eval_text'])}</font>",
                      "moment_head"), Spacer(1, 3)]
            for n in m["explanation"][1:]:
                text.append(Paragraph(escape(n), S["bullet"], bulletText="•"))
            if m.get("tip"):
                text += [Spacer(1, 3), p(f"<b>Coach tip:</b> {escape(m['tip'])}", "tip")]
            if m.get("best_line") and m["best_move"] != m["uci"]:
                text += [Spacer(1, 3), p(f"<b>Best line:</b> {escape(' '.join(m['best_line'][:8]))}", "small")]
            row = Table([[board, text]], colWidths=[diag + 6 * mm, content_w - diag - 6 * mm])
            row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                     ("LINEBELOW", (0, 0), (-1, -1), 0.4, HexColor("#dddbd6")),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6)]))
            story.append(KeepTogether(row))

    # --- full move list
    if include_moves and review["moves"]:
        story += [PageBreak(), p("All moves", "h2")]
        by_num: dict[int, dict] = {}
        for m in review["moves"]:
            by_num.setdefault(m["move_number"], {})[m["color"]] = m

        def cell(m):
            if not m:
                return "", ""
            sym, _, color = CLS[m["classification"]]
            return (p(f"{escape(m['san'])} <font color='{color}'><b>{escape(sym)}</b></font>", "cell"),
                    p(f"<font color='#6f6c67'>{escape(m['eval_text'])}</font>", "cell"))

        rows = [[p("<b>#</b>", "cell"), p("<b>White</b>", "cell"), "", p("<b>Black</b>", "cell"), ""]]
        for num in sorted(by_num):
            w, b = cell(by_num[num].get("white")), cell(by_num[num].get("black"))
            rows.append([p(f"{num}.", "cell"), w[0], w[1], b[0], b[1]])
        inner = content_w / 2 - 4 * mm
        rest = inner - 9 * mm
        cw = [9 * mm, rest * 0.3, rest * 0.2, rest * 0.3, rest * 0.2]

        def table(chunk):
            t = Table(chunk, colWidths=cw, rowHeights=13, repeatRows=1)
            t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, INK), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#f4f3f0")]),
                                   ("TOPPADDING", (0, 0), (-1, -1), 1.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2)]))
            return t

        # Two columns of moves per page to keep long games compact.
        body = rows[1:]
        per_col = min(48, -(-len(body) // 2))
        for start in range(0, len(body), per_col * 2):
            a = [rows[0]] + body[start:start + per_col]
            b = body[start + per_col:start + per_col * 2]
            pair = Table([[table(a), table([rows[0]] + b) if b else ""]], colWidths=[content_w / 2] * 2)
            pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [pair, CondPageBreak(40 * mm)]

        legend = " &nbsp; ".join(f"{cls_tag(c, False)} {CLS[c][1]}" for c in TABLE_ORDER)
        story += [Spacer(1, 6), p(legend, "small")]

    # --- puzzle solutions
    if pz:
        story += [CondPageBreak(70 * mm), p("Puzzle solutions", "h2")]
        for i, m in enumerate(pz, start=1):
            line = " ".join(m.get("best_line", [])[:6])
            reasons = [n for n in m["explanation"][1:3] if not n.startswith(("Better was", "White is", "Black is"))]
            game = (f"In the game, {escape(m['san'])} was played ({cls_tag(m['classification'])}): "
                    f"{escape(' '.join(reasons))}") if reasons else ""
            story.append(Paragraph(f"<b>Puzzle {i}: {escape(m['best_san'])}</b> ({escape(m['best_eval_text'])}) "
                                   f"&nbsp;<font color='#6f6c67'>{escape(line)}</font>" + (f"<br/>{game}" if game else ""),
                                   S["bullet"], bulletText=f"{i}."))

    story += [Spacer(1, 14), p(
        f"Analysis by Stockfish at depth {review.get('depth')}. Generated by Chess Game Review."
        + (f" Interactive version: <link href='{escape(link)}' color='#5d8a32'>{escape(link)}</link>" if link else "")
        + (f" · Original game: <link href='{escape(headers['Link'])}' color='#5d8a32'>{escape(headers['Link'])}</link>"
           if headers.get("Link", "").startswith("https://") else ""), "small")]

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
