"""Model-backed players. Each call is stateless: system prompt (rules + board) +
one user message (current view + options). Continuity comes from the notebook."""

import json
import random
import time
from dataclasses import dataclass, field

# USD per 1M tokens: input, output, cache_read, cache_write.
# Sources: Anthropic prices from the claude-api skill table (cached 2026-10-06);
# OpenAI input/output from developers.openai.com/api/docs/models (2026-11-09).
# Values marked "est" are guesses (cache pricing not confirmed) - cost totals are
# estimates; the provider dashboards are the source of truth.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50),   # cache_write est (1.25x)
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),      # cache_write est (1.25x)
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),    # cache_write est (1.25x)
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),     # cache_read/write est
    "gpt-6-astra": (10.00, 50.00, 1.00, 10.00),        # cached input est (10%)
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.10),            # cached input est (10%)
    "stub": (0, 0, 0, 0),
}


def price_of(model, uncached_in, cached_in, cache_write, out):
    p_in, p_out, p_cr, p_cw = PRICES.get(model, (0, 0, 0, 0))
    return (uncached_in * p_in + cached_in * p_cr + cache_write * p_cw + out * p_out) / 1e6


class AgentError(Exception):
    pass


@dataclass
class CallResult:
    data: dict
    raw_text: str
    thinking: str
    usage: dict
    cost: float
    latency: float
    model: str
    notes: list = field(default_factory=list)


class Agent:
    provider = "base"

    def __init__(self, color, model, effort="medium", label=None):
        self.color = color
        self.model = model
        self.effort = effort
        self.label = label or model

    def call(self, system, user, schema_name, schema, cache_key):
        raise NotImplementedError


class ClaudeAgent(Agent):
    provider = "anthropic"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=4, timeout=600)

    def call(self, system, user, schema_name, schema, cache_key):
        t0 = time.time()
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        latency = time.time() - t0
        u = resp.usage
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        usage = {"input": u.input_tokens, "cache_read": cr, "cache_write": cw, "output": u.output_tokens}
        cost = price_of(self.model, u.input_tokens, cr, cw, u.output_tokens)
        thinking = "\n\n".join(b.thinking for b in resp.content if b.type == "thinking" and b.thinking)
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise AgentError(f"model refused (category={cat})", ) from None
        if resp.stop_reason == "max_tokens":
            raise AgentError("hit max_tokens before finishing")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise AgentError(f"invalid JSON: {e}: {text[:300]}")
        return CallResult(data, text, thinking, usage, cost, latency, resp.model,
                          notes=[f"request_id={resp._request_id}"])


class OpenAIAgent(Agent):
    provider = "openai"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        import openai

        self._openai = openai
        self.client = openai.OpenAI(max_retries=4, timeout=600)
        self.summaries = True  # turned off if the org isn't allowed reasoning summaries

    def _create(self, system, user, schema_name, schema, cache_key):
        reasoning = {"effort": self.effort}
        if self.summaries:
            reasoning["summary"] = "auto"
        return self.client.responses.create(
            model=self.model,
            instructions=system,
            input=user,
            reasoning=reasoning,
            text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
            max_output_tokens=16000,
            prompt_cache_key=cache_key,
            store=False,
        )

    def call(self, system, user, schema_name, schema, cache_key):
        notes = []
        t0 = time.time()
        try:
            resp = self._create(system, user, schema_name, schema, cache_key)
        except self._openai.BadRequestError as e:
            msg = str(e).lower()
            if self.summaries and ("summary" in msg or "verif" in msg):
                self.summaries = False
                notes.append("reasoning summaries rejected for this org; disabled: " + str(e)[:200])
                resp = self._create(system, user, schema_name, schema, cache_key)
            else:
                raise
        latency = time.time() - t0
        u = resp.usage
        cached = (u.input_tokens_details.cached_tokens or 0) if u.input_tokens_details else 0
        usage = {"input": u.input_tokens - cached, "cache_read": cached, "cache_write": 0,
                 "output": u.output_tokens,
                 "reasoning": (u.output_tokens_details.reasoning_tokens if u.output_tokens_details else None)}
        cost = price_of(self.model, u.input_tokens - cached, cached, 0, u.output_tokens)
        thinking_parts = []
        for item in resp.output:
            if item.type == "reasoning":
                for s in item.summary or []:
                    thinking_parts.append(s.text)
        if resp.status != "completed":
            raise AgentError(f"response status {resp.status}: {resp.incomplete_details}")
        text = resp.output_text
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise AgentError(f"invalid JSON: {e}: {text[:300]}")
        return CallResult(data, text, "\n\n".join(thinking_parts), usage, cost, latency, resp.model,
                          notes=notes + [f"response_id={resp.id}"])


class StubAgent(Agent):
    """Free random player for testing the pipeline (no API calls)."""

    provider = "stub"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rng = random.Random(hash(self.color) & 0xFFFF)

    def call(self, system, user, schema_name, schema, cache_key):
        r = self.rng
        zero = {k: 0 for k in ["wood", "brick", "sheep", "wheat", "ore"]}
        import re
        opts = re.findall(r"^  \d+: (.*)$", user.split("OPTIONS:\n", 1)[1], re.M) if "OPTIONS:" in user else []
        if schema_name == "action":
            propose_idx = next((i for i, l in enumerate(opts) if "Propose a trade" in l), None)
            if propose_idx is not None and r.random() < 0.3:
                give, get = dict(zero), dict(zero)
                give[r.choice(list(zero))] = 1
                get[r.choice([k for k in zero if give[k] == 0])] = 1
                data = {"reason": "stub: propose", "choice": propose_idx, "say": "Anyone want a trade?",
                        "notebook": "", "trade_give": give, "trade_get": get}
            else:
                data = {"reason": "stub: random", "choice": r.randrange(max(1, len(opts))), "say": "",
                        "notebook": "", "trade_give": zero, "trade_get": zero}
        elif schema_name == "respond":
            data = {"reason": "stub", "response": r.choice(["accept", "reject", "reject"]),
                    "counter_you_give": zero, "counter_you_get": zero, "say": "", "notebook": ""}
        else:  # discard: parse "must discard N" and hand line
            need = int(re.search(r"discard exactly (\d+)", user).group(1))
            hand_line = re.search(r"YOUR HAND: (.*?)  \(", user).group(1)
            pool = []
            for part in hand_line.split(", "):
                n, res = part.split(" ")
                pool += [res.lower()] * int(n)
            r.shuffle(pool)
            d = dict(zero)
            for res in pool[:need]:
                d[res] += 1
            data = {"reason": "stub", "discard": d, "say": "", "notebook": ""}
        return CallResult(data, json.dumps(data), "", {"input": 0, "output": 0}, 0.0, 0.0, "stub")


def make_agent(color, seat_cfg, stub=False):
    if stub:
        return StubAgent(color, "stub", label=f"stub ({seat_cfg['model']})")
    provider = seat_cfg["provider"]
    cls = {"anthropic": ClaudeAgent, "openai": OpenAIAgent}[provider]
    return cls(color, seat_cfg["model"], effort=seat_cfg.get("effort", "medium"), label=seat_cfg.get("label"))
