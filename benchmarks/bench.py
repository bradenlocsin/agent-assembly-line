"""Reproducible benchmark: assembly line vs. a single model doing the whole job.

    python benchmarks/bench.py               # full run, writes benchmarks/results/
    python benchmarks/bench.py --quick       # smaller, for a fast sanity check

THIS IS A SIMULATION. No API calls are made. Model behavior (failure rates,
token counts, latency) comes from benchmarks/assumptions.json, and every
conclusion inherits those assumptions. What the benchmark does measure for real:
  - the engine's own overhead (scheduling, validation, ledger) per call
  - how retries, escalation and the budget/andon logic behave under failure
  - the cost arithmetic, given the token counts it is fed
What it cannot measure: output quality, real failure rates, real token usage.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import math
import random
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assembly_line.client import CallResult, fake_from_schema  # noqa: E402
from assembly_line.config import LineSpec, StationSpec, load_line  # noqa: E402
from assembly_line.line import Line  # noqa: E402
from assembly_line.models import next_up  # noqa: E402

LEAD_GEN = ROOT / "lines" / "lead_gen"
SUBTASKS = ["enrich", "score", "draft"]


def task_of(schema: dict) -> str:
    props = schema.get("properties", {})
    if all(t in props for t in SUBTASKS):
        return "combined"
    for key, task in [("what_they_do", "enrich"), ("fit_score", "score"),
                      ("subject", "draft"), ("pass", "inspector")]:
        if key in props:
            return task
    return "unknown"


class SimClient:
    """A fake model whose behavior is driven entirely by assumptions.json."""

    def __init__(self, a: dict, seed: int, time_scale: float = 0.0):
        self.a, self.seed, self.time_scale = a, seed, time_scale

    def fail_prob(self, alias: str, task: str) -> float:
        base = self.a["schema_fail_rate"][alias]
        diff = self.a["task_difficulty"]
        if task == "combined":
            ok = math.prod(1 - min(1.0, base * diff[t]) for t in SUBTASKS)
            return min(1.0, (1 - ok) * self.a["combined_task_penalty"])
        return min(1.0, base * diff.get(task, 1.0))

    def out_tokens(self, alias: str, task: str) -> int:
        o = self.a["output_tokens"]
        visible = sum(o[t] for t in SUBTASKS) + o["combined_overhead"] if task == "combined" else o.get(task, 100)
        return visible + self.a["thinking_tokens"][alias]

    async def call(self, alias, system, user, max_tokens, tools=None, schema=None) -> CallResult:
        rng = random.Random(f"{self.seed}|{alias}|{system}|{user}")
        task = task_of(schema or {})
        ws = self.a["web_search"]
        searches = ws["searches_per_call"] if tools and "web_search" in tools else 0
        in_tok = (len(system) + len(user)) // 4 + searches * ws["tokens_per_search"]
        out_tok = self.out_tokens(alias, task)
        lat = self.a["latency"]
        latency = lat["ttft_s"][alias] + out_tok / lat["tokens_per_s"][alias] + searches * ws["latency_s_per_search"]
        if self.time_scale:
            await asyncio.sleep(latency * self.time_scale)
        if rng.random() < self.fail_prob(alias, task):
            text = "I wasn't able to produce that in the requested format."
        else:
            text = json.dumps(fake_from_schema(schema or {"type": "object"}, rng))
        return CallResult(text=text, model=alias, input_tokens=in_tok, output_tokens=out_tok,
                          web_searches=searches, latency_s=latency)


# ---------- workflows ----------

def assembly_line(escalate=True, inspector=False) -> LineSpec:
    spec = load_line(LEAD_GEN)
    if not escalate:
        spec.stations = [replace(s, escalate_to=None) for s in spec.stations]
    if not inspector:
        spec.inspector = None
    spec.name = "line" + ("" if escalate else "_noesc") + ("_insp" if inspector else "")
    return spec


def single_model(model: str, inspector=False) -> LineSpec:
    """One call does research + scoring + drafting, same instructions, same schemas."""
    base = load_line(LEAD_GEN)
    instr = ["You do the whole job in one pass, in three parts. Put each part's result under its key."]
    for s in base.stations:
        instr.append(f"# Part '{s.name}'\n{s.instructions}")
    schema = {"type": "object", "properties": {s.name: s.output_schema for s in base.stations},
              "required": [s.name for s in base.stations]}
    st = StationSpec(name="all_in_one", model=model, escalate_to=next_up(model), max_retries=1,
                     max_tokens=4000, tools=["web_search"], instructions="\n\n".join(instr),
                     output_schema=schema)
    return LineSpec(name=f"single_{model}" + ("_insp" if inspector else ""), path=LEAD_GEN,
                    stations=[st], inspector=base.inspector if inspector else None,
                    concurrency=base.concurrency, budget_usd=1e9)


def run_once(spec: LineSpec, a: dict, items: list[dict], seed: int, time_scale: float) -> dict:
    spec.budget_usd = 1e9
    if spec.inspector:  # measure the inspector's cost, don't let a simulated verdict stop the line
        spec.inspector.stop_line_if_defect_rate_above = 1.01
    tmp = Path(tempfile.mkdtemp())
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        rd = asyncio.run(Line(spec, SimClient(a, seed, time_scale), runs_root=tmp, seed=seed).run(items))
    wall = time.perf_counter() - t0
    s = json.loads((rd / "summary.json").read_text())
    led = [json.loads(l) for l in (rd / "ledger.jsonl").read_text().splitlines()]
    finished_ids = {json.loads(l)["id"] for l in (rd / "output.jsonl").read_text().splitlines()}
    hiccup_ids = {e["item_id"] for e in led if not e["ok"]}
    n = len(items)
    by_station = {}
    search_fees = 0.0
    for e in led:
        fee = e["web_searches"] * 0.01
        search_fees += fee
        by_station[e["station"]] = by_station.get(e["station"], 0.0) + e["cost_usd"] - fee
    run_once.last_breakdown = {"web search fees": search_fees, **{f"{k} (tokens)": v for k, v in by_station.items()}}
    return {
        "success": s["finished"] / n,
        "cost": s["total_cost_usd"],
        "cost_per_finished": s["total_cost_usd"] / s["finished"] if s["finished"] else float("nan"),
        "calls_per_item": len(led) / n,
        "failed_calls": sum(not e["ok"] for e in led),
        "escalations": sum(e["escalated"] for e in led),
        "recovered": len(hiccup_ids & finished_ids),
        "had_failure": len(hiccup_ids),
        "sim_wall_s": wall / time_scale if time_scale else float("nan"),
    }


def summarize(runs: list[dict]) -> dict:
    out = {}
    for k in runs[0]:
        vals = [r[k] for r in runs if not (isinstance(r[k], float) and math.isnan(r[k]))]
        out[k] = (statistics.mean(vals), statistics.stdev(vals) if len(vals) > 1 else 0.0) if vals else (float("nan"), 0)
    return out


def engine_overhead(n_items: int) -> dict:
    """Real measurement: engine time per call with a zero-latency fake model."""
    a = json.loads((Path(__file__).parent / "assumptions.json").read_text())
    items = [{"id": str(i), "name": f"Biz {i}", "city": "Lehi UT"} for i in range(n_items)]
    spec = assembly_line()
    spec.concurrency = 50
    spec.budget_usd = 1e9
    tmp = Path(tempfile.mkdtemp())
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        rd = asyncio.run(Line(spec, SimClient(a, 0, 0.0), runs_root=tmp, seed=0).run(items))
    wall = time.perf_counter() - t0
    calls = len((rd / "ledger.jsonl").read_text().splitlines())
    return {"items": n_items, "calls": calls, "wall_s": wall, "ms_per_call": 1000 * wall / calls}


def fmt(m, sd, pct=False, money=False, digits=2):
    if isinstance(m, float) and math.isnan(m):
        return "n/a"
    if pct:
        return f"{m:.1%} ± {sd:.1%}"
    if money:
        return f"${m:.4f} ± {sd:.4f}"
    return f"{m:.{digits}f} ± {sd:.{digits}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=200)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--time-scale", type=float, default=0.001,
                    help="simulated seconds are slept as seconds*scale, then scaled back up")
    args = ap.parse_args()
    if args.quick:
        args.items, args.seeds = 40, 2

    a = json.loads((Path(__file__).parent / "assumptions.json").read_text())
    items = [{"id": str(i), "name": f"Example Business {i}", "category": "barbershop", "city": "Lehi UT"}
             for i in range(args.items)]
    seeds = list(range(args.seeds))

    workflows = {
        "Assembly line (haiku→haiku→sonnet, escalation on)": lambda: assembly_line(),
        "Assembly line, escalation OFF": lambda: assembly_line(escalate=False),
        "Single model: haiku (escalates to sonnet)": lambda: single_model("haiku"),
        "Single model: sonnet (escalates to opus)": lambda: single_model("sonnet"),
        "Single model: opus (escalates to fable)": lambda: single_model("opus"),
    }

    lines = []
    P = lines.append
    P("# Benchmark: assembly line vs. single model (SIMULATED)")
    P("")
    P("> **Read this first.** No real model was called. Failure rates, token counts and latencies come from "
      "`benchmarks/assumptions.json`, and those numbers are guesses. These results show what the framework's "
      "logic does *given* those assumptions. They are not evidence about real Claude performance or output quality.")
    P("")
    P(f"Items per run: {args.items} · seeds: {args.seeds} · values are mean ± stdev across seeds · "
      f"task: lead_gen (research with web search → score → draft) · inspector off for a like-for-like pipeline comparison.")
    P("")

    print("Engine overhead…", file=sys.stderr)
    ov = engine_overhead(2000 if not args.quick else 300)
    P("## 1. Engine overhead (real measurement)")
    P("")
    P(f"With a zero-latency fake model: {ov['calls']:,} calls for {ov['items']:,} items in {ov['wall_s']:.2f}s → "
      f"**{ov['ms_per_call']:.2f} ms of engine time per model call** (validation, scheduling, ledger writes). "
      "Real API calls take roughly 1–30 s, so engine overhead is negligible next to model latency.")
    P("")

    P("## 2. Workflow comparison (simulated)")
    P("")
    P("| Workflow | Success | Cost / finished item | Calls / item | Failed calls | Items recovered after a failure | Sim. wall time (s) |")
    P("|---|---|---|---|---|---|---|")
    results = {}
    for name, make in workflows.items():
        print(f"Running {name}…", file=sys.stderr)
        runs = [run_once(make(), a, items, s, args.time_scale) for s in seeds]
        sm = summarize(runs)
        results[name] = sm
        P(f"| {name} | {fmt(*sm['success'], pct=True)} | {fmt(*sm['cost_per_finished'], money=True)} | "
          f"{fmt(*sm['calls_per_item'])} | {fmt(*sm['failed_calls'], digits=1)} | "
          f"{fmt(*sm['recovered'], digits=1)} / {sm['had_failure'][0]:.1f} | {fmt(*sm['sim_wall_s'], digits=0)} |")
    P("")
    P("Wall time is simulated: modeled latency is slept at a reduced scale with the line's real concurrency (5), "
      "then scaled back up. It reflects the latency *assumptions*, not measured API speed.")
    P("")

    # Inspector overhead
    print("Inspector overhead…", file=sys.stderr)
    with_insp = summarize([run_once(assembly_line(inspector=True), a, items, s, 0) for s in seeds])
    base_cost = results["Assembly line (haiku→haiku→sonnet, escalation on)"]["cost"][0]
    P("## 3. Where the money goes, and what the inspector costs (simulated)")
    P("")
    run_once(assembly_line(), a, items, 0, 0)
    bd = run_once.last_breakdown
    total = sum(bd.values())
    P("Assembly line cost breakdown (seed 0):")
    P("")
    P("| Component | Cost | Share |")
    P("|---|---|---|")
    for k, v in sorted(bd.items(), key=lambda kv: -kv[1]):
        P(f"| {k} | ${v:.3f} | {v / total:.0%} |")
    P("")
    P(f"Adding the Opus inspector at a 25% sample rate raises total line cost from ${base_cost:.3f} to "
      f"${with_insp['cost'][0]:.3f} per {args.items} items (**{with_insp['cost'][0] / base_cost - 1:+.0%}**). "
      "(The line-stop threshold was disabled for this measurement so a simulated verdict couldn't halt the run.) "
      "Whether that buys anything depends on how often it catches real problems, which can't be simulated.")
    P("")

    # Sensitivity
    print("Sensitivity sweeps…", file=sys.stderr)
    sweep_items = items[: max(60, args.items // 2)]
    sweep_seeds = seeds[:3]

    def compare(mod):
        aa = json.loads(json.dumps(a))
        mod(aa)
        line_r = summarize([run_once(assembly_line(), aa, sweep_items, s, 0) for s in sweep_seeds])
        son_r = summarize([run_once(single_model("sonnet"), aa, sweep_items, s, 0) for s in sweep_seeds])
        return line_r, son_r

    P("## 4. Sensitivity: when does the conclusion flip? (simulated)")
    P("")
    P("Assembly line vs. single Sonnet, varying one assumption at a time.")
    P("")
    P("### 4a. Haiku schema-failure rate")
    P("")
    P("| Haiku fail rate | Line cost/item | Sonnet cost/item | Line ÷ Sonnet | Line success | Sonnet success |")
    P("|---|---|---|---|---|---|")
    for r in [0.02, 0.08, 0.2, 0.4, 0.6]:
        lr, sr = compare(lambda aa: aa["schema_fail_rate"].__setitem__("haiku", r))
        P(f"| {r:.0%} | ${lr['cost_per_finished'][0]:.4f} | ${sr['cost_per_finished'][0]:.4f} | "
          f"{lr['cost_per_finished'][0] / sr['cost_per_finished'][0]:.2f}× | {lr['success'][0]:.1%} | {sr['success'][0]:.1%} |")
    P("")
    P("### 4b. Tokens read per web search (the biggest cost driver)")
    P("")
    P("| Tokens per search | Line cost/item | Sonnet cost/item | Line ÷ Sonnet |")
    P("|---|---|---|---|")
    for t in [0, 1000, 6000, 20000]:
        lr, sr = compare(lambda aa: aa["web_search"].__setitem__("tokens_per_search", t))
        P(f"| {t:,} | ${lr['cost_per_finished'][0]:.4f} | ${sr['cost_per_finished'][0]:.4f} | "
          f"{lr['cost_per_finished'][0] / sr['cost_per_finished'][0]:.2f}× |")
    P("")
    P("### 4c. Combined-task penalty (do long multi-part prompts fail more than their parts?)")
    P("")
    P("| Penalty | Line success | Sonnet success | Sonnet cost/item |")
    P("|---|---|---|---|")
    for pen in [1.0, 2.0, 4.0]:
        lr, sr = compare(lambda aa: aa.__setitem__("combined_task_penalty", pen))
        P(f"| {pen:.0f}× | {lr['success'][0]:.1%} | {sr['success'][0]:.1%} | ${sr['cost_per_finished'][0]:.4f} |")
    P("")
    P("## 5. What this does and doesn't show")
    P("")
    P("- **Measured for real:** engine overhead; that retries, escalation and budget logic behave as designed under injected failures; the cost arithmetic.")
    P("- **Assumed, not measured:** every failure rate, token count, thinking-token count and latency.")
    P("- **Not measured at all:** output quality. A single strong model might write better emails, or the split might lose context between stations. Only real runs plus human or inspector review can answer that.")
    P("- **Reproduce:** `python benchmarks/bench.py` (seeded; same assumptions → same numbers except wall-clock overhead).")

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    report = "\n".join(lines) + "\n"
    (out_dir / "latest.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
