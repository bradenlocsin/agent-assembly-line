import asyncio
import json
from pathlib import Path

from assembly_line.client import CallResult, MockClient
from assembly_line.config import load_line
from assembly_line.line import Line, load_items, parse_and_validate
from assembly_line.models import cost_usd

ROOT = Path(__file__).resolve().parent.parent
LEAD_GEN = ROOT / "lines" / "lead_gen"


def test_pricing_haiku_long_prompt_tier():
    short = cost_usd("haiku", 50_000, 1_000)
    long = cost_usd("haiku", 150_000, 1_000)
    assert round(short, 6) == round((50_000 * 0.10 + 1_000 * 0.50) / 1e6, 6)
    assert round(long, 6) == round((150_000 * 0.50 + 1_000 * 2.50) / 1e6, 6)


def test_parse_and_validate():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    assert parse_and_validate('```json\n{"x": 3}\n```', schema) == ({"x": 3}, None)
    out, err = parse_and_validate('{"x": "three"}', schema)
    assert out is None and "schema violation" in err
    out, err = parse_and_validate("no json here", schema)
    assert out is None


def test_lead_gen_line_loads():
    spec = load_line(LEAD_GEN)
    assert [s.name for s in spec.stations] == ["enrich", "score", "draft"]
    assert spec.inspector.model == "opus"


def test_mock_run_end_to_end(tmp_path):
    spec = load_line(LEAD_GEN)
    items = load_items(LEAD_GEN / "sample_input.csv")
    line = Line(spec, MockClient(fail_rate=0.1, seed=1), runs_root=tmp_path, seed=1)
    run_dir = asyncio.run(line.run(items))
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["items_in"] == len(items)
    assert summary["finished"] + summary["defective"] + summary["skipped"] == len(items)
    assert (run_dir / "report.md").exists()
    assert (run_dir / "ledger.jsonl").read_text().strip()


class AlwaysBroken(MockClient):
    async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
        return CallResult(text="garbage", model=alias, input_tokens=10, output_tokens=5)


def test_escalation_then_defect(tmp_path):
    spec = load_line(LEAD_GEN)
    line = Line(spec, AlwaysBroken(), runs_root=tmp_path)
    run_dir = asyncio.run(line.run([{"id": "a", "name": "X"}]))
    entries = [json.loads(l) for l in (run_dir / "ledger.jsonl").read_text().splitlines()]
    # enrich: haiku, haiku retry, then sonnet escalation -> pulled off line
    assert [e["model"] for e in entries] == ["haiku", "haiku", "sonnet"]
    assert entries[-1]["escalated"] is True
    assert json.loads((run_dir / "summary.json").read_text())["defective"] == 1


class InspectorAlwaysFails(MockClient):
    async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
        if schema and "pass" in schema.get("properties", {}):
            return CallResult(text='{"pass": false, "issues": ["bad"]}', model=alias)
        return await super().call(alias, system, user, max_tokens, tools, schema)


def test_andon_cord_stops_line(tmp_path):
    spec = load_line(LEAD_GEN)
    spec.inspector.sample_rate = 1.0
    spec.concurrency = 1
    items = [{"id": str(i), "name": f"Biz {i}"} for i in range(30)]
    line = Line(spec, InspectorAlwaysFails(fail_rate=0.0), runs_root=tmp_path)
    run_dir = asyncio.run(line.run(items))
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["stopped_reason"] and "defect rate" in summary["stopped_reason"]
    assert summary["skipped"] > 0


def test_budget_stops_line(tmp_path):
    spec = load_line(LEAD_GEN)
    spec.budget_usd = 0.0
    line = Line(spec, MockClient(fail_rate=0.0), runs_root=tmp_path)
    run_dir = asyncio.run(line.run([{"id": "1"}, {"id": "2"}]))
    summary = json.loads((run_dir / "summary.json").read_text())
    assert "budget" in summary["stopped_reason"]
