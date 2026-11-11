"""The assembly line runner.

Each input item moves station to station. At every station a worker model must
return JSON matching the station's schema (interchangeable parts). If it can't:
retry with the error as feedback, then escalate to a stronger model. If that
still fails, the item is pulled off the line as defective.

A stronger inspector model spot-checks a sample of finished items. If the
defect rate crosses the threshold, it pulls the andon cord and the whole line
stops, so you don't pay to mass-produce garbage. The line also stops if the
budget is hit.
"""
from __future__ import annotations

import asyncio
import csv
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from .client import CallResult
from .config import LineSpec, StationSpec
from .ledger import Ledger, build_report

INSPECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "fix_suggestion": {"type": "string"},
    },
    "required": ["pass", "issues"],
}


def system_prompt(instructions: str, schema: dict) -> str:
    return (
        f"{instructions.strip()}\n\n"
        "## Output format\n"
        "You are one station on an assembly line. Your output is parsed by a program, not read by a person.\n"
        "Respond with ONLY a single JSON object matching this JSON Schema. No prose, no code fences.\n\n"
        f"{json.dumps(schema, indent=2)}"
    )


def extract_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start = t.find("{")
    if start == -1:
        raise ValueError("no JSON object found in output")
    # raw_decode stops at the end of the first complete object, so prose
    # after the JSON (even prose containing braces) doesn't break parsing.
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    return obj


def parse_and_validate(text: str, schema: dict):
    """Returns (output, None) on success or (None, error message) on failure."""
    try:
        out = extract_json(text)
        jsonschema.validate(out, schema)
        return out, None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg}"
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) or "(root)"
        return None, f"schema violation at {path}: {e.message}"
    except ValueError as e:
        return None, str(e)


@dataclass
class StationOutcome:
    output: dict | None
    error: str | None
    halted: bool = False        # stopped because the line stopped (budget/andon), not a defect
    fatal: str | None = None    # an error that will hit every item (bad key, bad model id...)


# API errors where retrying or moving to the next item can't help.
FATAL_ERRORS = {"AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError"}


async def run_station(client, ledger: Ledger, st: StationSpec, item_id: str, payload: dict,
                      should_stop=None, timeout_s: float | None = None) -> StationOutcome:
    plan = [st.model] * (1 + st.max_retries)
    if st.escalate_to:
        plan.append(st.escalate_to)
    system = system_prompt(st.instructions, st.output_schema)
    feedback = last_err = None
    for attempt, alias in enumerate(plan):
        # Check before every attempt, not just every station: retries and
        # escalations cost money too.
        if should_stop and should_stop():
            return StationOutcome(None, "line stopped", halted=True)
        user = "Work item (JSON):\n" + json.dumps(payload, indent=2, ensure_ascii=False)
        if feedback:
            user += f"\n\nYour previous attempt was rejected: {feedback}. Return valid JSON only."
        try:
            call = client.call(alias, system, user, st.max_tokens, st.tools, st.output_schema)
            result = await (asyncio.wait_for(call, timeout_s) if timeout_s else call)
        except Exception as e:  # timeout, network, API error
            name = type(e).__name__
            err = f"{name}: {e}" if str(e) else name
            if isinstance(e, asyncio.TimeoutError):
                err = f"timed out after {timeout_s}s"
            ledger.record(item_id, st.name, CallResult(text="", model=alias), attempt, ok=False,
                          escalated=alias != st.model, error=err)
            if name in FATAL_ERRORS:
                return StationOutcome(None, err, fatal=err)
            feedback = None  # nothing the model did wrong; just try again
            last_err = err
            continue
        out, err = parse_and_validate(result.text, st.output_schema)
        if out is None and result.extra.get("stop_reason") == "max_tokens":
            err += " (output was cut off at max_tokens; raise max_tokens for this station)"
        ledger.record(item_id, st.name, result, attempt, ok=out is not None,
                      escalated=alias != st.model, error=err)
        if out is not None:
            return StationOutcome(out, None)
        feedback = last_err = err
    return StationOutcome(None, last_err)


