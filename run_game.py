"""Run one game.

  .venv\\Scripts\\python.exe run_game.py --seed 1 --serve
  .venv\\Scripts\\python.exe run_game.py --stub            (free: random players, no API calls)
"""

import argparse
import json
import random
import threading
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lineup", default=str(ROOT / "config" / "lineup.json"))
    ap.add_argument("--seed", type=int, default=None, help="board/dice seed (>=1). Same seed = same board.")
    ap.add_argument("--rotate", type=int, default=0, help="shift seat order by N (for seat-rotation runs)")
    ap.add_argument("--max-cost", type=float, default=30.0, help="stop the game once estimated spend hits this (USD)")
    ap.add_argument("--max-turns", type=int, default=400, help="safety cap on total player-turns")
    ap.add_argument("--trade-limit", type=int, default=2, help="trade proposals allowed per turn")
    ap.add_argument("--stub", action="store_true", help="random stand-in players, no API calls")
    ap.add_argument("--serve", action="store_true", help="also start the viewer at http://localhost:8765")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    lineup = json.loads(Path(args.lineup).read_text(encoding="utf-8"))
    seats = lineup["seats"]
    k = args.rotate % len(seats)
    lineup["seats"] = seats[k:] + seats[:k]
    seed = args.seed or random.randrange(1, 10**6)

    from toowood.runner import GameRunner

    if args.serve:
        from serve import serve
        threading.Thread(target=serve, kwargs={"port": 8765}, daemon=True).start()
        print("Viewer: http://localhost:8765")

    runner = GameRunner(lineup, seed=seed, out_root=str(ROOT / "games"), max_cost=args.max_cost,
                        max_turns=args.max_turns, trade_limit=args.trade_limit, stub=args.stub)
    print(f"Game {runner.game_id} -> {runner.dir}")
    winner, reason = runner.run()
    print(f"Done: winner={winner.value if winner else None} ({reason}); "
          f"estimated cost ${runner.cost_total:.2f}; per player: "
          + ", ".join(f"{c} ${v:.2f}" for c, v in runner.cost_by.items()))
    print(f"Transcript: {runner.dir / 'transcript.md'}")
    if args.serve:
        input("Viewer still running - press Enter to stop.")


if __name__ == "__main__":
    main()
