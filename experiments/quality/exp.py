"""Blind quality experiment: single Haiku vs single Sonnet vs the assembly line.

    python experiments/quality/exp.py estimate --leads 3
    python experiments/quality/exp.py run --mock --leads 3
    python experiments/quality/exp.py run --real --leads 3 --approve-max-usd 0.60   # costs money
    python experiments/quality/exp.py blind <run_dir>
    python experiments/quality/exp.py score <run_dir> --reviews my_scores.csv

See experiments/quality/PROTOCOL.md for the design and its known weaknesses.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from assembly_line import openrouter  # noqa: E402
from assembly_line.client import PROVIDERS, MockClient  # noqa: E402
from assembly_line.config import LineSpec, StationSpec, load_line  # noqa: E402
from assembly_line.line import Line, system_prompt  # noqa: E402
from assembly_line.models import cost_usd, get_model, next_up  # noqa: E402

WORKFLOWS = ["single_haiku", "single_sonnet", "assembly_line"]
STEPS = ["enrich", "score", "draft"]
# Pre-registered pilot set: one clear high-fit lead with a named owner, one with almost
# no evidence (invention trap), one with contradictory evidence.
PILOT_IDS = ["L01", "L12", "L20"]
CRITERIA = ["accuracy_1to5", "personalization_1to5", "writing_1to5", "offer_fit_1to5"]
SHEET_COLS = ["review_id", "lead", *CRITERIA, "unsupported_claims_count", "would_send_yes_no",
              "rank_within_lead", "notes"]
EXPECTED_OUT = {"enrich": 250, "score": 150, "draft": 250, "all_in_one": 650, "inspector": 400}
WEB_SEARCH_WORST_TOKENS = 20_000  # per search, only if a station has web search (none do here)
SMALL_N = 10


# ---------------------------------------------------------------- data

def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def load_leads(n: int | None = None, ids: list[str] | None = None) -> list[dict]:
    leads = load_jsonl(HERE / "data" / "leads.jsonl")
    if ids:
        by = {l["id"]: l for l in leads}
        return [by[i] for i in ids]
    if n == 3:
        return load_leads(ids=PILOT_IDS)
    return leads[:n] if n else leads


def load_gold() -> dict[str, dict]:
    """Gold labels. Used only for scoring; never passed to any model."""
    return {g["id"]: g for g in load_jsonl(HERE / "data" / "gold.jsonl")}


# ---------------------------------------------------------------- workflows

def _spec_text(name: str) -> str:
    return (HERE / "spec" / f"{name}.md").read_text().strip()


def build_workflows(inspector: str = "none") -> dict[str, LineSpec]:
    """Same task spec, same schemas, same retry/escalation policy for every workflow.

    The assembly line keeps lead_gen's stations, models, escalation and max_tokens.
    Differences from lines/lead_gen: no web search (every workflow gets the same evidence
    packet instead) and instructions come from the shared spec in experiments/quality/spec.
    """
    base = load_line(ROOT / "lines" / "lead_gen")
    ctx = _spec_text("context")
    specs: dict[str, LineSpec] = {}

    line_stations = [replace(s, tools=[], instructions=f"{ctx}\n\n{_spec_text(s.name)}") for s in base.stations]
    assert [s.name for s in line_stations] == STEPS
    specs["assembly_line"] = LineSpec(name="assembly_line", path=base.path, stations=line_stations,
                                      inspector=None, concurrency=1)

    combined_schema = {"type": "object",
                       "properties": {s.name: s.output_schema for s in base.stations},
                       "required": STEPS}
    combined_instr = (f"{ctx}\n\nComplete all three steps below, in order. Put each step's result "
                      f"under its key ({', '.join(STEPS)}).\n\n" +
                      "\n\n".join(f"## Step {i + 1}: {n}\n{_spec_text(n)}" for i, n in enumerate(STEPS)))
    for model in ["haiku", "sonnet"]:
        st = StationSpec(name="all_in_one", model=model, escalate_to=next_up(model), max_retries=1,
                         max_tokens=sum(s.max_tokens for s in base.stations), tools=[],
                         instructions=combined_instr, output_schema=combined_schema)
        specs[f"single_{model}"] = LineSpec(name=f"single_{model}", path=base.path, stations=[st],
                                            inspector=None, concurrency=1)

    if inspector == "all":
        insp = replace(base.inspector, sample_rate=1.0, min_inspected=10**9,
                       stop_line_if_defect_rate_above=1.01)
        for s in specs.values():
            s.inspector = deepcopy(insp)
    elif inspector != "none":
        raise ValueError("inspector must be 'none' or 'all'")
    for s in specs.values():
        s.call_timeout_s = 300
    return specs


def normalize(workflow: str, payload: dict) -> dict:
    src = payload["all_in_one"] if workflow.startswith("single_") else payload
    return {"facts": src["enrich"], "fit_score": src["score"]["fit_score"],
            "best_offer": src["score"]["best_offer"], "reasoning": src["score"]["reasoning"],
            "subject": src["draft"]["subject"], "email_body": src["draft"]["email_body"]}


def item_payload_chars(lead: dict) -> int:
    return len(json.dumps({"id": lead["id"], "input": lead}, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- cost estimate

def estimate(specs: dict[str, LineSpec], leads: list[dict], budget_per_workflow: float) -> dict:
    """Worst case assumes every attempt fails, uses all max_tokens, and escalates.
    Tokens are estimated at 3 characters per token (conservative for English)."""
    out = {}
    for wf, spec in specs.items():
        worst = expected = max_single = 0.0
        for lead in leads:
            prev_out_chars = 0
            stations = list(spec.stations)
            if spec.inspector:
                stations.append(StationSpec(name="inspector", model=spec.inspector.model,
                                            instructions=spec.inspector.instructions, output_schema={},
                                            max_retries=1, max_tokens=spec.inspector.max_tokens))
            for st in stations:
                sys_chars = len(system_prompt(st.instructions, st.output_schema))
                user_chars = item_payload_chars(lead) + prev_out_chars + 300
                in_worst = math.ceil((sys_chars + user_chars) / 3)
                searches = 3 if "web_search" in (st.tools or []) else 0
                plan = [st.model] * (1 + st.max_retries) + ([st.escalate_to] if st.escalate_to else [])
                for alias in plan:
                    c = cost_usd(alias, in_worst + searches * WEB_SEARCH_WORST_TOKENS, st.max_tokens,
                                 web_searches=searches)
                    worst += c
                    max_single = max(max_single, c)
                exp_out = min(st.max_tokens, EXPECTED_OUT.get(st.name, 300))
                expected += cost_usd(st.model, math.ceil((sys_chars + user_chars) / 4), exp_out,
                                     web_searches=searches)
                prev_out_chars += st.max_tokens * 4
        ceiling = min(worst, budget_per_workflow + max_single)
        out[wf] = {"expected": expected, "worst_case": worst, "max_single_call": max_single,
                   "budget": budget_per_workflow, "ceiling": ceiling}
    out["_total"] = {k: sum(v[k] for w, v in out.items() if not w.startswith("_"))
                     for k in ["expected", "worst_case", "ceiling"]}
    return out


def print_estimate(est: dict, n_leads: int, inspector: str):
    print(f"Cost estimate: {n_leads} leads per workflow, inspector={inspector}, concurrency 1")
    print(f"{'workflow':15} {'expected':>10} {'worst case':>11} {'budget':>8} {'max 1 call':>11} {'hard ceiling':>13}")
    for wf in WORKFLOWS:
        e = est[wf]
        print(f"{wf:15} ${e['expected']:9.4f} ${e['worst_case']:10.4f} ${e['budget']:7.2f} "
              f"${e['max_single_call']:10.4f} ${e['ceiling']:12.4f}")
    t = est["_total"]
    print(f"{'TOTAL':15} ${t['expected']:9.4f} ${t['worst_case']:10.4f} {'':8} {'':11} ${t['ceiling']:12.4f}")
    print("hard ceiling = min(worst case, budget + one maximum call). With concurrency 1 the budget is checked "
          "before every call, so at most one call can cross it.")


# ---------------------------------------------------------------- run

def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def ledger_by_item(run_dir: Path) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for l in (run_dir / "ledger.jsonl").read_text().splitlines():
        e = json.loads(l)
        by.setdefault(e["item_id"], []).append(e)
    return by


def run_experiment(specs, leads, client, out_dir: Path, mode: str, budget: float, seed: int,
                   inspector: str, provider: str = "anthropic") -> Path:
    out_dir.mkdir(parents=True)
    order = list(WORKFLOWS)
    random.Random(seed).shuffle(order)  # don't always run the same workflow first
    manifest = {"mode": mode, "provider": provider, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "git_commit": git_commit(),
                "leads": [l["id"] for l in leads], "workflow_order": order, "seed": seed,
                "budget_per_workflow": budget, "inspector": inspector,
                "models": {wf: [(s.name, s.model, s.escalate_to) for s in specs[wf].stations] for wf in order}}
    rows = []
    for wf in order:
        spec = specs[wf]
        spec.budget_usd = budget
        spec.concurrency = 1
        print(f"[{mode}] {wf}: {len(leads)} leads", flush=True)
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            rd = asyncio.run(Line(spec, client, runs_root=out_dir / "workflows", seed=seed).run(leads))
        wall = time.perf_counter() - t0
        summary = json.loads((rd / "summary.json").read_text())
        led = ledger_by_item(rd)
        done = {p["id"]: (p, None) for p in load_jsonl(rd / "output.jsonl")}
        failed = {}
        for d in load_jsonl(rd / "defects.jsonl"):
            if d["failed_at"] == "inspection":  # inspector never removes outputs from the experiment
                done[d["id"]] = (d["payload"], d["payload"].get("_inspection"))
            else:
                failed[d["id"]] = d
        for lead in leads:
            entries = led.get(lead["id"], [])
            row = {"workflow": wf, "lead": lead["id"],
                   "cost_usd": sum(e["cost_usd"] for e in entries),
                   "latency_s": sum(e["latency_s"] for e in entries),
                   "calls": len(entries), "failed_calls": sum(not e["ok"] for e in entries),
                   "escalations": sum(e["escalated"] for e in entries),
                   "models_used": sorted({e["model"] for e in entries if e["ok"]}),
                   "output": None, "inspector_verdict": None, "error": None}
            if lead["id"] in done:
                payload, verdict = done[lead["id"]]
                row.update(status="complete", output=normalize(wf, payload),
                           inspector_verdict=verdict or payload.get("_inspection"))
            elif lead["id"] in failed:
                row.update(status="failed", error=failed[lead["id"]]["error"],
                           failed_at=failed[lead["id"]]["failed_at"])
            else:
                row.update(status="not_run", error=summary.get("stopped_reason"))
            rows.append(row)
        manifest.setdefault("workflow_wall_s", {})[wf] = wall
        manifest.setdefault("workflow_cost_usd", {})[wf] = summary["total_cost_usd"]
        manifest.setdefault("stopped", {})[wf] = summary.get("stopped_reason")
        if summary.get("stopped_reason") and "fatal" in summary["stopped_reason"]:
            manifest["aborted"] = f"{wf}: {summary['stopped_reason']}"
            print(f"ABORTING experiment: {manifest['aborted']}")
            break
    (out_dir / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    # Which exact model builds served each alias, across all arms (they should match).
    served: dict[str, set] = {}
    sources: dict[str, int] = {}
    for led in (out_dir / "workflows").glob("*/ledger.jsonl"):
        for e in load_jsonl(led):
            if e.get("served_model"):
                served.setdefault(e["model"], set()).add(e["served_model"])
            sources[e.get("cost_source", "estimated")] = sources.get(e.get("cost_source", "estimated"), 0) + 1
    manifest["served_models"] = {k: sorted(v) for k, v in served.items()}
    manifest["model_versions_consistent"] = all(len(v) == 1 for v in served.values())
    manifest["cost_sources"] = sources
    if not manifest["model_versions_consistent"]:
        print(f"WARNING: different model builds served the same alias: {manifest['served_models']}")
    manifest["total_cost_usd"] = sum(manifest.get("workflow_cost_usd", {}).values())
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out_dir


# ---------------------------------------------------------------- blind review

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_blind(run_dir: Path, seed: int) -> dict:
    rows = load_jsonl(run_dir / "results.jsonl")
    leads = {l["id"]: l for l in load_leads()}
    rng = random.Random(f"blind|{seed}")
    by_lead: dict[str, list[dict]] = {}
    for r in rows:
        if r["status"] == "complete":
            by_lead.setdefault(r["lead"], []).append(r)
    used, key, sheet, md = set(), {}, [], []
    md += ["# Blind review packet", "",
           "Score each response using RUBRIC.md. You are not told which system wrote which response.",
           "Fill in review_sheet.csv. Do not open KEY_sealed.json until every row is scored.", ""]
    lead_order = sorted(by_lead)
    rng.shuffle(lead_order)
    for lid in lead_order:
        lead = leads[lid]
        md += [f"## Lead {lid}: {lead['business']['name']} ({lead['business']['category']}, {lead['business']['city']})",
               "", "**Evidence (the only facts the responses were allowed to use):**", ""]
        md += [f"- *{e['source']}*: {e['text']}" for e in lead["evidence"]]
        md.append("")
        outs = by_lead[lid][:]
        rng.shuffle(outs)
        for r in outs:
            while True:
                rid = "".join(rng.choice(ALPHABET) for _ in range(6))
                if rid not in used:
                    used.add(rid)
                    break
            o = r["output"]
            key[rid] = {"workflow": r["workflow"], "lead": lid}
            md += [f"### Response {rid}", "",
                   f"- fit_score: {o['fit_score']}", f"- best_offer: {o['best_offer']}",
                   f"- subject: {o['subject'].strip()}", "", "```", o["email_body"].strip(), "```", ""]
            sheet.append({c: "" for c in SHEET_COLS} | {"review_id": rid, "lead": lid})
    (run_dir / "review.md").write_text("\n".join(md))
    with open(run_dir / "review_sheet.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLS)
        w.writeheader()
        w.writerows(sheet)
    (run_dir / "KEY_sealed.json").write_text(json.dumps(
        {"_warning": "Do not open until all reviews are complete.", "key": key}, indent=2))
    return key


def simulate_review(run_dir: Path, seed: int) -> Path:
    """Random scores for testing the scoring pipeline. NOT evidence of anything."""
    rng = random.Random(f"synthetic|{seed}")
    rows = list(csv.DictReader(open(run_dir / "review_sheet.csv")))
    by_lead: dict[str, list[dict]] = {}
    for r in rows:
        for c in CRITERIA:
            r[c] = rng.randint(1, 5)
        r["unsupported_claims_count"] = rng.choice([0, 0, 0, 1, 2])
        r["would_send_yes_no"] = rng.choice(["yes", "no"])
        r["notes"] = "SYNTHETIC"
        by_lead.setdefault(r["lead"], []).append(r)
    for rs in by_lead.values():
        for rank, r in enumerate(rng.sample(rs, len(rs)), 1):
            r["rank_within_lead"] = rank
    out = run_dir / f"SYNTHETIC_reviews_seed{seed}.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLS + ["synthetic"])
        w.writeheader()
        for r in rows:
            w.writerow(r | {"synthetic": 1})
    return out


# ---------------------------------------------------------------- automatic checks

def auto_checks(o: dict, lead: dict, gold: dict) -> dict:
    """Objective checks against gold labels + cheap hallucination proxies. Not a quality judgment."""
    evidence = " ".join(e["text"] for e in lead["evidence"]) + " " + lead["business"]["name"]
    facts = o["facts"]
    fields = ["online_booking", "chat_or_text_widget", "review_presence"]
    correct = sum(facts.get(f) in gold[f] for f in fields)
    body = o["email_body"]
    closed = gold["fit_range"] == [1, 1]
    nums_ev = set(re.findall(r"\d+(?:\.\d+)?", evidence))
    nums_out = re.findall(r"\d+(?:\.\d+)?", o["subject"] + " " + body)
    unsupported_numbers = sorted({n for n in nums_out if n not in nums_ev and n not in {"10"}})
    m = re.match(r"\s*(?:hi|hey|hello|dear)\s+([A-Z][a-z]+)", body, re.I)
    greet = m.group(1) if m else None
    greet_unsupported = bool(greet and greet.lower() not in {"there", "team", "all"}
                             and greet.lower() not in evidence.lower())
    words = len(body.split())
    lo, hi = gold["fit_range"]
    return {
        "facts_correct_of_3": correct,
        "fit_in_range": lo <= o["fit_score"] <= hi,
        "offer_acceptable": o["best_offer"] in gold["acceptable_offers"],
        "word_count": words,
        "under_110_words": words < 110 or body.strip() == "NO_EMAIL",
        "closed_handled": (body.strip() == "NO_EMAIL") if closed else None,
        "unsupported_numbers": unsupported_numbers,
        "greeting_name_unsupported": greet_unsupported,
        "signed_braden": "braden" in body.lower() or body.strip() == "NO_EMAIL",
    }


# ---------------------------------------------------------------- stats

def ci_text(lo, hi, signed=False):
    if math.isnan(lo):
        return f"n/a (n<{SMALL_N})"
    return f"{lo:+.2f} to {hi:+.2f}" if signed else f"{lo:.2f}–{hi:.2f}"


def mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return statistics.mean(xs) if xs else float("nan")


def bootstrap_ci(xs, seed=0, B=4000, alpha=0.05):
    """Percentile bootstrap. Returns NaNs below SMALL_N: with a handful of points the
    interval is far too narrow and would look like evidence when it isn't."""
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if len(xs) < SMALL_N:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    ms = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(B))
    return ms[int(B * alpha / 2)], ms[int(B * (1 - alpha / 2)) - 1]


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test, ties dropped."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


