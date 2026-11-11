"""Talking to Claude, plus a free mock "worker" for testing lines without an API key."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field

from .models import get_model

# If the API rejects this tool version, set WEB_SEARCH_TOOL_VERSION in .env to the
# current one from https://platform.claude.com/docs (web search tool page).
# Built at call time (not import time) so values from .env are honored.
def web_search_tool() -> dict:
    return {
        "type": os.getenv("WEB_SEARCH_TOOL_VERSION", "web_search_20260209"),
        "name": "web_search",
        "max_uses": int(os.getenv("WEB_SEARCH_MAX_USES", "3")),
    }


@dataclass
class CallResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    web_searches: int = 0
    latency_s: float = 0.0
    extra: dict = field(default_factory=dict)


class ClaudeClient:
    """Thin async wrapper around the Anthropic SDK."""

    def __init__(self, max_attempts: int = 4, api=None):
        if api is not None:  # injected SDK client (tests use one with a fake HTTP transport)
            self.api = api
            return
        from anthropic import AsyncAnthropic  # imported lazily so mock mode needs no key

        self.api = AsyncAnthropic(max_retries=max_attempts)

    async def call(self, alias: str, system: str, user: str, max_tokens: int,
                   tools: list[str] | None = None, schema: dict | None = None) -> CallResult:
        model = get_model(alias)
        kwargs = {}
        if tools and "web_search" in tools:
            kwargs["tools"] = [web_search_tool()]
        start = time.perf_counter()
        resp = await self.api.messages.create(
            model=model.id,
            max_tokens=max_tokens,
            # Cache the station's instructions: every item on the line reuses them.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            **kwargs,
        )
        latency = time.perf_counter() - start
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        u = resp.usage
        stu = getattr(u, "server_tool_use", None)
        return CallResult(
            text=text,
            model=alias,
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            web_searches=(getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
            latency_s=latency,
            extra={"stop_reason": getattr(resp, "stop_reason", None)},
        )


class MockClient:
    """Fake worker: returns schema-shaped JSON instantly, costs nothing.

    `fail_rate` makes some outputs malformed so you can watch retries,
    escalation and the andon cord (line stop) actually happen.
    """

    def __init__(self, fail_rate: float = 0.1, seed: int | None = None):
        self.fail_rate = fail_rate
        self.seed = seed
        self.rng = random.Random(seed)

    def _rng_for(self, alias: str, system: str, user: str) -> random.Random:
        # With a seed, each call's randomness depends only on its own content, so
        # results don't change with the order concurrent calls happen to finish in.
        if self.seed is None:
            return self.rng
        return random.Random(f"{self.seed}|{alias}|{system}|{user}")

    async def call(self, alias, system, user, max_tokens, tools=None, schema=None) -> CallResult:
        rng = self._rng_for(alias, system, user)
        await asyncio.sleep(0.01)
        # Bigger models fail less in the mock, so escalation visibly helps.
        rank = {"haiku": 1.0, "sonnet": 0.5, "opus": 0.25, "fable": 0.1}[alias]
        if rng.random() < self.fail_rate * rank:
            text = "Sorry, I couldn't format that properly."
        else:
            text = json.dumps(fake_from_schema(schema or {"type": "object"}, rng))
        in_tok = (len(system) + len(user)) // 4
        return CallResult(text=text, model=alias, input_tokens=in_tok,
                          output_tokens=len(text) // 4, latency_s=0.01)


def fake_from_schema(schema: dict, rng: random.Random):
    t = schema.get("type", "string")
    if "enum" in schema:
        return rng.choice(schema["enum"])
    if t == "object":
        props = schema.get("properties", {})
        return {k: fake_from_schema(v, rng) for k, v in props.items()}
    if t == "array":
        return [fake_from_schema(schema.get("items", {}), rng) for _ in range(2)]
    if t == "integer":
        lo, hi = schema.get("minimum", 0), schema.get("maximum", 100)
        return rng.randint(lo, hi)
    if t == "number":
        return round(rng.uniform(schema.get("minimum", 0), schema.get("maximum", 1)), 2)
    if t == "boolean":
        # Inspector mock: mostly passes
        return rng.random() < 0.85
    return "mock text"


def make_client(mock: bool, seed: int | None = None):
    if mock:
        return MockClient(fail_rate=float(os.getenv("MOCK_FAIL_RATE", "0.15")), seed=seed)
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "No ANTHROPIC_API_KEY found. Put it in .env (see .env.example) or run with --mock."
        )
    return ClaudeClient()
