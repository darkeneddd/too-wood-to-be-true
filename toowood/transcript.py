"""Turn a game's events.jsonl into a readable transcript.md (start to finish)."""

import json
import sys
from pathlib import Path


def _quote(text):
    return "\n".join("> " + line if line.strip() else ">" for line in text.strip().splitlines())


def write_transcript(game_dir):
    game_dir = Path(game_dir)
    meta = json.loads((game_dir / "meta.json").read_text(encoding="utf-8"))
    events = [json.loads(l) for l in (game_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if l]
    out = [f"# Game {meta['id']}", ""]
    out.append(f"- **Seed:** {meta['seed']}")
    out.append(f"- **Status:** {meta['status']} ({meta.get('reason')})")
    out.append(f"- **Winner:** {meta.get('winner') or 'none'}")
    if meta.get("final_vp"):
        out.append("- **Final VP:** " + ", ".join(f"{k} {v}" for k, v in meta["final_vp"].items()))
    out.append(f"- **Estimated API cost:** ${meta['cost_total']:.2f}")
    out.append("")
    out.append("## Players (in seat order)")
    out.append("")
    for s in meta["seats"]:
        c = s["color"]
        out.append(f"- **{c}:** {s['label']} ({s['provider']}, effort {s['effort']}), "
                   f"{meta['calls_by_player'].get(c, 0)} calls, ${meta['cost_by_player'].get(c, 0):.2f}")
    out.append("")
    out.append("## Play-by-play")
    cur_round = None
    for ev in events:
        if ev["round"] != cur_round:
            cur_round = ev["round"]
            out.append("")
            out.append("### Setup" if cur_round == 0 else f"### Round {cur_round}")
            out.append("")
        k = ev["kind"]
        p = ev.get("player")
        if k == "decision":
            d = ev.get("data", {})
            if d.get("fallback"):
                out.append(f"- **{p} (no valid answer, default used)** for: {d.get('decision')}")
                continue
            out.append(f"- **{p} decides:** {ev['text']}")
            if d.get("reason"):
                out.append(f"  - *Stated reason:* {d['reason']}")
            if d.get("thinking"):
                out.append("  - *Thinking summary:*")
                out.append("")
                out.append("\n".join("    " + l for l in _quote(d["thinking"]).splitlines()))
                out.append("")
        elif k == "chat":
            out.append(f"- 💬 **{p}:** \"{ev['text']}\"")
        else:
            line = f"- {ev['text']}"
            if ev.get("detail"):
                line += f" *({ev['detail']})*"
            out.append(line)
    final = events[-1].get("data", {}) if events else {}
    if final.get("notebooks"):
        out.append("")
        out.append("## Final private notebooks")
        for c, nb in final["notebooks"].items():
            out.append("")
            out.append(f"### {c}")
            out.append("")
            out.append(_quote(nb) if nb else "(empty)")
    (game_dir / "transcript.md").write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_transcript(sys.argv[1])
