"""Tests for the blind quality experiment. All free: mock workers or a fake HTTP layer."""
import asyncio
import csv
import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("exp", ROOT / "experiments" / "quality" / "exp.py")
exp = importlib.util.module_from_spec(_spec)
sys.modules["exp"] = exp
_spec.loader.exec_module(exp)

from assembly_line.client import CallResult, ClaudeClient, MockClient  # noqa: E402
from assembly_line.models import cost_usd  # noqa: E402


class Spy(MockClient):
    def __init__(self, fail_rate=0.0, seed=1):
        super().__init__(fail_rate, seed)
        self.prompts = []

    async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
        self.prompts.append((alias, system, user, tools))
        return await super().call(alias, system, user, max_tokens, tools, schema)


def mock_run(tmp_path, n=3, ids=None, client=None, budget=10.0, inspector="none", seed=1):
    leads = exp.load_leads(n, ids)
    specs = exp.build_workflows(inspector)
    client = client or MockClient(0.0, seed)
    out = tmp_path / "run"
    exp.run_experiment(specs, leads, client, out, "MOCK", budget, seed, inspector)
    exp.make_blind(out, seed)
    return out, leads


# ---------------- dataset and fairness ----------------

def test_dataset_shape_and_separation():
    leads = exp.load_leads()
    gold = exp.load_gold()
    assert len(leads) == 20 and len({l["id"] for l in leads}) == 20
    assert set(gold) == {l["id"] for l in leads}
    for l in leads:
        assert set(l) == {"id", "business", "evidence"}  # nothing gold-ish in model-visible data
    text = (ROOT / "experiments/quality/data/leads.jsonl").read_text()
    for field in ["acceptable_offers", "fit_range", "traps", "owner_name"]:
        assert field not in text


def test_pilot_set_is_fixed():
    assert [l["id"] for l in exp.load_leads(3)] == ["L01", "L12", "L20"]


def test_gold_never_reaches_any_prompt(tmp_path):
    spy = Spy()
    mock_run(tmp_path, client=spy, inspector="all")
    blob = "\n".join(s + u for _, s, u, _ in spy.prompts)
    gold = exp.load_gold()
    for g in gold.values():
        for t in g["traps"]:
            assert t not in blob
    for word in ["acceptable_offers", "fit_range", "traps", "gold"]:
        assert word not in blob


def test_all_workflows_get_identical_information(tmp_path):
    spy = Spy()
    specs = exp.build_workflows()
    leads = exp.load_leads(3)
    for wf, spec in specs.items():
        spy.prompts.clear()
        asyncio.run(exp.Line(spec, spy, runs_root=tmp_path / wf, seed=1).run(leads))
        sys_all = "\n".join(s for _, s, _, _ in spy.prompts)
        for part in ["context", "enrich", "score", "draft"]:
            assert exp._spec_text(part) in sys_all, (wf, part)  # same task text, every arm
        for lead in leads:
            users = [u for _, _, u, _ in spy.prompts if f'"id": "{lead["id"]}"' in u]
            assert users, (wf, lead["id"])
            for ev in lead["evidence"]:
                assert all(json.dumps(ev["text"], ensure_ascii=False)[1:-1] in u for u in users)  # full evidence, every call
        assert all(not t for *_, t in spy.prompts)  # no web search for anyone


def test_schemas_and_retry_policy_match():
    specs = exp.build_workflows()
    line = specs["assembly_line"].stations
    for wf in ["single_haiku", "single_sonnet"]:
        st = specs[wf].stations[0]
        assert st.output_schema["properties"] == {s.name: s.output_schema for s in line}
        assert st.max_retries == 1 and st.escalate_to
    assert all(s.max_retries == 1 and s.escalate_to for s in line)
    assert [(s.model, s.escalate_to) for s in line] == [("haiku", "sonnet"), ("haiku", "sonnet"), ("sonnet", "opus")]


def test_same_leads_through_all_three(tmp_path):
    out, leads = mock_run(tmp_path)
    rows = exp.load_jsonl(out / "results.jsonl")
    assert len(rows) == 3 * len(leads)
    for wf in exp.WORKFLOWS:
        rs = [r for r in rows if r["workflow"] == wf]
        assert sorted(r["lead"] for r in rs) == sorted(l["id"] for l in leads)
        assert all(r["status"] == "complete" and set(r["output"]) >= {"subject", "email_body", "facts"} for r in rs)


# ---------------- blinding ----------------

LEAK = re.compile(r"\b(haiku|sonnet|opus|fable|single_\w+|assembly_line|assembly line|workflow|station)\b", re.I)


