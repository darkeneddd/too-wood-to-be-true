"""Game loop: asks agents for decisions, runs negotiation, logs everything."""

import json
import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from catanatron.models.enums import ActionType, ActionPrompt, RESOURCES, SETTLEMENT

from .agents import AgentError, make_agent
from .board import BoardInfo
from .engine import GameSession, freq_from_dict, freq_to_text
from .prompts import (
    ACTION_SCHEMA,
    DISCARD_SCHEMA,
    RESPOND_SCHEMA,
    describe_action,
    options_block,
    state_view,
    system_prompt,
)
from catanatron import Color

PROPOSE_LABEL = "Propose a trade to the other players (fill in trade_give and trade_get)"
FATAL_ERRORS = {"AuthenticationError", "PermissionDeniedError", "NotFoundError"}


class Fatal(Exception):
    pass


def _atomic_write(path, text, tries=20):
    """Write via temp file + rename. On Windows the rename fails if the viewer is
    reading the file at that instant, so retry briefly."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


class GameRunner:
    def __init__(self, lineup, seed, out_root="games", max_cost=30.0, max_turns=400,
                 trade_limit=2, stub=False, game_id=None):
        self.seats = lineup["seats"]
        self.colors = [Color[s["color"]] for s in self.seats]
        self.seed = seed
        self.max_cost = max_cost
        self.max_turns = max_turns
        self.trade_limit = trade_limit
        self.stub = stub

        self.session = GameSession(self.colors, seed=seed)
        self.board = BoardInfo(self.session.board_map)
        self.system = system_prompt(self.board.static_text())
        self.agents = {c: make_agent(c, s, stub=stub) for c, s in zip(self.colors, self.seats)}

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.game_id = game_id or f"{stamp}-seed{seed}" + ("-stub" if stub else "")
        self.dir = Path(out_root) / self.game_id
        self.dir.mkdir(parents=True, exist_ok=False)
        (self.dir / "system_prompt.txt").write_text(self.system, encoding="utf-8")

        self.lock = threading.Lock()
        self.seq = 0
        self.call_seq = 0
        self.cost_total = 0.0
        self.cost_by = {c.value: 0.0 for c in self.colors}
        self.calls_by = {c.value: 0 for c in self.colors}
        self.fail_streak = {c: 0 for c in self.colors}
        self.notebooks = {c: "" for c in self.colors}
        self.events_ctx = deque(maxlen=40)  # (public_text, {color: private_text})
        self.chat = deque(maxlen=30)
        self.trades_this_turn = 0
        self.end_turns = 0
        self.last_roll = None
        self.rng = random.Random(seed * 7919)
        self.started = time.time()
        self._write_meta(status="running")

    # ================================================================ logging
    def _write_meta(self, status, **extra):
        meta = {
            "id": self.game_id,
            "created": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
            "seed": self.seed,
            "stub": self.stub,
            "seats": [{"color": s["color"], "provider": "stub" if self.stub else s["provider"],
                       "model": s["model"], "effort": s.get("effort", "medium"),
                       "label": s.get("label") or s["model"]} for s in self.seats],
            "config": {"max_cost": self.max_cost, "max_turns": self.max_turns, "trade_limit": self.trade_limit},
            "board": self.board.to_json(),
            "status": status,
            "cost_total": round(self.cost_total, 4),
            "cost_by_player": {k: round(v, 4) for k, v in self.cost_by.items()},
            "calls_by_player": self.calls_by,
            "events": self.seq,
            **extra,
        }
        _atomic_write(self.dir / "meta.json", json.dumps(meta, indent=1))

    def _status(self, **kw):
        """Cosmetic 'who is thinking' file for the viewer; never allowed to crash the game."""
        kw["updated"] = time.time()
        with self.lock:
            try:
                _atomic_write(self.dir / "status.json", json.dumps(kw), tries=2)
            except OSError:
                pass

    def round_no(self):
        if self.session.state.is_initial_build_phase:
            return 0
        return self.end_turns // len(self.colors) + 1

    def emit(self, kind, text, player=None, private=None, detail=None, data=None, snapshot=True):
        with self.lock:
            self.seq += 1
            ev = {
                "seq": self.seq,
                "ts": round(time.time() - self.started, 2),
                "round": self.round_no(),
                "kind": kind,
                "player": player.value if isinstance(player, Color) else player,
                "text": text,
            }
            if private:
                ev["private"] = {k.value: v for k, v in private.items()}
            if detail:
                ev["detail"] = detail
            if data:
                ev["data"] = data
            if snapshot:
                ev["snapshot"] = self.session.snapshot()
            with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
            if kind in ("action", "trade", "game"):
                self.events_ctx.append((text, private or {}))
            if kind == "chat":
                self.chat.append(f"{ev['player']}: {text}")
            return ev

    def _log_call(self, rec):
        with self.lock:
            self.call_seq += 1
            rec["call_id"] = self.call_seq
            with open(self.dir / "calls.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            return self.call_seq

    # ================================================================ asking
    def _events_for(self, color):
        return [priv.get(color, pub) for pub, priv in self.events_ctx]

    def prepare(self, color, kind, decision, labels=None):
        view = state_view(self.session, self.board, color, self.notebooks[color],
                          self._events_for(color), list(self.chat), self.round_no())
        parts = [view, "", "DECISION: " + decision]
        if labels is not None:
            parts += ["", options_block(labels)]
        parts += ["", REPLY_HINT[kind]]
        return {"color": color, "kind": kind, "decision": decision, "labels": labels,
                "user": "\n".join(parts)}

    def call_with_retries(self, req, validate):
        color = req["color"]
        agent = self.agents[color]
        schema_name, schema = {"action": ("action", ACTION_SCHEMA), "respond": ("respond", RESPOND_SCHEMA),
                               "discard": ("discard", DISCARD_SCHEMA)}[req["kind"]]
        user = req["user"]
        attempts = []
        for attempt in range(2):
            self._status(thinking=color.value, model=agent.model, what=req["kind"], decision=req["decision"])
            rec = {"player": color.value, "model": agent.model, "provider": agent.provider,
                   "kind": req["kind"], "attempt": attempt, "round": self.round_no(), "user_prompt": user}
            try:
                res = agent.call(self.system, user, schema_name, schema, cache_key=f"toowood-{self.game_id}")
            except AgentError as e:
                rec["error"] = str(e)
                attempts.append(self._log_call(rec))
                user = req["user"] + f"\n\nYOUR PREVIOUS REPLY WAS INVALID: {e}. Please answer again."
                continue
            except Exception as e:  # API errors
                name = type(e).__name__
                rec["error"] = f"{name}: {str(e)[:500]}"
                attempts.append(self._log_call(rec))
                if name in FATAL_ERRORS:
                    raise Fatal(f"{color.value} ({agent.model}): {name}: {str(e)[:300]}")
                with self.lock:
                    self.fail_streak[color] += 1
                    if self.fail_streak[color] >= 4:
                        raise Fatal(f"{color.value} ({agent.model}) failed 4 calls in a row: {rec['error']}")
                time.sleep(5)
                continue
            rec.update({"raw": res.raw_text, "thinking": res.thinking, "usage": res.usage,
                        "cost": res.cost, "latency": round(res.latency, 2), "served_model": res.model,
                        "notes": res.notes})
            with self.lock:
                self.cost_total += res.cost
                self.cost_by[color.value] += res.cost
                self.calls_by[color.value] += 1
            err = validate(res.data)
            if err:
                rec["invalid"] = err
                attempts.append(self._log_call(rec))
                user = req["user"] + f"\n\nYOUR PREVIOUS REPLY WAS INVALID: {err}. Please answer again."
                continue
            attempts.append(self._log_call(rec))
            with self.lock:
                self.fail_streak[color] = 0
            return {"data": res.data, "res": res, "call_ids": attempts}
        return {"data": None, "res": None, "call_ids": attempts}

    def finalize(self, req, out, summary_fn):
        color = req["color"]
        data = out["data"]
        res = out["res"]
        if data is None:
            self.emit("decision", f"{color.value} gave no valid answer; a default was used",
                      player=color, snapshot=False,
                      data={"kind": req["kind"], "fallback": True, "call_ids": out["call_ids"],
                            "decision": req["decision"]})
            return None
        if data.get("notebook", "").strip():
            self.notebooks[color] = data["notebook"].strip()
        self.emit("decision", summary_fn(data), player=color, snapshot=False, data={
            "kind": req["kind"],
            "decision": req["decision"],
            "options": req["labels"],
            "reason": data.get("reason", ""),
            "thinking": res.thinking,
            "say": data.get("say", ""),
            "notebook": self.notebooks[color],
            "raw": data,
            "model": self.agents[color].model,
            "cost": round(res.cost, 5),
            "usage": res.usage,
            "latency": round(res.latency, 2),
            "call_ids": out["call_ids"],
        })
        if data.get("say", "").strip():
            self.emit("chat", data["say"].strip(), player=color, snapshot=False)
        return data

    def ask(self, color, kind, decision, labels, validate, summary_fn):
        req = self.prepare(color, kind, decision, labels)
        out = self.call_with_retries(req, validate)
        return self.finalize(req, out, summary_fn)

    # ================================================================ turn logic
    def decision_text(self, color):
        st = self.session.state
        if st.current_prompt == ActionPrompt.BUILD_INITIAL_SETTLEMENT:
            n = len(st.buildings_by_color[color][SETTLEMENT])
            if n == 0:
                return "SETUP: place your FIRST settlement."
            return ("SETUP: place your SECOND settlement. It immediately gives you one card of each "
                    "adjacent resource.")
        if st.current_prompt == ActionPrompt.BUILD_INITIAL_ROAD:
            return "SETUP: place a road touching the settlement you just placed."
        if st.current_prompt == ActionPrompt.MOVE_ROBBER:
            return "Move the robber to a new tile and choose whom to steal from."
        if st.is_road_building:
            return f"Road Building card: place a free road ({st.free_roads_available} left)."
        if not self.session.ps(color, "HAS_ROLLED"):
            return "Start of your turn: roll the dice, or play a Knight first."
        left = self.trade_limit - self.trades_this_turn
        return (f"Your turn; you rolled {self.last_roll}. Choose your next move. You can make several "
                f"moves this turn, one at a time; choose 'End your turn' when finished. "
                f"Trade proposals left this turn: {left}.")

    def step(self):
        s = self.session
        color = s.current_color()
        if s.is_discarding():
            return self.handle_discard(color)
        legal = s.legal_actions()
        labels = [describe_action(a, s, self.board) for a in legal]
        can_trade = s.can_domestic_trade() and self.trades_this_turn < self.trade_limit
        if can_trade:
            labels.append(PROPOSE_LABEL)
        if len(labels) == 1:
            return self.apply(legal[0], auto=True)

        hand = s.hand(color)

        def validate(d):
            c = d.get("choice")
            if not isinstance(c, int) or not (0 <= c < len(labels)):
                return f"choice must be an integer from 0 to {len(labels) - 1}"
            if can_trade and c == len(labels) - 1:
                give, get = freq_from_dict(d["trade_give"]), freq_from_dict(d["trade_get"])
                if sum(give) == 0 or sum(get) == 0:
                    return "a trade proposal needs cards on both sides (trade_give and trade_get)"
                if any(g and w for g, w in zip(give, get)):
                    return "the same resource can't be on both sides of a trade"
                if any(g > h for g, h in zip(give, hand)):
                    return f"you can't offer {freq_to_text(give)}; your hand is {freq_to_text(hand)}"
            return None

        def summary(d):
            c = d["choice"]
            if can_trade and c == len(labels) - 1:
                return (f"proposes a trade: give {freq_to_text(freq_from_dict(d['trade_give']))} "
                        f"for {freq_to_text(freq_from_dict(d['trade_get']))}")
            return labels[c]

        data = self.ask(color, "action", self.decision_text(color), labels, validate, summary)
        if data is None:
            choice = self._default_choice(legal)
        else:
            choice = data["choice"]
            if can_trade and choice == len(labels) - 1:
                return self.run_trade(color, data)
        self.apply(legal[choice])

    def _default_choice(self, legal):
        for want in (ActionType.ROLL, ActionType.END_TURN):
            for i, a in enumerate(legal):
                if a.action_type == want:
                    return i
        return self.rng.randrange(len(legal))

    # ---------------------------------------------------------------- applying actions
    def apply(self, action, auto=False):
        s = self.session
        c = action.color
        before = {col: s.hand(col) for col in self.colors}
        result = s.execute(action)
        after = {col: s.hand(col) for col in self.colors}
        t = result.action_type
        v = result.value
        private, detail = None, None
        cv = c.value
        if t == ActionType.ROLL:
            total = v[0] + v[1]
            self.last_roll = total
            gains = [f"{col.value} +{freq_to_text([a - b for a, b in zip(after[col], before[col])])}"
                     for col in self.colors if after[col] != before[col]]
            text = f"{cv} rolled {total} ({v[0]}+{v[1]})." + (f" Production: {'; '.join(gains)}." if gains
                                                                 else (" Nobody produced." if total != 7 else ""))
        elif t == ActionType.END_TURN:
            text = f"{cv} ended their turn."
            self.end_turns += 1
            self.trades_this_turn = 0
        elif t == ActionType.BUILD_SETTLEMENT:
            text = f"{cv} built a settlement at {self.board.node_summary(v)}."
        elif t == ActionType.BUILD_CITY:
            text = f"{cv} upgraded {self.board.node_summary(v)} to a city."
        elif t == ActionType.BUILD_ROAD:
            text = f"{cv} built a road N{v[0]}-N{v[1]}."
        elif t == ActionType.BUY_DEVELOPMENT_CARD:
            text = f"{cv} bought a development card."
            private = {c: f"You bought a development card: {v}."}
            detail = f"{cv} bought {v}"
        elif t == ActionType.PLAY_KNIGHT_CARD:
            text = f"{cv} played a Knight."
        elif t == ActionType.PLAY_ROAD_BUILDING:
            text = f"{cv} played Road Building."
        elif t == ActionType.PLAY_YEAR_OF_PLENTY:
            text = f"{cv} played Year of Plenty and took {' + '.join(v)}."
        elif t == ActionType.PLAY_MONOPOLY:
            got = after[c][RESOURCES.index(v)] - before[c][RESOURCES.index(v)]
            text = f"{cv} played Monopoly on {v} and collected {got}."
        elif t == ActionType.MARITIME_TRADE:
            give = [r for r in v[:-1] if r is not None]
            text = f"{cv} traded {len(give)} {give[0]} to the bank for 1 {v[-1]}."
        elif t == ActionType.MOVE_ROBBER:
            coord, victim, stolen = v
            tid = self.board.coord_to_tile[coord]
            text = f"{cv} moved the robber to {self.board.tile_label(tid)}"
            if victim is not None:
                text += f" and stole a card from {victim.value}."
                private = {c: f"{cv} (you) moved the robber to {self.board.tile_label(tid)} and stole 1 {stolen} from {victim.value}.",
                           victim: f"{cv} moved the robber to {self.board.tile_label(tid)} and stole 1 {stolen} from you."}
                detail = f"stole 1 {stolen}"
            else:
                text += "."
        elif t == ActionType.DISCARD:
            text = f"{cv} discarded {len(v)} cards."
            private = {c: f"You discarded {freq_to_text([v.count(r) for r in RESOURCES])}."}
            detail = f"discarded {freq_to_text([v.count(r) for r in RESOURCES])}"
        else:
            text = f"{cv} {t.value} {v}"
        self.emit("action", text, player=c, private=private, detail=detail,
                  data={"type": t.value, "auto": auto,
                        "value": json.loads(json.dumps(v, default=lambda o: getattr(o, "value", str(o))))})

    def handle_discard(self, color):
        s = self.session
        need = s.num_cards(color) // 2
        hand = s.hand(color)

        def validate(d):
            freq = freq_from_dict(d["discard"])
            if sum(freq) != need:
                return f"you must discard exactly {need} cards, not {sum(freq)}"
            if any(f > h for f, h in zip(freq, hand)):
                return f"you can't discard {freq_to_text(freq)}; your hand is {freq_to_text(hand)}"
            return None

        data = self.ask(color, "discard",
                        f"A 7 was rolled and you hold {sum(hand)} cards: you must discard exactly {need} cards. "
                        f"Choose which.", None, validate,
                        lambda d: f"discards {freq_to_text(freq_from_dict(d['discard']))}")
        before = s.hand(color)
        if data is None:
            s.random_discard(color)
        else:
            s.discard(color, freq_from_dict(data["discard"]))
        lost = [b - a for a, b in zip(s.hand(color), before)]
        self.emit("action", f"{color.value} discarded {sum(lost)} cards.", player=color,
                  private={color: f"You discarded {freq_to_text(lost)}."},
                  detail=f"discarded {freq_to_text(lost)}", data={"type": "DISCARD", "auto": data is None})

    # ---------------------------------------------------------------- negotiation
    def run_trade(self, proposer, pdata):
        s = self.session
        self.trades_this_turn += 1
        give = freq_from_dict(pdata["trade_give"])
        get = freq_from_dict(pdata["trade_get"])
        pv = proposer.value
        self.emit("trade", f"{pv} offers {freq_to_text(give)} in exchange for {freq_to_text(get)}.",
                  player=proposer, data={"stage": "proposal", "give": give, "get": get}, snapshot=False)

        others = [c for c in self.colors if c != proposer]
        reqs, validators = [], []
        for c in others:
            hand = s.hand(c)
            decision = (f"{pv} (whose turn it is) proposes a trade. If you accept, you give {pv} "
                        f"{freq_to_text(get)} and receive {freq_to_text(give)}. "
                        f"Accept, reject, or counter (a counter is a different deal between you and {pv}; "
                        f"{pv} then picks at most one offer).")

            def validate(d, hand=hand):
                r = d.get("response")
                if r == "accept" and any(g > h for g, h in zip(get, hand)):
                    return f"you can't accept: you don't hold {freq_to_text(get)} (your hand: {freq_to_text(hand)})"
                if r == "counter":
                    yg = freq_from_dict(d["counter_you_give"])
                    yr = freq_from_dict(d["counter_you_get"])
                    if sum(yg) == 0 or sum(yr) == 0:
                        return "a counter needs cards on both sides (counter_you_give and counter_you_get)"
                    if any(a and b for a, b in zip(yg, yr)):
                        return "the same resource can't be on both sides of a counter"
                    if any(g > h for g, h in zip(yg, hand)):
                        return f"you can't offer {freq_to_text(yg)}; your hand is {freq_to_text(hand)}"
                return None

            reqs.append(self.prepare(c, "respond", decision))
            validators.append(validate)

        with ThreadPoolExecutor(max_workers=len(reqs)) as ex:
            outs = list(ex.map(lambda rv: self.call_with_retries(*rv), zip(reqs, validators)))

        offers = []  # (label, partner, proposer_gives, partner_gives)
        for req, out in zip(reqs, outs):
            c = req["color"]

            def summ(d):
                if d["response"] == "counter":
                    return (f"counters: gives {freq_to_text(freq_from_dict(d['counter_you_give']))} "
                            f"for {freq_to_text(freq_from_dict(d['counter_you_get']))}")
                return d["response"] + "s"

            d = self.finalize(req, out, summ)
            resp = d["response"] if d else "reject"
            if resp == "accept":
                self.emit("trade", f"{c.value} accepts {pv}'s offer.", player=c,
                          data={"stage": "response", "response": "accept"}, snapshot=False)
                offers.append((f"Trade with {c.value} as you proposed (you give {freq_to_text(give)}, "
                               f"get {freq_to_text(get)})", c, give, get))
            elif resp == "counter":
                yg = freq_from_dict(d["counter_you_give"])
                yr = freq_from_dict(d["counter_you_get"])
                self.emit("trade", f"{c.value} counters: {c.value} gives {freq_to_text(yg)}, wants {freq_to_text(yr)}.",
                          player=c, data={"stage": "response", "response": "counter", "give": yg, "get": yr},
                          snapshot=False)
                if s.check_trade(proposer, yr, c, yg) is None:
                    offers.append((f"Accept {c.value}'s counter (you give {freq_to_text(yr)}, "
                                   f"get {freq_to_text(yg)})", c, yr, yg))
            else:
                self.emit("trade", f"{c.value} rejects.", player=c,
                          data={"stage": "response", "response": "reject"}, snapshot=False)

        if not offers:
            self.emit("trade", f"No deal for {pv}.", player=proposer, data={"stage": "no_deal"}, snapshot=False)
            return
        labels = [o[0] for o in offers] + ["Don't trade"]
        data = self.ask(proposer, "action", "Responses to your trade proposal are in. Pick one deal, or none.",
                        labels, lambda d: None if isinstance(d.get("choice"), int) and 0 <= d["choice"] < len(labels)
                        else f"choice must be 0 to {len(labels) - 1}", lambda d: labels[d["choice"]])
        choice = data["choice"] if data else len(labels) - 1
        if choice == len(labels) - 1:
            self.emit("trade", f"{pv} declined all offers.", player=proposer, data={"stage": "declined"},
                      snapshot=False)
            return
        _, partner, p_gives, partner_gives = offers[choice]
        s.domestic_trade(proposer, p_gives, partner, partner_gives)
        self.emit("trade", f"TRADE: {pv} gave {freq_to_text(p_gives)} to {partner.value} for "
                           f"{freq_to_text(partner_gives)}.", player=proposer,
                  data={"stage": "done", "partner": partner.value, "gave": p_gives, "got": partner_gives})

    # ================================================================ main loop
    def run(self):
        s = self.session
        seats = ", ".join(f"{c.value}={a.label}" for c, a in self.agents.items())
        self.emit("game", f"Game {self.game_id} starts. Seats in order: {seats}.")
        reason = None
        try:
            while True:
                if s.winner() is not None:
                    reason = "win"
                    break
                if s.state.num_turns >= self.max_turns:
                    reason = f"turn limit ({self.max_turns}) reached"
                    break
                if self.cost_total >= self.max_cost:
                    reason = f"cost cap (${self.max_cost}) reached"
                    break
                self.step()
        except Fatal as e:
            reason = f"aborted: {e}"
        except KeyboardInterrupt:
            reason = "interrupted by user"
        winner = s.winner()
        final_vp = {c.value: s.ps(c, "ACTUAL_VICTORY_POINTS") for c in self.colors}
        text = (f"{winner.value} wins with {final_vp[winner.value]} VP!" if winner else f"Game ended: {reason}.")
        self.emit("game", text, data={"reason": reason, "winner": winner.value if winner else None,
                                      "final_vp": final_vp, "notebooks": {c.value: n for c, n in self.notebooks.items()}})
        self._status(thinking=None, finished=True)
        self._write_meta(status="finished" if winner else "stopped", winner=winner.value if winner else None,
                         reason=reason, final_vp=final_vp, duration_s=round(time.time() - self.started))
        from .transcript import write_transcript
        write_transcript(self.dir)
        return winner, reason


REPLY_HINT = {
    "action": ("Reply in JSON: reason, choice (the option number), say, notebook, and trade_give / trade_get "
               "(all zeros unless your choice is to propose a trade)."),
    "respond": ("Reply in JSON: reason, response (accept / reject / counter), counter_you_give / "
                "counter_you_get (all zeros unless countering), say, notebook."),
    "discard": "Reply in JSON: reason, discard (exact cards to discard), say, notebook.",
}
