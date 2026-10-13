"""Thin async client for the public chess.com API (https://www.chess.com/news/view/published-data-api)."""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Any

import httpx

API = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "chess-analysis-self-hosted/1.0 (personal game review tool)"}

DRAW_RESULTS = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}

_cache: dict[str, tuple[float, Any]] = {}
_client: httpx.AsyncClient | None = None


class ChessComError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True)
    return _client


async def _get(url: str, ttl: float = 300) -> Any:
    hit = _cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        resp = await client().get(url)
    except httpx.HTTPError as e:
        raise ChessComError(502, f"Could not reach chess.com ({type(e).__name__}), try again") from e
    if resp.status_code == 404:
        raise ChessComError(404, "Player or resource not found on chess.com")
    if resp.status_code == 429:
        raise ChessComError(429, "chess.com rate limit reached, try again in a moment")
    if resp.status_code >= 400:
        raise ChessComError(resp.status_code, f"chess.com returned HTTP {resp.status_code}")
    data = resp.json()
    _cache[url] = (time.time() + ttl, data)
    return data


async def profile(username: str) -> dict:
    u = username.lower()
    prof = await _get(f"{API}/player/{u}", 3600)
    try:
        stats = await _get(f"{API}/player/{u}/stats", 600)
    except ChessComError:
        # chess.com's stats endpoint fails for some accounts; the profile is still usable.
        stats = {}
    ratings = {}
    for key in ("chess_bullet", "chess_blitz", "chess_rapid", "chess_daily"):
        if key in stats:
            s = stats[key]
            ratings[key.removeprefix("chess_")] = {
                "rating": s.get("last", {}).get("rating"),
                "best": s.get("best", {}).get("rating"),
                "record": s.get("record", {}),
            }
    if not ratings:
        ratings = await _ratings_from_games(u)
    return {
        "username": prof.get("username", username),
        "name": prof.get("name"),
        "avatar": prof.get("avatar"),
        "title": prof.get("title"),
        "country": (prof.get("country") or "").rsplit("/", 1)[-1],
        "joined": prof.get("joined"),
        "url": prof.get("url"),
        "ratings": ratings,
        "puzzle_best": stats.get("tactics", {}).get("highest", {}).get("rating"),
    }


async def _ratings_from_games(username: str) -> dict:
    """Fallback when /stats is unavailable: latest rating per time class from recent games."""
    ratings: dict = {}
    try:
        for a in (await archives(username))[:3]:
            for g in await games(username, a["year"], a["month"]):
                tc = g["time_class"]
                if g["rules"] == "chess" and g["rated"] and tc not in ratings:
                    ratings[tc] = {"rating": g[g["player_color"]]["rating"], "best": None, "record": {}}
    except ChessComError:
        pass
    return ratings


async def archives(username: str) -> list[dict]:
    data = await _get(f"{API}/player/{username.lower()}/games/archives", 600)
    out = []
    for url in data.get("archives", []):
        m = re.search(r"/(\d{4})/(\d{2})$", url)
        if m:
            out.append({"year": int(m.group(1)), "month": int(m.group(2))})
    return list(reversed(out))


def opening_from_eco_url(url: str | None) -> str | None:
    if not url:
        return None
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    # Strip move-sequence tails like "...-3.Nc3-Nf6" to get a family name.
    slug = re.sub(r"-\d+\..*$", "", slug)
    return slug.replace("-", " ")


def _result_for(game: dict, username: str) -> tuple[str, str]:
    """Return (color, outcome) from the player's perspective: outcome in win/draw/loss."""
    u = username.lower()
    color = "white" if game["white"]["username"].lower() == u else "black"
    res = game[color]["result"]
    if res == "win":
        return color, "win"
    if res in DRAW_RESULTS:
        return color, "draw"
    return color, "loss"


def summarize_game(game: dict, username: str) -> dict:
    color, outcome = _result_for(game, username)
    opp = "black" if color == "white" else "white"
    loser_reason = game[opp]["result"] if outcome == "win" else game[color]["result"]
    return {
        "url": game.get("url"),
        "uuid": game.get("uuid"),
        "end_time": game.get("end_time"),
        "time_class": game.get("time_class"),
        "time_control": game.get("time_control"),
        "rated": game.get("rated"),
        "rules": game.get("rules"),
        "white": {k: game["white"].get(k) for k in ("username", "rating", "result")},
        "black": {k: game["black"].get(k) for k in ("username", "rating", "result")},
        "player_color": color,
        "outcome": outcome,
        "reason": loser_reason,
        "opening": opening_from_eco_url(game.get("eco")),
        "accuracies": game.get("accuracies"),
        "pgn": game.get("pgn"),
    }


async def games(username: str, year: int, month: int) -> list[dict]:
    data = await _get(f"{API}/player/{username.lower()}/games/{year}/{month:02d}", 300)
    out = [summarize_game(g, username) for g in data.get("games", []) if "pgn" in g]
    out.sort(key=lambda g: g["end_time"] or 0, reverse=True)
    return out


async def insights(username: str, months: int) -> dict:
    """Aggregate statistics across the player's most recent `months` monthly archives."""
    arch = (await archives(username))[:months]
    month_games = await asyncio.gather(*(games(username, a["year"], a["month"]) for a in arch))
    all_games = [g for mg in month_games for g in mg if g["rules"] == "chess"]
    all_games.sort(key=lambda g: g["end_time"] or 0)

    def bucket():
        return {"win": 0, "draw": 0, "loss": 0, "games": 0}

    total = bucket()
    by_color = defaultdict(bucket)
    by_class = defaultdict(bucket)
    by_opening = defaultdict(lambda: {"win": 0, "draw": 0, "loss": 0, "games": 0, "white": 0, "black": 0})
    reasons_win = defaultdict(int)
    reasons_loss = defaultdict(int)
    rating_series = defaultdict(list)
    acc_sum = defaultdict(float)
    acc_n = defaultdict(int)
    by_hour = defaultdict(bucket)

    for g in all_games:
        o, c = g["outcome"], g["player_color"]
        for b in (total, by_color[c], by_class[g["time_class"]]):
            b[o] += 1
            b["games"] += 1
        if g["opening"]:
            ob = by_opening[g["opening"]]
            ob[o] += 1
            ob["games"] += 1
            ob[c] += 1
        if o == "win":
            reasons_win[g["reason"]] += 1
        elif o == "loss":
            reasons_loss[g["reason"]] += 1
        if g["rated"] and g["end_time"]:
            rating_series[g["time_class"]].append([g["end_time"], g[c]["rating"]])
        if g["accuracies"] and c in g["accuracies"]:
            acc_sum[g["time_class"]] += g["accuracies"][c]
            acc_n[g["time_class"]] += 1
        if g["end_time"]:
            hour = time.gmtime(g["end_time"]).tm_hour
            hb = by_hour[hour]
            hb[o] += 1
            hb["games"] += 1

    openings = sorted(({"name": k, **v} for k, v in by_opening.items()), key=lambda x: -x["games"])
    return {
        "months": [f"{a['year']}-{a['month']:02d}" for a in arch],
        "total": total,
        "by_color": dict(by_color),
        "by_time_class": dict(by_class),
        "openings": openings[:12],
        "win_reasons": dict(reasons_win),
        "loss_reasons": dict(reasons_loss),
        "rating_series": dict(rating_series),
        "avg_accuracy": {k: round(acc_sum[k] / acc_n[k], 1) for k in acc_n},
        "by_hour_utc": {str(k): v for k, v in sorted(by_hour.items())},
    }
