from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chess

from . import analysis, chesscom, coach, pdf_report

ENGINE_THREADS = int(os.environ.get("ENGINE_THREADS", "2"))
ENGINE_HASH_MB = int(os.environ.get("ENGINE_HASH_MB", "128"))
DEFAULT_DEPTH = int(os.environ.get("DEFAULT_DEPTH", "16"))
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "24"))
COACH_DEPTH = int(os.environ.get("COACH_DEPTH", "16"))
# Bump when the review format changes so stale cached reviews are recomputed.
CACHE_VERSION = "7"
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent / "data"))
CACHE_DIR = DATA_DIR / "analyses"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

STATIC = Path(__file__).with_name("static")

app = FastAPI(title="Chess Game Review")
executor = ThreadPoolExecutor(max_workers=int(os.environ.get("ANALYSIS_WORKERS", "2")))
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _wrap(exc: chesscom.ChessComError):
    raise HTTPException(status_code=exc.status if exc.status in (404, 429) else 502, detail=str(exc))


@app.get("/api/health")
def health():
    return {"ok": True, "stockfish": analysis.stockfish_path(), "openings": len(analysis.openings())}


@app.get("/api/player/{username}")
async def player(username: str):
    try:
        return await chesscom.profile(username)
    except chesscom.ChessComError as e:
        _wrap(e)


@app.get("/api/player/{username}/archives")
async def archives(username: str):
    try:
        return await chesscom.archives(username)
    except chesscom.ChessComError as e:
        _wrap(e)


@app.get("/api/player/{username}/games/{year}/{month}")
async def games(username: str, year: int, month: int):
    try:
        return await chesscom.games(username, year, month)
    except chesscom.ChessComError as e:
        _wrap(e)


@app.get("/api/player/{username}/insights")
async def insights(username: str, months: int = Query(3, ge=1, le=24)):
    try:
        return await chesscom.insights(username, months)
    except chesscom.ChessComError as e:
        _wrap(e)


class AnalyzeRequest(BaseModel):
    pgn: str = Field(..., min_length=1, max_length=200_000)
    depth: int | None = None


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _run_job(job_id: str, pgn: str, depth: int, key: str):
    def progress(done: int, total: int):
        with jobs_lock:
            jobs[job_id].update(progress=done, total=total)

    try:
        result = analysis.analyse_pgn(pgn, depth, ENGINE_THREADS, ENGINE_HASH_MB, progress)
        _cache_path(key).write_text(json.dumps(result))
        with jobs_lock:
            jobs[job_id].update(status="done", result=result)
    except Exception as e:  # surfaced to the client
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(e))


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    depth = max(6, min(MAX_DEPTH, req.depth or DEFAULT_DEPTH))
    pgn = req.pgn.strip()
    key = hashlib.sha1(f"v{CACHE_VERSION}:{depth}:{pgn}".encode()).hexdigest()
    job_id = uuid.uuid4().hex

    cached = _cache_path(key)
    if cached.exists():
        with jobs_lock:
            jobs[job_id] = {"status": "done", "progress": 1, "total": 1, "created": time.time(),
                            "result": json.loads(cached.read_text())}
        return {"job_id": job_id, "key": key, "cached": True}

    with jobs_lock:
        # Reuse an in-flight job for the same game/depth.
        for jid, j in jobs.items():
            if j.get("key") == key and j["status"] == "running":
                return {"job_id": jid, "key": key, "cached": False}
        # Drop old finished jobs to keep memory bounded.
        cutoff = time.time() - 3600
        for jid in [jid for jid, j in jobs.items() if j["status"] != "running" and j["created"] < cutoff]:
            del jobs[jid]
        jobs[job_id] = {"status": "running", "progress": 0, "total": 0, "created": time.time(), "key": key}
    executor.submit(_run_job, job_id, pgn, depth, key)
    return {"job_id": job_id, "key": key, "cached": False}


@app.get("/api/analysis/{key}")
def saved_analysis(key: str):
    """A finished review by its cache key; powers shareable #/review/<key> links."""
    if not re.fullmatch(r"[0-9a-f]{40}", key) or not _cache_path(key).exists():
        raise HTTPException(404, "Analysis not found")
    return FileResponse(_cache_path(key), media_type="application/json")


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    with jobs_lock:
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "Unknown job")
        out = {k: v for k, v in j.items() if k not in ("key", "created")}
    return out


class PdfRequest(BaseModel):
    perspective: str | None = Field(None, pattern="^(white|black)$")
    sender: str = Field("", max_length=60)
    note: str = Field("", max_length=1000)
    link: str | None = Field(None, max_length=300, pattern="^https?://")
    puzzles: bool = True
    moves: bool = True