# ---------------------------------------------------------------- score

def load_reviews(files: list[Path]) -> tuple[dict[str, list[dict]], bool]:
    by_rid: dict[str, list[dict]] = {}
    synthetic = False
    for f in files:
        rows = list(csv.DictReader(open(f)))
        if Path(f).name.startswith("SYNTHETIC") or any(r.get("synthetic") == "1" for r in rows):
            synthetic = True
        for r in rows:
            if all(r.get(c, "").strip() for c in CRITERIA) and r.get("would_send_yes_no", "").strip():
                by_rid.setdefault(r["review_id"], []).append(r)
    return by_rid, synthetic


def score(run_dir: Path, review_files: list[Path], seed: int = 0) -> str:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    rows = load_jsonl(run_dir / "results.jsonl")
    leads = {l["id"]: l for l in load_leads()}
    gold = load_gold()
    key = json.loads((run_dir / "KEY_sealed.json").read_text())["key"] if (run_dir / "KEY_sealed.json").exists() else {}
    rid_of = {(v["workflow"], v["lead"]): k for k, v in key.items()}
    reviews, synthetic = load_reviews(review_files) if review_files else ({}, False)

    per = []
    for r in rows:
        rec = dict(r)
        if r["status"] == "complete":
            rec["auto"] = auto_checks(r["output"], leads[r["lead"]], gold[r["lead"]])
        rid = rid_of.get((r["workflow"], r["lead"]))
        rv = reviews.get(rid, [])
        if rv:
            rec["human"] = {c: mean([float(x[c]) for x in rv]) for c in CRITERIA}
            rec["human"]["overall"] = mean([rec["human"][c] for c in CRITERIA])
            rec["human"]["unsupported_claims"] = mean([float(x["unsupported_claims_count"] or 0) for x in rv])
            yes = sum(x["would_send_yes_no"].strip().lower().startswith("y") for x in rv)
            rec["human"]["accepted"] = yes * 2 > len(rv)
            ranks = [float(x["rank_within_lead"]) for x in rv if x.get("rank_within_lead", "").strip()]
            rec["human"]["rank"] = mean(ranks) if ranks else float("nan")
            rec["n_reviewers"] = len(rv)
        per.append(rec)

    lead_ids = sorted({r["lead"] for r in rows})
    n = len(lead_ids)
    has_human = any("human" in r for r in per)
    L = []
    P = L.append
    banners = []
    if manifest["mode"] != "REAL":
        banners.append(f"**{manifest['mode']} RUN: outputs came from fake workers. Nothing here says anything about real model quality.**")
    if synthetic:
        banners.append("**SYNTHETIC REVIEWS: human scores were randomly generated to test the pipeline. They are not evidence.**")
    if n < SMALL_N:
        banners.append(f"**n = {n} leads. Descriptive only — far too small to conclude any workflow is better or worse.**")
    P("# Experiment results")
    P("")
    for b in banners:
        P(f"> {b}")
        P("")
    P(f"Mode: {manifest['mode']} · commit {manifest['git_commit']} · leads: {', '.join(lead_ids)} · "
      f"inspector: {manifest['inspector']} · workflow order: {', '.join(manifest['workflow_order'])}")
    if manifest.get("aborted"):
        P(f"\n**Aborted:** {manifest['aborted']}")
    P("")
    P("## Reliability, cost and latency (from the ledger)")
    P("")
    P("| Workflow | Completed | Calls / lead | Failed calls | Escalations | Total cost | Cost / lead | Mean latency / lead |")
    P("|---|---|---|---|---|---|---|---|")
    for wf in WORKFLOWS:
        rs = [r for r in per if r["workflow"] == wf]
        if not rs:
            continue
        comp = sum(r["status"] == "complete" for r in rs)
        calls = sum(r["calls"] for r in rs)
        P(f"| {wf} | {comp}/{len(rs)} | {calls / len(rs):.2f} | {sum(r['failed_calls'] for r in rs)} "
          f"({sum(r['failed_calls'] for r in rs) / calls:.0%} of calls) | {sum(r['escalations'] for r in rs)} | "
          f"${sum(r['cost_usd'] for r in rs):.4f} | ${mean([r['cost_usd'] for r in rs]):.4f} | "
          f"{mean([r['latency_s'] for r in rs]):.1f}s |" if calls else f"| {wf} | 0/{len(rs)} | 0 | – | – | $0 | – | – |")
    P("")
    P("## Automatic checks against gold labels")
    P("")
    P("Objective but narrow: facts the evidence settles, plus simple hallucination proxies. Not a quality score.")
    P("")
    P("| Workflow | Facts correct | Fit score in range | Offer acceptable | Under 110 words | Unsupported numbers | Invented greeting name | Closed business handled |")
    P("|---|---|---|---|---|---|---|---|")
    for wf in WORKFLOWS:
        a = [r["auto"] for r in per if r["workflow"] == wf and "auto" in r]
        if not a:
            P(f"| {wf} | no completed outputs | | | | | | |")
            continue
        closed = [x["closed_handled"] for x in a if x["closed_handled"] is not None]
        P(f"| {wf} | {sum(x['facts_correct_of_3'] for x in a)}/{3 * len(a)} | "
          f"{sum(x['fit_in_range'] for x in a)}/{len(a)} | {sum(x['offer_acceptable'] for x in a)}/{len(a)} | "
          f"{sum(x['under_110_words'] for x in a)}/{len(a)} | {sum(len(x['unsupported_numbers']) for x in a)} | "
          f"{sum(x['greeting_name_unsupported'] for x in a)} | "
          f"{f'{sum(closed)}/{len(closed)}' if closed else 'n/a'} |")
    P("")

    if has_human:
        P("## Blind human review")
        P("")
        P("| Workflow | Reviewed | Accuracy | Personalization | Writing | Offer fit | Overall (95% CI) | Unsupported claims / email | Accepted | Mean rank | Cost per accepted |")
        P("|---|---|---|---|---|---|---|---|---|---|---|")
        for wf in WORKFLOWS:
            rs = [r for r in per if r["workflow"] == wf]
            h = [r["human"] for r in rs if "human" in r]
            if not h:
                continue
            accepted = sum(x["accepted"] for x in h)
            cost = sum(r["cost_usd"] for r in rs)
            lo, hi = bootstrap_ci([x["overall"] for x in h], seed)
            P(f"| {wf} | {len(h)}/{len(rs)} | " + " | ".join(f"{mean([x[c] for x in h]):.2f}" for c in CRITERIA) +
              f" | {mean([x['overall'] for x in h]):.2f} ({ci_text(lo, hi)}) | {mean([x['unsupported_claims'] for x in h]):.2f} | "
              f"{accepted}/{len(rs)} | {mean([x['rank'] for x in h]):.2f} | "
              f"{'$%.4f' % (cost / accepted) if accepted else 'no accepted outputs'} |")
        P("")
        P("Leads where a workflow produced no output count as not accepted. Cost per accepted output includes the cost of failures.")
        P("")
        P("### Assembly line vs. each single model (paired by lead, overall human score)")
        P("")
        P("| Comparison | Leads compared | Line wins / ties / losses | Mean difference (95% CI) | Sign-test p |")
        P("|---|---|---|---|---|")
        line = {r["lead"]: r["human"]["overall"] for r in per if r["workflow"] == "assembly_line" and "human" in r}
        for wf in ["single_haiku", "single_sonnet"]:
            other = {r["lead"]: r["human"]["overall"] for r in per if r["workflow"] == wf and "human" in r}
            both = sorted(set(line) & set(other))
            diffs = [line[i] - other[i] for i in both]
            w = sum(d > 0 for d in diffs)
            t = sum(d == 0 for d in diffs)
            lo_ = sum(d < 0 for d in diffs)
            lo, hi = bootstrap_ci(diffs, seed)
            P(f"| line vs {wf} | {len(both)} | {w} / {t} / {lo_} | {mean(diffs):+.2f} ({ci_text(lo, hi, signed=True)}) | {sign_test(w, lo_):.3f} |")
        P("")
        P("A p-value above 0.05 or a CI that crosses 0 means *no detectable difference at this sample size*, "
          "not that the workflows are equal. With 3 leads the smallest possible sign-test p is 0.25.")
        multi = [r for r in per if r.get("n_reviewers", 0) >= 2]
        if multi:
            agree = []
            for r in multi:
                rv = reviews[rid_of[(r["workflow"], r["lead"])]]
                votes = [x["would_send_yes_no"].strip().lower().startswith("y") for x in rv]
                agree.append(len(set(votes)) == 1)
            P("")
            P(f"Inter-reviewer agreement on 'would send': {sum(agree)}/{len(agree)} outputs.")
        P("")
    else:
        P("_No human reviews loaded yet. Run `blind`, fill in review_sheet.csv, then `score --reviews`._")
        P("")

    P("## Does the extra structure justify its cost?")
    P("")
    P("Pre-registered rule (PROTOCOL.md): the assembly line is only justified over single Haiku if, at 20 leads, "
      "its acceptance rate or overall human score is higher with a 95% CI excluding zero, and its cost per "
      "accepted output is no more than 2× single Haiku's. Otherwise the hypothesis is not supported.")
    if n < SMALL_N or not has_human or synthetic or manifest["mode"] != "REAL":
        P("")
        P("**Verdict: not evaluable from this run.**")
    report = "\n".join(L) + "\n"
    (run_dir / "report.md").write_text(report)
    flat = []
    for r in per:
        f = {k: r.get(k) for k in ["workflow", "lead", "status", "cost_usd", "latency_s", "calls", "failed_calls", "escalations"]}
        f.update({f"auto_{k}": v for k, v in (r.get("auto") or {}).items()})
        f.update({f"human_{k}": v for k, v in (r.get("human") or {}).items()})
        flat.append(f)
    cols = sorted({k for f in flat for k in f}, key=lambda k: (not k in ("workflow", "lead", "status"), k))
    with open(run_dir / "scores.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(flat)
    return report


# ---------------------------------------------------------------- CLI

def make_real_client(provider: str = "anthropic"):
    from assembly_line.client import ClaudeClient
    return ClaudeClient(provider=provider)


def models_needed(specs: dict[str, LineSpec]) -> list[str]:
    """Every model a run could call, including escalations and the inspector."""
    need = set()
    for spec in specs.values():
        for st in spec.stations:
            need.add(st.model)
            if st.escalate_to:
                need.add(st.escalate_to)
        if spec.inspector:
            need.add(spec.inspector.model)
    order = ["haiku", "sonnet", "opus", "fable"]
    return [m for m in order if m in need]


def print_models(specs, provider: str):
    print(f"Provider: {provider}. Models this run can call (same IDs for every arm):")
    for alias in models_needed(specs):
        m = get_model(alias)
        mid = m.openrouter_id if provider == "openrouter" else m.id
        print(f"  {alias:7} {mid or 'NOT AVAILABLE ON THIS PROVIDER':30} "
              f"${m.price.input:.2f} in / ${m.price.output:.2f} out per M tokens")
    for wf in WORKFLOWS:
        plan = " → ".join(f"{st.name}:{st.model}(→{st.escalate_to})" for st in specs[wf].stations)
        print(f"  {wf:14} {plan}")


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def cmd_estimate(a):
    leads = load_leads(a.leads)
    specs = build_workflows(a.inspector)
    print_models(specs, a.provider)
    print()
    est = estimate(specs, leads, a.budget_per_workflow)
    print_estimate(est, len(leads), a.inspector)
    return est


def cmd_check_openrouter(a):
    """Free: reads OpenRouter's public model list and your key's limit. No model calls."""
    load_env()
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY is not set in .env. Run: bash scripts/set-openrouter-key.sh")
    specs = build_workflows(a.inspector)
    est = estimate(specs, load_leads(a.leads), a.budget_per_workflow)
    res = openrouter_preflight(key, models_needed(specs), est["_total"]["ceiling"])
    openrouter.print_preflight(res)
    return res


def openrouter_preflight(key, aliases, ceiling):
    return openrouter.preflight(key, aliases, ceiling)


def cmd_run(a):
    leads = load_leads(a.leads)
    specs = build_workflows(a.inspector)
    print_models(specs, a.provider)
    print()
    est = estimate(specs, leads, a.budget_per_workflow)
    print_estimate(est, len(leads), a.inspector)
    print()
    if a.real:
        # Every gate below must pass before a client that can spend money is created.
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        key_env = PROVIDERS[a.provider]["key_env"]
        if not os.getenv(key_env):
            sys.exit(f"REAL run refused: {key_env} is not set for provider '{a.provider}'. "
                     f"(Not falling back to another provider or to mock.)")
        missing = [m for m in models_needed(specs)
                   if a.provider == "openrouter" and not get_model(m).openrouter_id]
        if missing:
            sys.exit(f"REAL run refused: no verified OpenRouter ID for {', '.join(missing)}.")
        ceiling = est["_total"]["ceiling"]
        if a.approve_max_usd is None or a.approve_max_usd < ceiling:
            sys.exit(f"REAL run refused: the hard ceiling is ${ceiling:.4f}. Re-run with "
                     f"--approve-max-usd {math.ceil(ceiling * 100) / 100:.2f} (or higher) to approve it.")
        if a.provider == "openrouter":
            res = openrouter_preflight(os.getenv(key_env), models_needed(specs), ceiling)
            openrouter.print_preflight(res)
            if res["problems"]:
                sys.exit("REAL run refused: OpenRouter pre-flight found problems (above). Nothing was spent.")
        if not a.yes:
            if not sys.stdin.isatty():
                sys.exit("REAL run refused: no terminal to confirm in. Add --yes only if you mean it.")
            ans = input(f"This will make PAID API calls via {a.provider}, up to ${ceiling:.4f}. "
                        f"Type SPEND to continue: ")
            if ans.strip() != "SPEND":
                sys.exit("Cancelled. Nothing was spent.")
        client, mode = make_real_client(a.provider), "REAL"
    else:
        client, mode = MockClient(fail_rate=a.mock_fail_rate, seed=a.seed), "MOCK"
    out = HERE / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{mode}-{a.provider}-{len(leads)}leads"
    run_experiment(specs, leads, client, out, mode, a.budget_per_workflow, a.seed, a.inspector,
                   provider=a.provider)
    make_blind(out, a.seed)
    print(score(out, []))
    print(f"\nRun folder: {out}")
    print(f"Next: open {out / 'review.md'}, score in {out / 'review_sheet.csv'}, then:\n"
          f"  python experiments/quality/exp.py score {out} --reviews {out / 'review_sheet.csv'}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="exp")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--leads", type=int, default=3, help="3 = pre-registered pilot set; 20 = all")
        p.add_argument("--inspector", choices=["none", "all"], default="none")
        p.add_argument("--budget-per-workflow", type=float, default=0.20)
        p.add_argument("--provider", choices=["anthropic", "openrouter"], default="anthropic")

    e = sub.add_parser("estimate")
    common(e)
    e.set_defaults(fn=cmd_estimate)

    c = sub.add_parser("check-openrouter", help="free: verify model IDs, prices and your key's limit")
    common(c)
    c.set_defaults(fn=cmd_check_openrouter)

    r = sub.add_parser("run")
    common(r)
    mode = r.add_mutually_exclusive_group(required=True)  # no default: you must say which
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--real", action="store_true")
    r.add_argument("--approve-max-usd", type=float)
    r.add_argument("--yes", action="store_true", help="skip the typed confirmation (real runs)")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--mock-fail-rate", type=float, default=0.15)
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("blind")
    b.add_argument("run_dir", type=Path)
    b.add_argument("--seed", type=int, default=1)
    b.set_defaults(fn=lambda a: print(f"{len(make_blind(a.run_dir, a.seed))} responses anonymized"))

    s = sub.add_parser("simulate-review", help="fill the sheet with RANDOM scores to test scoring")
    s.add_argument("run_dir", type=Path)
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(fn=lambda a: print(simulate_review(a.run_dir, a.seed)))

    sc = sub.add_parser("score")
    sc.add_argument("run_dir", type=Path)
    sc.add_argument("--reviews", type=Path, nargs="*", default=[])
    sc.set_defaults(fn=lambda a: print(score(a.run_dir, a.reviews)))

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    main()
