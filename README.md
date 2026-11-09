# too-wood-to-be-true

AI agents (Claude and OpenAI models) play Catan against each other, with free-form trade negotiation and public table talk. Every decision is logged with the agent's stated reason and the provider's thinking summary, and a local viewer lets you watch live or replay any game step by step.

## Setup

1. Create `.env` in this folder:
   - `ANTHROPIC_API_KEY=...`
   - `OPENAI_API_KEY=sk-...`
   - `OPENAI_ORG_ID=org-...` (optional; picks which org is billed)
2. Dependencies are already in `.venv`. To recreate: `py -m venv .venv` then `.venv\Scripts\python.exe -m pip install -r requirements.txt`

## Running

- **Real game, with the viewer:** `.venv\Scripts\python.exe run_game.py --seed 1 --serve`, then open http://localhost:8765
- **Free test game (random stand-in players, no API calls):** `.venv\Scripts\python.exe run_game.py --stub`
- **Viewer only (replay old games):** `.venv\Scripts\python.exe serve.py`

Useful flags:

- **`--max-cost 30`:** stop the game when estimated spend reaches this many dollars
- **`--rotate N`:** shift the seat order by N (for fair seat-rotation batches)
- **`--trade-limit 2`:** trade proposals each player may make per turn
- **`--lineup path.json`:** a different set of models (see `config/lineup.json`)

## What gets saved (`games/<game id>/`)

- **`events.jsonl`:** every game event, decision and chat line, each with a full board snapshot. This is the source of truth for the viewer.
- **`calls.jsonl`:** every API call: the exact prompt, the raw reply, the thinking summary, tokens, cost and latency.
- **`system_prompt.txt`:** the rules and board briefing that every agent received.
- **`transcript.md`:** a readable start-to-finish account, generated at the end. Regenerate it with `.venv\Scripts\python.exe -m toowood.transcript games\<id>`.
- **`meta.json`:** the players, the board, the result and the cost totals.

## Design notes

- **Engine:** [Catanatron](https://github.com/bcollazo/catanatron) handles the rules. `toowood/engine.py` adds player-to-player trading (which Catanatron lacks), player-chosen discards, and the rule that a dev card can't be played on the turn it was bought.
- **Stateless calls:** each decision is one API call made up of the rules and board (cached) plus the current view and a numbered list of legal options. Agents keep continuity through a private notebook they can rewrite on every decision.
- **What agents see:** only what a real player would see (their own hand and dev cards, public counts for everyone else). The viewer is omniscient.
- **Identities hidden:** agents only know each other as colours, never as model names.
- **Forced moves are automatic:** if there's only one legal option, no model is called.
- **Trading:** the active player proposes a trade; the other three answer in parallel (accept / reject / counter) without seeing each other's answers; the proposer then picks at most one.
- **Bad replies:** an invalid or failed reply is retried once with the error message. If that also fails, a safe default is used and the fallback is logged. Four API failures in a row for one player abort the game.
- **Costs are estimates.** Some cache prices in `toowood/agents.py` are guesses; the provider dashboards are the source of truth.
