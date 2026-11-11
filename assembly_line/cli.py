"""Command line: `line run | report | new | kaizen | models`."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .client import make_client
from .config import load_line
from .ledger import build_report
from .line import Line, load_items
from .models import MODELS

TEMPLATE_YAML = """name: {name}
# What this line produces, in one sentence:
# TODO

settings:
  concurrency: 5      # items worked on at once
  budget_usd: 1.00    # line stops when spend hits this

stations:
  - name: first_station
    model: haiku          # haiku | sonnet | opus | fable
    escalate_to: sonnet   # stronger model to try if haiku keeps failing
    max_retries: 1
    instructions: prompts/first_station.md
    output_schema:
      type: object
      properties:
        result: {{type: string}}
      required: [result]

inspector:
  model: opus
  instructions: prompts/inspector.md
  sample_rate: 0.2                     # inspect 20% of finished items
  min_inspected: 5                     # don't judge the line on fewer than this
  stop_line_if_defect_rate_above: 0.3  # andon cord

output_fields: [id, first_station.result]
"""

TEMPLATE_STATION = """You are the first station on the {name} line.

Given the work item, do ONE thing: TODO describe the single transformation.

Rules:
- TODO
"""

TEMPLATE_INSPECTOR = """You are the quality inspector for the {name} line.

You receive one finished work item with every station's output.
Mark pass=false if TODO (what counts as a defect).
List concrete issues. Be strict but fair: minor style nits are not defects.
"""


def cmd_run(a):
    spec = load_line(a.line)
    if a.budget is not None:
        spec.budget_usd = a.budget
    if a.concurrency is not None:
        spec.concurrency = a.concurrency
    items = load_items(a.input)
    if a.limit:
        items = items[: a.limit]
    client = make_client(a.mock)
    run_dir = asyncio.run(Line(spec, client, Path(a.runs_dir), seed=a.seed).run(items))
    print()
    print((run_dir / "report.md").read_text())
    print(f"\nOutputs in {run_dir}/")


def cmd_report(a):
    print(build_report(Path(a.run_dir)))


def cmd_new(a):
    d = Path(a.lines_dir) / a.name
    if d.exists():
        sys.exit(f"{d} already exists")
    (d / "prompts").mkdir(parents=True)
    (d / "line.yaml").write_text(TEMPLATE_YAML.format(name=a.name))
    (d / "prompts" / "first_station.md").write_text(TEMPLATE_STATION.format(name=a.name))
    (d / "prompts" / "inspector.md").write_text(TEMPLATE_INSPECTOR.format(name=a.name))
    print(f"Created {d}/ — edit line.yaml and the prompts, then:\n"
          f"  line run {d} --input your_items.csv --mock --limit 5")


KAIZEN_PROMPT = """You are a process engineer doing kaizen on an AI assembly line.
Below are the line definition, the station instructions, and items that failed (defects).
Find the patterns behind the failures and propose specific edits to station
instructions or schemas that would prevent them. Be concrete: quote the line to change
and give the replacement. Also flag any station that should use a different model.
Write in Markdown."""


def cmd_kaizen(a):
    run_dir = Path(a.run_dir)
    spec = load_line(a.line)
    defects = (run_dir / "defects.jsonl").read_text().splitlines()
    if not defects:
        print("No defects in this run. Nothing to improve.")
        return
    stations = "\n\n".join(f"### {s.name} ({s.model})\n{s.instructions}\nSchema: {json.dumps(s.output_schema)}"
                           for s in spec.stations)
    user = (f"# Stations\n{stations}\n\n# Defects ({len(defects)})\n" + "\n".join(defects[:40]))
    if a.mock:
        text = "_Mock mode: run without --mock to get real kaizen suggestions from Opus._"
    else:
        client = make_client(False)
        res = asyncio.run(client.call(a.model, KAIZEN_PROMPT, user, 4000))
        text = res.text
    out = run_dir / "kaizen.md"
    out.write_text(text)
    print(text)
    print(f"\nSaved to {out}")


def cmd_models(a):
    print(f"{'alias':8} {'model id':20} {'in $/M':>8} {'out $/M':>8}")
    for m in MODELS.values():
        print(f"{m.alias:8} {m.id:20} {m.price.input:8.2f} {m.price.output:8.2f}")
    print("(haiku costs $0.50/$2.50 when a prompt exceeds 100K tokens)")


def main(argv=None):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(prog="line", description="AI agent assembly lines")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a line over a file of input items")
    r.add_argument("line", help="path to the line folder, e.g. lines/lead_gen")
    r.add_argument("--input", required=True, help=".csv, .jsonl or .json of items")
    r.add_argument("--mock", action="store_true", help="fake workers, no API key, $0")
    r.add_argument("--limit", type=int, help="only run the first N items")
    r.add_argument("--budget", type=float, help="override budget_usd")
    r.add_argument("--concurrency", type=int, help="override concurrency")
    r.add_argument("--seed", type=int, help="seed for inspector sampling")
    r.add_argument("--runs-dir", default="runs")
    r.set_defaults(fn=cmd_run)

    rp = sub.add_parser("report", help="print the report for a past run")
    rp.add_argument("run_dir")
    rp.set_defaults(fn=cmd_report)

    n = sub.add_parser("new", help="scaffold a new line")
    n.add_argument("name")
    n.add_argument("--lines-dir", default="lines")
    n.set_defaults(fn=cmd_new)

    k = sub.add_parser("kaizen", help="have a strong model study a run's defects and suggest fixes")
    k.add_argument("line")
    k.add_argument("run_dir")
    k.add_argument("--model", default="opus")
    k.add_argument("--mock", action="store_true")
    k.set_defaults(fn=cmd_kaizen)

    m = sub.add_parser("models", help="show models and prices")
    m.set_defaults(fn=cmd_models)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