def test_blind_packet_has_no_identity_leaks(tmp_path):
    out, _ = mock_run(tmp_path)
    for f in ["review.md", "review_sheet.csv"]:
        text = (out / f).read_text()
        assert not LEAK.search(text), LEAK.search(text)
    for c in ["cost", "latency", "model", "calls"]:
        assert c not in (out / "review_sheet.csv").read_text().splitlines()[0]


def test_key_maps_back_to_the_right_outputs(tmp_path):
    out, _ = mock_run(tmp_path, ids=["L01", "L05", "L14"])
    key = json.loads((out / "KEY_sealed.json").read_text())["key"]
    md = (out / "review.md").read_text()
    rows = {(r["workflow"], r["lead"]): r for r in exp.load_jsonl(out / "results.jsonl")}
    assert len(key) == 9 and all(len(k) == 6 for k in key)
    for rid, v in key.items():
        section = md.split(f"### Response {rid}")[1].split("###")[0]
        o = rows[(v["workflow"], v["lead"])]["output"]
        assert f"fit_score: {o['fit_score']}" in section and o["email_body"].strip() in section


def test_order_is_randomized_and_reproducible(tmp_path):
    out, _ = mock_run(tmp_path, n=20)
    key1 = json.loads((out / "KEY_sealed.json").read_text())["key"]
    first = {}
    for lead_block in (out / "review.md").read_text().split("## Lead ")[1:]:
        rid = re.search(r"### Response (\w+)", lead_block).group(1)
        first[key1[rid]["workflow"]] = first.get(key1[rid]["workflow"], 0) + 1
    assert len(first) == 3, first  # every arm shows up first for some lead
    assert exp.make_blind(out, 1) == key1  # same seed, same packet
    assert exp.make_blind(out, 2) != key1  # different seed, different IDs


# ---------------- scoring ----------------

def write_reviews(out, path, scorer):
    key = json.loads((out / "KEY_sealed.json").read_text())["key"]
    rows = list(csv.DictReader(open(out / "review_sheet.csv")))
    for r in rows:
        r.update(scorer(key[r["review_id"]]["workflow"], r))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=exp.SHEET_COLS)
        w.writeheader()
        w.writerows(rows)
    return path


def test_scoring_math(tmp_path):
    out, leads = mock_run(tmp_path, n=20)
    vals = {"assembly_line": 5, "single_sonnet": 4, "single_haiku": 2}

    def scorer(wf, r):
        return {**{c: vals[wf] for c in exp.CRITERIA}, "unsupported_claims_count": 0,
                "would_send_yes_no": "yes" if wf != "single_haiku" else "no",
                "rank_within_lead": {"assembly_line": 1, "single_sonnet": 2, "single_haiku": 3}[wf]}
    f = write_reviews(out, tmp_path / "r.csv", scorer)
    report = exp.score(out, [f])
    scores = list(csv.DictReader(open(out / "scores.csv")))
    for wf, v in vals.items():
        assert all(float(s["human_overall"]) == v for s in scores if s["workflow"] == wf)
    rows = exp.load_jsonl(out / "results.jsonl")
    line_cost = sum(r["cost_usd"] for r in rows if r["workflow"] == "assembly_line")
    assert f"${line_cost / 20:.4f}" in report  # cost per accepted = total / 20 accepted
    assert "no accepted outputs" in report  # single_haiku: 0 accepted
    assert "| line vs single_haiku | 20 | 20 / 0 / 0 | +3.00" in report
    assert "0.000 |" in report  # sign test p for 20/0


def test_incomplete_outputs_count_as_not_accepted(tmp_path):
    class HaikuSinglesBroken(MockClient):
        async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
            if "Complete all three steps" in system:
                return CallResult(text="garbage", model=alias)
            return await super().call(alias, system, user, max_tokens, tools, schema)
    out, _ = mock_run(tmp_path, client=HaikuSinglesBroken(0.0, 1))
    rows = exp.load_jsonl(out / "results.jsonl")
    assert all(r["status"] == "failed" for r in rows if r["workflow"].startswith("single_"))
    key = json.loads((out / "KEY_sealed.json").read_text())["key"]
    assert {v["workflow"] for v in key.values()} == {"assembly_line"}  # failed outputs not in packet
    f = write_reviews(out, tmp_path / "r.csv", lambda wf, r: {**{c: 4 for c in exp.CRITERIA},
                      "would_send_yes_no": "yes", "rank_within_lead": 1, "unsupported_claims_count": 0})
    report = exp.score(out, [f])
    assert "| single_haiku | 0/3 |" in report


