"""Model catalog and pricing (USD per million tokens).

Prices from https://platform.claude.com/docs/en/about-claude/pricing (checked Nov 2026).
If Anthropic changes prices, update this file and nothing else.
"""
from dataclasses import dataclass

WEB_SEARCH_USD_PER_SEARCH = 10 / 1000  # $10 per 1,000 searches
HAIKU_LONG_PROMPT_THRESHOLD = 100_000


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write_5m: float
    cache_read: float


@dataclass(frozen=True)
class Model:
    alias: str
    id: str
    price: Price
    long_price: Price | None = None  # Haiku 5.5 charges more for prompts > 100K tokens


MODELS: dict[str, Model] = {
    "haiku": Model(
        "haiku", "claude-haiku-5-5",
        Price(0.10, 0.50, 0.125, 0.01),
        long_price=Price(0.50, 2.50, 0.625, 0.05),
    ),
    "sonnet": Model("sonnet", "claude-sonnet-5-5", Price(2.00, 10.00, 2.50, 0.10)),
    "opus": Model("opus", "claude-opus-5-5", Price(4.00, 20.00, 5.00, 0.20)),
    "fable": Model("fable", "claude-fable-5-2", Price(8.00, 40.00, 10.00, 0.20)),
}

# Cheapest to most capable. Escalation walks up this ladder.
LADDER = ["haiku", "sonnet", "opus", "fable"]


def get_model(alias: str) -> Model:
    try:
        return MODELS[alias]
    except KeyError:
        raise ValueError(f"Unknown model '{alias}'. Use one of: {', '.join(MODELS)}") from None


def next_up(alias: str) -> str | None:
    """The next model up the ladder, or None if already at the top."""
    i = LADDER.index(alias)
    return LADDER[i + 1] if i + 1 < len(LADDER) else None


def cost_usd(alias: str, input_tokens: int, output_tokens: int,
             cache_write: int = 0, cache_read: int = 0, web_searches: int = 0) -> float:
    m = get_model(alias)
    prompt_len = input_tokens + cache_write + cache_read
    p = m.long_price if (m.long_price and prompt_len > HAIKU_LONG_PROMPT_THRESHOLD) else m.price
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_write * p.cache_write_5m
        + cache_read * p.cache_read
    ) / 1_000_000 + web_searches * WEB_SEARCH_USD_PER_SEARCH
