"""Download the lichess chess-openings dataset and index it by position (EPD)."""
import io
import json
import sys
import urllib.request

import chess
import chess.pgn

BASE = "https://raw.githubusercontent.com/lichess-org/chess-openings/master/{}.tsv"


def main(out_path: str) -> None:
    book = {}
    for letter in "abcde":
        req = urllib.request.Request(BASE.format(letter), headers={"User-Agent": "chess-analysis-build"})
        text = urllib.request.urlopen(req, timeout=30).read().decode()
        for line in text.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            eco, name, pgn = parts[0], parts[1], parts[2]
            game = chess.pgn.read_game(io.StringIO(pgn))
            if game is None:
                continue
            board = game.board()
            moves = list(game.mainline_moves())
            for i, move in enumerate(moves):
                board.push(move)
                epd = board.epd()
                final = i == len(moves) - 1
                # A position named by its own line wins over one that is merely a prefix of a longer line.
                entry = book.get(epd)
                if entry is None or (final and not entry["exact"]):
                    book[epd] = {"eco": eco, "name": name, "ply": i + 1, "exact": final}
    with open(out_path, "w") as f:
        json.dump(book, f)
    print(f"wrote {len(book)} opening positions to {out_path}")


if __name__ == "__main__":
    main(sys.argv[1])