def test_stats_helpers():
    assert math.isclose(exp.sign_test(15, 5), 0.0414, abs_tol=5e-4)
    assert exp.sign_test(3, 0) == 0.25 and exp.sign_test(0, 0) == 1.0
    assert all(math.isnan(x) for x in exp.bootstrap_ci([1, 2, 3]))
    lo, hi = exp.bootstrap_ci(list(range(20)))
    assert lo < 9.5 < hi


def test_auto_checks():
    leads = {l["id"]: l for l in exp.load_leads()}
    gold = exp.load_gold()
    base = {"facts": {"online_booking": "no", "chat_or_text_widget": "unknown", "review_presence": "weak"},
            "fit_score": 10, "best_offer": "booking_automation", "subject": "Quick idea"}
    a = exp.auto_checks({**base, "email_body": "Hi Mike, you get 37% more bookings... Braden"}, leads["L05"], gold["L05"])
    assert a["greeting_name_unsupported"] and a["unsupported_numbers"] == ["37"] and a["facts_correct_of_3"] == 3
    a = exp.auto_checks({**base, "email_body": "Hi Marcus, 212 reviews is great. Braden"}, leads["L01"], gold["L01"])
    assert not a["greeting_name_unsupported"] and a["unsupported_numbers"] == []
    a = exp.auto_checks({**base, "fit_score": 1, "best_offer": "none", "email_body": "NO_EMAIL"}, leads["L14"], gold["L14"])
    assert a["closed_handled"] and a["fit_in_range"] and a["offer_acceptable"]


def test_synthetic_and_mock_banners(tmp_path):
    out, _ = mock_run(tmp_path)
    f = exp.simulate_review(out, 1)
    report = exp.score(out, [f])
    assert "MOCK RUN" in report and "SYNTHETIC REVIEWS" in report and "not evaluable" in report
    assert "n = 3 leads" in report


def test_exports_exist(tmp_path):
    out, _ = mock_run(tmp_path)
    exp.score(out, [exp.simulate_review(out, 1)])
    for f in ["results.jsonl", "manifest.json", "review.md", "review_sheet.csv", "KEY_sealed.json",
              "report.md", "scores.csv"]:
        assert (out / f).exists(), f
    m = json.loads((out / "manifest.json").read_text())
    assert m["mode"] == "MOCK" and sorted(m["workflow_order"]) == sorted(exp.WORKFLOWS)


# ---------------- cost estimate and budget ----------------

def test_estimate_formula_by_hand():
    st = exp.StationSpec(name="x", model="haiku", instructions="I" * 300, output_schema={"type": "object"},
                         max_retries=0, max_tokens=100)
    spec = exp.LineSpec(name="t", path=ROOT, stations=[st], inspector=None)
    lead = exp.load_leads(ids=["L01"])
    est = exp.estimate({"t": spec}, lead, budget_per_workflow=1.0)["t"]
    chars = len(exp.system_prompt(st.instructions, st.output_schema)) + exp.item_payload_chars(lead[0]) + 300
    want = cost_usd("haiku", math.ceil(chars / 3), 100)
    assert math.isclose(est["worst_case"], want) and math.isclose(est["max_single_call"], want)
    assert est["ceiling"] == min(want, 1.0 + want)


def test_estimate_bounds_actual_mock_spend_even_when_everything_fails(tmp_path):
    specs = exp.build_workflows()
    leads = exp.load_leads(3)
    est = exp.estimate(specs, leads, 100.0)

    class AllFail(MockClient):
        async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
            # bill the full max_tokens of output on every failed attempt
            r = await super().call(alias, system, user, max_tokens, tools, schema)
            return CallResult(text="garbage", model=alias, input_tokens=r.input_tokens, output_tokens=max_tokens)
    out = tmp_path / "run"
    exp.run_experiment(specs, leads, AllFail(0.0, 1), out, "MOCK", 100.0, 1, "none")
    m = json.loads((out / "manifest.json").read_text())
    for wf in exp.WORKFLOWS:
        assert m["workflow_cost_usd"][wf] <= est[wf]["worst_case"] + 1e-9, wf


def test_budget_respected_in_experiment(tmp_path):
    out, _ = mock_run(tmp_path, budget=0.0)
    rows = exp.load_jsonl(out / "results.jsonl")
    assert all(r["status"] == "not_run" and r["cost_usd"] == 0 for r in rows)


def test_inspector_all_never_removes_outputs(tmp_path):
    class Harsh(MockClient):
        async def call(self, alias, system, user, max_tokens, tools=None, schema=None):
            if schema and "pass" in schema.get("properties", {}):
                return CallResult(text='{"pass": false, "issues": ["x"]}', model=alias)
            return await super().call(alias, system, user, max_tokens, tools, schema)
    out, _ = mock_run(tmp_path, client=Harsh(0.0, 1), inspector="all")
    rows = exp.load_jsonl(out / "results.jsonl")
    assert all(r["status"] == "complete" and r["inspector_verdict"]["pass"] is False for r in rows)


