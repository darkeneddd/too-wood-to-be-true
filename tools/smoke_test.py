"""One real API call per seat on the game's opening decision (no game is played).
Checks keys, model names, the JSON reply format and thinking summaries.

  .venv\\Scripts\\python.exe tools\\smoke_test.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from catanatron import Color  # noqa: E402
from toowood.agents import make_agent  # noqa: E402
from toowood.board import BoardInfo  # noqa: E402
from toowood.engine import GameSession  # noqa: E402
from toowood.prompts import ACTION_SCHEMA, describe_action, options_block, state_view, system_prompt  # noqa: E402
from toowood.runner import REPLY_HINT  # noqa: E402

lineup = json.loads((ROOT / "config" / "lineup.json").read_text(encoding="utf-8"))
s = GameSession([Color.RED, Color.BLUE, Color.ORANGE, Color.WHITE], seed=1)
board = BoardInfo(s.board_map)
system = system_prompt(board.static_text())
labels = [describe_action(a, s, board) for a in s.legal_actions()]
total = 0.0
for seat in lineup["seats"]:
    color = Color[seat["color"]]
    user = "\n".join([state_view(s, board, color, "", [], [], 0), "",
                      "DECISION: SETUP: place your FIRST settlement.", "", options_block(labels), "",
                      REPLY_HINT["action"]])
    print(f"\n=== {seat['color']}: {seat['provider']} / {seat['model']} ===")
    try:
        agent = make_agent(color, seat)
        r = agent.call(system, user, "action", ACTION_SCHEMA, cache_key="toowood-smoke")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {str(e)[:600]}")
        continue
    total += r.cost
    d = r.data
    ok = isinstance(d.get("choice"), int) and 0 <= d["choice"] < len(labels)
    print(f"served model: {r.model} | {r.latency:.1f}s | usage {r.usage} | est ${r.cost:.4f}")
    print(f"valid choice: {ok} -> {labels[d['choice']] if ok else d.get('choice')}")
    print(f"reason: {d.get('reason')}")
    print(f"say: {d.get('say')!r}")
    print(f"thinking summary: {len(r.thinking)} chars" + (f" | starts: {r.thinking[:300]!r}" if r.thinking else " (NONE)"))
    if r.notes:
        print(f"notes: {r.notes}")
print(f"\nTotal estimated cost: ${total:.4f}")
