"""Failure-mode tests: what the line does when workers misbehave. All mocked, $0."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from assembly_line.client import CallResult, MockClient
from assembly_line.config import load_line
from assembly_line.line import Line, parse_and_validate, run_station
from assembly_line.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
LEAD_GEN = ROOT / "lines" / "lead_gen"


def run(spec, client, items, tmp_path, **kw):
    rd = asyncio.run(Line(spec, client, runs_root=tmp_path, **kw).run(items))
    return rd, json.loads((rd / "summary.json").read_text())


def ledger(rd):
    return [json.loads(l) for l in (rd / "ledger.jsonl").read_text().splitlines()]


class Scripted(MockClient):
    """Returns a scripted sequence of responses per station call, then valid fakes."""
    def __init__(self, script):
        super().__init__(fail_rate=0.0, seed=0)
        self.script = list(script)
        self.calls = []

    async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
        self.calls.append(alias)
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, BaseException):
                raise r
            if callable(r):
                return await r(alias)
            return CallResult(text=r, model=alias, input_tokens=100, output_tokens=10)
        return await super().call(alias, system, user, max_tokens, tools, schema)


# ---------- malformed output ----------

@pytest.mark.parametrize("bad", [
    "", "not json at all", '{"what_they_do": "x"', "[]",
    '{"what_they_do": 5, "online_booking": "yes", "chat_or_text_widget": "no", "review_presence": "some", "signals": []}',
    '{"what_they_do": "x", "online_booking": "maybe", "chat_or_text_widget": "no", "review_presence": "some", "signals": []}',
])
def test_malformed_output_is_retried_then_recovered(tmp_path, bad):
    spec = load_line(LEAD_GEN)
    client = Scripted([bad])  # first enrich attempt bad, everything after valid
    rd, s = run(spec, client, [{"id": "1"}], tmp_path)
    e = ledger(rd)
    assert e[0]["ok"] is False and e[0]["error"]
    assert e[1]["ok"] is True and e[1]["model"] == "haiku" and not e[1]["escalated"]
    assert s["finished"] + s["defective"] == 1  # defective only if inspector sampled & failed it


def test_retry_feedback_includes_error():
    spec = load_line(LEAD_GEN)
    seen = []

    class Spy(Scripted):
        async def call(self, alias, system, user, *a, **k):
            seen.append(user)
            return await super().call(alias, system, user, *a, **k)

    async def go(tmp):
        led = Ledger(tmp)
        await run_station(Spy(["nope"]), led, spec.stations[1], "1", {"id": "1"})
    import tempfile
    asyncio.run(go(Path(tempfile.mkdtemp())))
    assert "rejected" not in seen[0] and "rejected" in seen[1]


def test_trailing_prose_with_braces_parses():
    sch = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    assert parse_and_validate('{"a": 1}\nNote: {see above}', sch) == ({"a": 1}, None)


def test_escalation_recovers(tmp_path):
    spec = load_line(LEAD_GEN)
    client = Scripted(["bad", "bad"])  # haiku twice, then sonnet (valid)
    rd, s = run(spec, client, [{"id": "1"}], tmp_path)
    e = ledger(rd)
    assert [x["model"] for x in e[:3]] == ["haiku", "haiku", "sonnet"]
    assert e[2]["ok"] and e[2]["escalated"]


def test_repeated_failure_pulls_item_and_line_continues(tmp_path):
    spec = load_line(LEAD_GEN)
    spec.concurrency = 1
    client = Scripted(["bad", "bad", "bad"])  # item 1 dies at enrich; item 2 fine
    rd, s = run(spec, client, [{"id": "1"}, {"id": "2"}], tmp_path)
    defects = [json.loads(l) for l in (rd / "defects.jsonl").read_text().splitlines()]
    assert defects[0]["id"] == "1" and defects[0]["failed_at"] == "enrich"
    assert s["finished"] + s["defective"] == 2 and s["stopped_reason"] is None


def test_max_tokens_cutoff_is_explained(tmp_path):
    spec = load_line(LEAD_GEN)

    async def truncated(alias):
        return CallResult(text='{"what_they_do": "cut', model=alias, extra={"stop_reason": "max_tokens"})
    rd, _ = run(spec, Scripted([truncated]), [{"id": "1"}], tmp_path)
    assert "max_tokens" in ledger(rd)[0]["error"]


# ---------- exceptions and timeouts ----------

def test_transient_exception_is_retried(tmp_path):
    spec = load_line(LEAD_GEN)
    rd, s = run(spec, Scripted([ConnectionError("reset")]), [{"id": "1"}], tmp_path)
    e = ledger(rd)
    assert e[0]["ok"] is False and "ConnectionError" in e[0]["error"] and e[0]["cost_usd"] == 0
    assert e[1]["ok"] is True


def test_real_timeout(tmp_path):
    spec = load_line(LEAD_GEN)
    spec.call_timeout_s = 0.05

    async def hang(alias):
        await asyncio.sleep(5)
    rd, s = run(spec, Scripted([hang]), [{"id": "1"}], tmp_path)
    e = ledger(rd)
    assert "timed out" in e[0]["error"] and e[1]["ok"]


def test_every_call_raises_run_still_completes(tmp_path):
    class Dead(MockClient):
        async def call(self, *a, **k):
            raise asyncio.TimeoutError()
    spec = load_line(LEAD_GEN)
    rd, s = run(spec, Dead(), [{"id": str(i)} for i in range(4)], tmp_path)
    assert s["defective"] == 4
    assert (rd / "report.md").exists() and (rd / "output.jsonl").exists()


def test_fatal_api_error_stops_line(tmp_path):
    class AuthenticationError(Exception):
        pass

    class BadKey(MockClient):
        async def call(self, *a, **k):
            raise AuthenticationError("invalid x-api-key")
    spec = load_line(LEAD_GEN)
    spec.concurrency = 1
    rd, s = run(spec, BadKey(), [{"id": str(i)} for i in range(10)], tmp_path)
    assert "fatal" in s["stopped_reason"]
    assert len(ledger(rd)) == 1  # no retries, no other items attempted
    assert s["skipped"] == 9


def test_engine_bug_in_one_item_doesnt_kill_run(tmp_path, monkeypatch):
    spec = load_line(LEAD_GEN)
    line = Line(spec, MockClient(0.0, seed=1), runs_root=tmp_path)
    orig = line._process

    async def flaky(item_id, item, sem):
        if item_id == "boom":
            raise RuntimeError("engine bug")
        return await orig(item_id, item, sem)
    monkeypatch.setattr(line, "_process", flaky)
    rd = asyncio.run(line.run([{"id": "boom"}, {"id": "ok"}]))
    s = json.loads((rd / "summary.json").read_text())
    defects = [json.loads(l) for l in (rd / "defects.jsonl").read_text().splitlines()]
    assert any(d["failed_at"] == "engine" for d in defects)
    assert s["finished"] + s["defective"] == 2


# ---------- budget ----------

def test_budget_checked_between_retries(tmp_path):
    """One item, one station, 21 attempts at ~$0.01 each, $0.05 budget.
    Old engine only checked between stations, so it spent all 21 (~$0.21)."""
    class Bad(MockClient):
        async def call(self, alias, *a, **k):
            return CallResult(text="bad", model=alias, input_tokens=99_999, output_tokens=0)
    spec = load_line(LEAD_GEN)
    spec.stations[0].max_retries = 20
    spec.stations[0].escalate_to = None
    spec.budget_usd = 0.05
    rd, s = run(spec, Bad(), [{"id": "1"}], tmp_path)
    one_call = ledger(rd)[0]["cost_usd"]
    assert "budget" in s["stopped_reason"]
    assert s["total_cost_usd"] <= spec.budget_usd + one_call + 1e-9
    assert s["skipped"] == 1 and s["defective"] == 0  # halted by budget, not a quality defect


def test_budget_zero_does_no_work(tmp_path):
    spec = load_line(LEAD_GEN)
    spec.budget_usd = 0.0
    rd, s = run(spec, MockClient(0.0), [{"id": "1"}, {"id": "2"}], tmp_path)
    assert s["skipped"] == 2 and not ledger(rd)


# ---------- inspector ----------

def test_unparseable_inspector_verdict_not_counted(tmp_path):
    class BadInspector(MockClient):
        async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
            if schema and "pass" in schema.get("properties", {}):
                return CallResult(text="looks fine to me!", model=alias)
            return await super().call(alias, system, user, max_tokens, tools, schema)
    spec = load_line(LEAD_GEN)
    spec.inspector.sample_rate = 1.0
    rd, s = run(spec, BadInspector(0.0, seed=1), [{"id": "1"}], tmp_path)
    assert s["inspected"] == 0 and s["finished"] == 1


# ---------- run hygiene ----------

def test_runs_never_share_a_folder(tmp_path):
    spec = load_line(LEAD_GEN)
    dirs = {run(spec, MockClient(0.0, seed=1), [{"id": str(i)}], tmp_path)[0] for i in range(3)}
    assert len(dirs) == 3


def test_seeded_mock_runs_are_reproducible(tmp_path):
    spec = load_line(LEAD_GEN)
    items = [{"id": str(i), "name": f"b{i}"} for i in range(20)]
    out = []
    for i in range(2):
        rd, s = run(spec, MockClient(0.3, seed=42), items, tmp_path / str(i), seed=42)
        out.append((s, [(e["item_id"], e["station"], e["model"], e["ok"]) for e in sorted(
            ledger(rd), key=lambda e: (e["item_id"], e["station"], e["attempt"]))]))
    assert out[0] == out[1]


def test_missing_prompt_file_is_an_error(tmp_path):
    import shutil
    d = tmp_path / "line"
    shutil.copytree(LEAD_GEN, d)
    (d / "prompts" / "score.md").unlink()
    with pytest.raises(FileNotFoundError):
        load_line(d)


def test_env_web_search_version_read_at_call_time(monkeypatch):
    from assembly_line import client
    monkeypatch.setenv("WEB_SEARCH_TOOL_VERSION", "web_search_test")
    assert client.web_search_tool()["type"] == "web_search_test"


# ---------- CLI smoke (all mock) ----------

def cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "assembly_line", *args], cwd=cwd,
                          capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "PATH": ""})


def test_cli_end_to_end(tmp_path):
    r = cli("run", str(LEAD_GEN), "--input", str(LEAD_GEN / "sample_input.csv"), "--mock",
            "--seed", "1", "--runs-dir", str(tmp_path / "runs"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    rd = next((tmp_path / "runs").iterdir())
    for f in ["output.jsonl", "output.csv", "defects.jsonl", "ledger.jsonl", "report.md", "summary.json"]:
        assert (rd / f).exists(), f
    assert cli("report", str(rd), cwd=tmp_path).returncode == 0
    assert cli("kaizen", str(LEAD_GEN), str(rd), "--mock", cwd=tmp_path).returncode == 0
    assert cli("models", cwd=tmp_path).returncode == 0
    assert cli("new", "demo", "--lines-dir", str(tmp_path / "lines"), cwd=tmp_path).returncode == 0
    r = cli("run", str(tmp_path / "lines" / "demo"), "--input", str(LEAD_GEN / "sample_input.csv"),
            "--mock", "--limit", "3", "--runs-dir", str(tmp_path / "runs2"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr


def test_cli_without_key_fails_cleanly(tmp_path):
    r = cli("run", str(LEAD_GEN), "--input", str(LEAD_GEN / "sample_input.csv"), cwd=tmp_path)
    assert r.returncode != 0 and "ANTHROPIC_API_KEY" in (r.stderr + r.stdout)
    assert "Traceback" not in r.stderr