def test_fatal_error_aborts_experiment(tmp_path):
    class AuthenticationError(Exception):
        pass

    class BadKey(MockClient):
        async def call(self, *a, **k):
            raise AuthenticationError("invalid x-api-key")
    out, _ = mock_run(tmp_path, client=BadKey(0.0, 1))
    m = json.loads((out / "manifest.json").read_text())
    assert "fatal" in m["aborted"] and len(m["workflow_cost_usd"]) == 1


# ---------------- paid-call gates ----------------

@pytest.fixture
def no_real_client(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real client was created")
    monkeypatch.setattr(exp, "make_real_client", boom)
    monkeypatch.setattr(exp, "HERE", exp.HERE)  # runs dir stays in repo; cleaned by test
    return boom


def test_mode_must_be_explicit():
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--leads", "3"])
    assert e.value.code == 2


def test_real_refused_without_key(monkeypatch, no_real_client):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "dotenv", None)  # don't read a developer's .env in tests
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--real", "--leads", "3", "--approve-max-usd", "99"])
    assert "ANTHROPIC_API_KEY" in str(e.value) and "Not falling back" in str(e.value)


def test_real_refused_when_approval_below_ceiling(monkeypatch, no_real_client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--real", "--leads", "3", "--approve-max-usd", "0.01"])
    assert "hard ceiling" in str(e.value)
    with pytest.raises(SystemExit):
        exp.main(["run", "--real", "--leads", "3"])  # no approval at all


def test_real_refused_without_confirmation(monkeypatch, no_real_client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--real", "--leads", "3", "--approve-max-usd", "5"])
    assert "no terminal" in str(e.value)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "yes")  # anything but SPEND cancels
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--real", "--leads", "3", "--approve-max-usd", "5"])
    assert "Nothing was spent" in str(e.value)


def test_mock_never_creates_real_client(monkeypatch, no_real_client, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # even with a key present
    monkeypatch.setattr(exp, "HERE", tmp_path)
    (tmp_path / "data").symlink_to(ROOT / "experiments/quality/data")
    (tmp_path / "spec").symlink_to(ROOT / "experiments/quality/spec")
    out = exp.main(["run", "--mock", "--leads", "3"])
    assert json.loads((out / "manifest.json").read_text())["mode"] == "MOCK" and "MOCK" in out.name


# ---------------- real SDK wire format, no network ----------------

def _httpx():
    """The SDK's own HTTP library (httpx2 in SDK 2.x, httpx before that)."""
    try:
        import httpx2 as hx
    except ImportError:
        import httpx as hx
    return hx


def fake_sdk(handler):
    import anthropic
    hx = _httpx()
    return anthropic.AsyncAnthropic(api_key="test-key", max_retries=0,
                                    http_client=hx.AsyncClient(transport=hx.MockTransport(handler)))


def test_sdk_request_and_response_shapes():
    httpx = _httpx()
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "thinking", "thinking": "hmm", "signature": "sig"},
                        {"type": "text", "text": '{"a": 1}'}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 30, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 50, "server_tool_use": {"web_search_requests": 2}}})

    client = ClaudeClient(api=fake_sdk(handler))
    r = asyncio.run(client.call("haiku", "SYS", "USER", 500))
    b = seen["body"]
    assert seen["url"].endswith("/v1/messages")
    assert b["model"] == "claude-haiku-5-5" and b["max_tokens"] == 500
    assert b["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert b["messages"] == [{"role": "user", "content": "USER"}] and "tools" not in b
    assert r.text == '{"a": 1}'  # thinking block ignored
    assert (r.input_tokens, r.output_tokens, r.cache_read, r.web_searches) == (120, 30, 50, 2)
    assert r.extra["stop_reason"] == "end_turn"

    asyncio.run(client.call("sonnet", "S", "U", 10, tools=["web_search"]))
    assert seen["body"]["tools"][0]["name"] == "web_search" and seen["body"]["model"] == "claude-sonnet-5-5"


def test_sdk_auth_error_is_fatal(tmp_path):
    httpx = _httpx()

    def handler(request):
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                     "message": "invalid x-api-key"}})
    client = ClaudeClient(api=fake_sdk(handler))
    spec = exp.build_workflows()["assembly_line"]
    rd = asyncio.run(exp.Line(spec, client, runs_root=tmp_path, seed=1).run(exp.load_leads(3)))
    s = json.loads((rd / "summary.json").read_text())
    assert "AuthenticationError" in s["stopped_reason"]
    assert len((rd / "ledger.jsonl").read_text().splitlines()) == 1  # one call, then stop
