"""Talking to Claude, plus a free mock "worker" for testing lines without an API key."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field

from .models import get_model

PROVIDERS = {
    "anthropic": {"key_env": "ANTHROPIC_API_KEY", "base_url": None},
    # OpenRouter's Anthropic-compatible Messages endpoint (POST /api/v1/messages).
    "openrouter": {"key_env": "OPENROUTER_API_KEY", "base_url": "https://openrouter.ai/api"},
}
# Ask OpenRouter to serve Claude from Anthropic itself, with no fallback to other hosts, so
# every arm gets the same model build as the direct API.
OPENROUTER_ROUTING = {"provider": {"order": ["anthropic"], "allow_fallbacks": False}}


class UnsupportedModelError(Exception):
    """The model has no verified ID on this provider. Fatal: stops the line before any request."""

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
    """Thin async wrapper around the Anthropic SDK. Talks to Anthropic directly, or to
    OpenRouter's Anthropic-compatible endpoint with provider="openrouter"."""

    def __init__(self, max_attempts: int = 4, api=None, provider: str = "anthropic", http_client=None):
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider '{provider}' (use {', '.join(PROVIDERS)})")
        self.provider = provider
        if api is not None:  # injected SDK client (tests use one with a fake HTTP transport)
            self.api = api
            return
        from anthropic import AsyncAnthropic  # imported lazily so mock mode needs no key

        extra = {"http_client": http_client} if http_client is not None else {}
        if provider == "openrouter":
            key = os.getenv("OPENROUTER_API_KEY")
            if not key:
                raise SystemExit("OPENROUTER_API_KEY is not set (put it in .env).")
            # auth_token -> "Authorization: Bearer". api_key=None stops the SDK from also sending
            # an ANTHROPIC_API_KEY from the environment as x-api-key to OpenRouter.
            self.api = AsyncAnthropic(api_key=None, auth_token=key, base_url=PROVIDERS["openrouter"]["base_url"],
                                      max_retries=max_attempts, **extra)
        else:
            self.api = AsyncAnthropic(max_retries=max_attempts, **extra)

    def model_id(self, alias: str) -> str:
        m = get_model(alias)
        if self.provider == "openrouter":
            if not m.openrouter_id:
                raise UnsupportedModelError(f"'{alias}' has no verified OpenRouter model ID")
            return m.openrouter_id
        return m.id

    async def call(self, alias: str, system: str, user: str, max_tokens: int,
                   tools: list[str] | None = None, schema: dict | None = None) -> CallResult:
        model_id = self.model_id(alias)
        kwargs = {}
        if tools and "web_search" in tools:
            kwargs["tools"] = [web_search_tool()]
        if self.provider == "openrouter":
            kwargs["extra_body"] = OPENROUTER_ROUTING
        start = time.perf_counter()
        resp = await self.api.messages.create(
            model=model_id,
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
        # OpenRouter reports what it actually charged (usage.cost, USD). Prefer that over our
        # own token arithmetic when it's there.
        billed = getattr(u, "cost", None)
        if billed is None and getattr(u, "model_extra", None):
            billed = u.model_extra.get("cost")
        return CallResult(
            text=text,
            model=alias,
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            web_searches=(getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
            latency_s=latency,
            extra={"stop_reason": getattr(resp, "stop_reason", None),
                   "served_model": getattr(resp, "model", None),
                   "billed_cost_usd": float(billed) if billed is not None else None},
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


def make_client(mock: bool, seed: int | None = None, provider: str = "anthropic"):
    if mock:
        return MockClient(fail_rate=float(os.getenv("MOCK_FAIL_RATE", "0.15")), seed=seed)
    env = PROVIDERS[provider]["key_env"]
    if not os.getenv(env):
        raise SystemExit(f"No {env} found. Put it in .env (see .env.example) or run with --mock.")
    return ClaudeClient(provider=provider)
