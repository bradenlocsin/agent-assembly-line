"""The time-and-motion study: every model call gets logged with cost and latency."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import cost_usd


@dataclass
class Entry:
    item_id: str
    station: str
    model: str
    attempt: int
    ok: bool
    input_tokens: int
    output_tokens: int
    cache_write: int
    cache_read: int
    web_searches: int
    cost_usd: float
    latency_s: float
    escalated: bool = False
    error: str | None = None


class Ledger:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.entries: list[Entry] = []
        self._fh = open(run_dir / "ledger.jsonl", "a")

    @property
    def spent(self) -> float:
        return sum(e.cost_usd for e in self.entries)

    def record(self, item_id, station, result, attempt, ok, escalated=False, error=None) -> Entry:
        e = Entry(
            item_id=item_id, station=station, model=result.model, attempt=attempt, ok=ok,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            cache_write=result.cache_write, cache_read=result.cache_read,
            web_searches=result.web_searches,
            cost_usd=cost_usd(result.model, result.input_tokens, result.output_tokens,
                              result.cache_write, result.cache_read, result.web_searches),
            latency_s=round(result.latency_s, 3), escalated=escalated, error=error,
        )
        self.entries.append(e)
        self._fh.write(json.dumps(asdict(e)) + "\n")
        self._fh.flush()
        return e

    def close(self):
        self._fh.close()


def load_entries(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "ledger.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def build_report(run_dir: Path, summary: dict | None = None) -> str:
    entries = load_entries(run_dir)
    if summary is None:
        sp = Path(run_dir) / "summary.json"
        summary = json.loads(sp.read_text()) if sp.exists() else {}

    by_station: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    order: list[str] = []
    for e in entries:
        s = by_station[e["station"]]
        if e["station"] not in order:
            order.append(e["station"])
        s["calls"] += 1
        s["failed"] += 0 if e["ok"] else 1
        s["escalated"] += 1 if e.get("escalated") else 0
        s["in"] += e["input_tokens"] + e["cache_read"] + e["cache_write"]
        s["out"] += e["output_tokens"]
        s["cost"] += e["cost_usd"]
        s["latency"] += e["latency_s"]

    total = sum(e["cost_usd"] for e in entries)
    finished = summary.get("finished", 0)
    lines = [
        f"# Run report: {summary.get('line', '?')}",
        "",
        f"- Items in: **{summary.get('items_in', '?')}**  |  finished: **{finished}**  |  "
        f"defective (pulled off line): **{summary.get('defective', '?')}**",
        f"- Total cost: **${total:.4f}**"
        + (f"  |  cost per finished item: **${total / finished:.5f}**" if finished else ""),
        f"- Line stopped: **{summary.get('stopped_reason') or 'no'}**",
    ]
    if summary.get("inspected"):
        lines.append(
            f"- Inspector: {summary['inspected']} inspected, {summary['inspection_failures']} failed "
            f"({summary['inspection_failures'] / summary['inspected']:.0%} defect rate)"
        )
    lines += [
        "",
        "## Time-and-motion by station",
        "",
        "| Station | Calls | Failed | Escalated | Tokens in | Tokens out | Cost | Avg latency |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in order:
        s = by_station[name]
        lines.append(
            f"| {name} | {int(s['calls'])} | {int(s['failed'])} | {int(s['escalated'])} | "
            f"{int(s['in']):,} | {int(s['out']):,} | ${s['cost']:.4f} | {s['latency'] / s['calls']:.2f}s |"
        )
    lines += ["", "Biggest cost first is where to optimize. High 'Failed' means that station's prompt needs kaizen."]
    return "\n".join(lines)