@app.post("/api/analysis/{key}/pdf")
def analysis_pdf(key: str, req: PdfRequest):
    """A shareable PDF of a finished review."""
    if not re.fullmatch(r"[0-9a-f]{40}", key) or not _cache_path(key).exists():
        raise HTTPException(404, "Analysis not found")
    review = json.loads(_cache_path(key).read_text())
    data = pdf_report.build_pdf(review, perspective=req.perspective, note=req.note, sender=req.sender,
                                link=req.link, include_puzzles=req.puzzles, include_moves=req.moves)
    name = pdf_report.filename(review)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ coach

coach_engine = coach.CoachEngine(COACH_DEPTH, ENGINE_THREADS, ENGINE_HASH_MB)


class PositionRequest(BaseModel):
    fen: str = Field(..., max_length=100)
    engine: bool = True


class MoveRequest(PositionRequest):
    uci: str = Field(..., min_length=4, max_length=5)


class LineRequest(PositionRequest):
    moves: list[str] = Field(..., max_length=40)


def _board(fen: str) -> chess.Board:
    try:
        board = chess.Board(fen)
    except ValueError:
        raise HTTPException(400, "Invalid FEN")
    if not board.is_valid():
        raise HTTPException(400, "Illegal position")
    return board


def _line_info(board: chess.Board, line: dict) -> dict:
    pv = line["pv"]
    return {
        "uci": pv[0].uci() if pv else None,
        "san": board.san(pv[0]) if pv else None,
        "eval": line["eval"],
        "eval_text": analysis.format_eval(line["eval"]),
        "line": analysis.pv_to_san(board, pv, 8),
        "pv": [m.uci() for m in pv[:10]],
    }


def _position(board: chess.Board) -> dict:
    lines = coach_engine.analyse(board, 2)
    best = lines[0]
    return {
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "legal": [m.uci() for m in board.legal_moves],
        "game_over": board.is_game_over(claim_draw=False),
        "check": board.is_check(),
        "eval": best["eval"],
        "eval_text": analysis.format_eval(best["eval"]),
        "best": _line_info(board, best),
        "hint": coach.hint_for(board, best["pv"][0], best["eval"]) if best["pv"] else None,
    }


@app.post("/api/coach/position")
def coach_position(req: PositionRequest):
    """Legal moves, engine assessment and hints for any position."""
    board = _board(req.fen)
    if not req.engine:
        return {"fen": board.fen(), "legal": [m.uci() for m in board.legal_moves]}
    return _position(board)


@app.post("/api/coach/move")
def coach_move(req: MoveRequest):
    """Grade a move the user tried and explain it like a coach would."""
    board = _board(req.fen)
    try:
        move = chess.Move.from_uci(req.uci)
    except ValueError:
        raise HTTPException(400, "Invalid move")
    piece = board.piece_at(move.from_square)
    if piece and piece.piece_type == chess.PAWN and chess.square_rank(move.to_square) in (0, 7) and not move.promotion:
        move.promotion = chess.QUEEN
    if not board.is_legal(move):
        raise HTTPException(400, "Illegal move")

    before_lines = coach_engine.analyse(board, 2)
    after = board.copy()
    after.push(move)
    after_pos = _position(after)
    after_line = coach_engine.analyse(after, 2)[0]
    reply_pv = after_line["pv"]
    book = analysis.openings()
    book_entry = book.get(after.epd()) if board.epd() in book or board.fullmove_number == 1 else None
    c = analysis.classify(board, move, before_lines[0], before_lines[1] if len(before_lines) > 1 else None,
                          after_line["eval"], reply_pv, book_entry=book_entry)
    second = before_lines[1] if len(before_lines) > 1 else None
    notes = coach.explain_move(board, move, c["cls"], before_lines[0], after_line["eval"], reply_pv,
                               opening=book_entry["name"] if book_entry else None,
                               second_san=board.san(second["pv"][0]) if second else None,
                               second_eval_text=analysis.format_eval(second["eval"]) if second else None,
                               sac=c["sac"])
    return {
        "san": board.san(move),
        "uci": move.uci(),
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
        "classification": c["cls"],
        "correct": c["cls"] in coach.CORRECT,
        "win_loss": round(c["loss"], 1),
        "best": _line_info(board, before_lines[0]),
        "reply": _line_info(after, after_line),
        "after": after_pos,
        **notes,
    }


@app.post("/api/coach/line")
def coach_line(req: LineRequest):
    """Positions along a line of SAN or UCI moves (for stepping through engine lines)."""
    board = _board(req.fen)
    out = []
    for token in req.moves:
        try:
            move = board.parse_san(token)
        except ValueError:
            try:
                move = chess.Move.from_uci(token)
            except ValueError:
                break
            if not board.is_legal(move):
                break
        out.append({"uci": move.uci(), "san": board.san(move), "from": chess.square_name(move.from_square),
                    "to": chess.square_name(move.to_square)})
        board.push(move)
        out[-1]["fen"] = board.fen()
        out[-1]["check"] = board.is_check()
    return out


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