class Line:
    def __init__(self, spec: LineSpec, client, runs_root: Path = Path("runs"), seed: int | None = None):
        self.spec = spec
        self.client = client
        stamp = time.strftime("%Y%m%d-%H%M%S")
        runs_root.mkdir(parents=True, exist_ok=True)
        n = 1
        while True:  # never reuse a folder: two runs in the same second used to share a ledger
            self.run_dir = runs_root / (f"{spec.name}-{stamp}" + (f"-{n}" if n > 1 else ""))
            try:
                self.run_dir.mkdir()
                break
            except FileExistsError:
                n += 1
        self.ledger = Ledger(self.run_dir)
        self.seed = seed
        self.rng = random.Random(seed)
        self.stop = asyncio.Event()
        self.stopped_reason: str | None = None
        self.finished: list[dict] = []
        self.defective: list[dict] = []
        self.skipped = 0
        self.inspected = 0
        self.inspection_failures = 0

    def _pull_andon(self, reason: str):
        if not self.stop.is_set():
            self.stopped_reason = reason
            self.stop.set()
            print(f"\n[ANDON] Line stopped: {reason}")

    def _over_budget(self) -> bool:
        if self.ledger.spent >= self.spec.budget_usd:
            self._pull_andon(f"budget of ${self.spec.budget_usd:.2f} reached")
            return True
        return False

    def _should_stop(self) -> bool:
        return self.stop.is_set() or self._over_budget()

    async def _inspect(self, item_id: str, payload: dict) -> dict | None:
        insp = self.spec.inspector
        st = StationSpec(name="inspector", model=insp.model, instructions=insp.instructions,
                         output_schema=INSPECTION_SCHEMA, max_retries=1, max_tokens=insp.max_tokens)
        outcome = await run_station(self.client, self.ledger, st, item_id, payload,
                                    should_stop=self._should_stop, timeout_s=self.spec.call_timeout_s)
        if outcome.fatal:
            self._pull_andon(f"fatal API error: {outcome.fatal}")
        return outcome.output

    async def _process(self, item_id: str, item: dict, sem: asyncio.Semaphore):
        async with sem:
            payload: dict = {"id": item_id, "input": item}
            for st in self.spec.stations:
                if self.stop.is_set() or self._over_budget():
                    self.skipped += 1
                    return
                outcome = await run_station(self.client, self.ledger, st, item_id, payload,
                                            should_stop=self._should_stop,
                                            timeout_s=self.spec.call_timeout_s)
                if outcome.halted:
                    self.skipped += 1
                    return
                if outcome.fatal:
                    self._pull_andon(f"fatal API error: {outcome.fatal}")
                if outcome.output is None:
                    self.defective.append({"id": item_id, "failed_at": st.name,
                                           "error": outcome.error, "payload": payload})
                    print(f"  x {item_id}: pulled off line at '{st.name}' ({outcome.error})")
                    return
                payload[st.name] = outcome.output

            insp = self.spec.inspector
            # Per-item sampling so a seeded run inspects the same items regardless of finish order.
            sample = random.Random(f"{self.seed}|{item_id}") if self.seed is not None else self.rng
            if insp and not self._should_stop() and sample.random() < insp.sample_rate:
                verdict = await self._inspect(item_id, payload)
                if verdict is not None:
                    self.inspected += 1
                    payload["_inspection"] = verdict
                    if not verdict["pass"]:
                        self.inspection_failures += 1
                        self.defective.append({"id": item_id, "failed_at": "inspection",
                                               "error": "; ".join(verdict.get("issues", [])),
                                               "payload": payload})
                        print(f"  ! {item_id}: failed inspection")
                        rate = self.inspection_failures / self.inspected
                        if self.inspected >= insp.min_inspected and rate > insp.stop_line_if_defect_rate_above:
                            self._pull_andon(
                                f"inspector defect rate {rate:.0%} > {insp.stop_line_if_defect_rate_above:.0%} "
                                f"({self.inspection_failures}/{self.inspected})"
                            )
                        return

            self.finished.append(payload)
            print(f"  ✓ {item_id}")

    async def run(self, items: list[dict]) -> Path:
        print(f"Running line '{self.spec.name}' on {len(items)} items "
              f"(concurrency {self.spec.concurrency}, budget ${self.spec.budget_usd:.2f})")
        sem = asyncio.Semaphore(self.spec.concurrency)
        ids = [str(it.get("id", i)) for i, it in enumerate(items)]
        try:
            results = await asyncio.gather(*(self._process(i, it, sem) for i, it in zip(ids, items)),
                                           return_exceptions=True)
            for item_id, r in zip(ids, results):
                if isinstance(r, BaseException):  # a bug, not a model failure: record it, keep the run
                    self.defective.append({"id": item_id, "failed_at": "engine",
                                           "error": f"{type(r).__name__}: {r}", "payload": None})
        finally:
            self.ledger.close()
            self._write_outputs(len(items))
        return self.run_dir

    def _write_outputs(self, items_in: int):
        d = self.run_dir
        with open(d / "output.jsonl", "w") as f:
            for p in self.finished:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        with open(d / "defects.jsonl", "w") as f:
            for p in self.defective:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        if self.spec.output_fields and self.finished:
            with open(d / "output.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(self.spec.output_fields)
                for p in self.finished:
                    w.writerow([dig(p, k) for k in self.spec.output_fields])
        summary = {
            "line": self.spec.name,
            "items_in": items_in,
            "finished": len(self.finished),
            "defective": len(self.defective),
            "skipped": self.skipped,
            "inspected": self.inspected,
            "inspection_failures": self.inspection_failures,
            "stopped_reason": self.stopped_reason,
            "total_cost_usd": round(self.ledger.spent, 6),
        }
        (d / "summary.json").write_text(json.dumps(summary, indent=2))
        (d / "report.md").write_text(build_report(d, summary))


def dig(obj, dotted: str):
    """dig(payload, 'score.fit') -> payload['score']['fit']"""
    for k in dotted.split("."):
        if not isinstance(obj, dict) or k not in obj:
            return ""
        obj = obj[k]
    return obj if not isinstance(obj, (dict, list)) else json.dumps(obj, ensure_ascii=False)


def load_items(path: str | Path) -> list[dict]:
    p = Path(path)
    if p.suffix == ".csv":
        with open(p, newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    if p.suffix in (".jsonl", ".ndjson"):
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    if p.suffix == ".json":
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else [data]
    raise ValueError(f"Unsupported input type {p.suffix} (use .csv, .jsonl or .json)")
