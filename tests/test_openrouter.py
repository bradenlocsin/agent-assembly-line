"""OpenRouter provider tests. The real Anthropic SDK runs against a fake HTTP layer: no network, $0."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from assembly_line import openrouter
from assembly_line.client import ClaudeClient, OPENROUTER_ROUTING
from assembly_line.config import load_line
from assembly_line.line import Line

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-or-v1-TESTSECRET-0123456789abcdef"
ANTHROPIC_SECRET = "sk-ant-TESTSECRET-should-never-reach-openrouter"

_spec = importlib.util.spec_from_file_location("exp_or", ROOT / "experiments" / "quality" / "exp.py")
exp = importlib.util.module_from_spec(_spec)
sys.modules["exp_or"] = exp
_spec.loader.exec_module(exp)


def hx():
    try:
        import httpx2 as m
    except ImportError:
        import httpx as m
    return m


def ok_body(text='{"a": 1}', model="anthropic/claude-haiku-5.5", cost=0.0021, input_tokens=900, output_tokens=120):
    return {"id": "gen-1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "cost": cost, "is_byok": False}}


def err_body(status, etype, msg, **meta):
    return {"type": "error", "error": {"type": etype, "message": msg, **({"metadata": meta} if meta else {})},
            "request_id": None}


class FakeOpenRouter:
    """Scripted responses; records every request."""
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request):
        self.requests.append({"url": str(request.url), "headers": {k.lower(): v for k, v in request.headers.items()},
                              "body": json.loads(request.content)})
        status, body = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return hx().Response(status, json=body)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)  # present, must not leak to OpenRouter


def or_client(fake):
    m = hx()
    return ClaudeClient(provider="openrouter", max_attempts=0,
                        http_client=m.AsyncClient(transport=m.MockTransport(fake)))


# ---------------- request / response compatibility ----------------

def test_request_goes_to_openrouter_with_bearer_and_pinned_models(keys):
    fake = FakeOpenRouter([(200, ok_body())])
    c = or_client(fake)
    r = asyncio.run(c.call("haiku", "SYS", "USER", 300))
    req = fake.requests[0]
    assert req["url"] == "https://openrouter.ai/api/v1/messages"
    assert req["headers"]["authorization"] == f"Bearer {SECRET}"
    assert "x-api-key" not in req["headers"]  # the Anthropic key is never sent to OpenRouter
    assert ANTHROPIC_SECRET not in json.dumps(req)
    b = req["body"]
    assert b["model"] == "anthropic/claude-haiku-5.5" and b["max_tokens"] == 300
    assert b["provider"] == OPENROUTER_ROUTING["provider"]  # served by Anthropic, no fallbacks
    assert b["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert r.text == '{"a": 1}' and (r.input_tokens, r.output_tokens) == (900, 120)
    assert r.extra["billed_cost_usd"] == 0.0021 and r.extra["served_model"] == "anthropic/claude-haiku-5.5"


@pytest.mark.parametrize("alias,mid", [("sonnet", "anthropic/claude-sonnet-5.5"), ("opus", "anthropic/claude-opus-5.5")])
def test_model_ids(keys, alias, mid):
    fake = FakeOpenRouter([(200, ok_body(model=mid))])
    asyncio.run(or_client(fake).call(alias, "S", "U", 10))
    assert fake.requests[0]["body"]["model"] == mid


def test_unverified_model_refused_before_any_request(keys, tmp_path):
    fake = FakeOpenRouter([(200, ok_body())])
    spec = load_line(ROOT / "lines" / "lead_gen")
    spec.stations = [spec.stations[0]]
    spec.stations[0].model, spec.stations[0].escalate_to = "fable", None
    rd = asyncio.run(Line(spec, or_client(fake), runs_root=tmp_path).run([{"id": "1"}]))
    s = json.loads((rd / "summary.json").read_text())
    assert "fatal" in s["stopped_reason"] and "fable" in s["stopped_reason"] and fake.requests == []


def test_anthropic_direct_path_unchanged(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)
    seen = {}

    def h(request):
        seen["url"], seen["h"], seen["b"] = str(request.url), {k.lower(): v for k, v in request.headers.items()}, json.loads(request.content)
        return hx().Response(200, json=ok_body(model="claude-haiku-5-5", cost=None))
    m = hx()
    c = ClaudeClient(provider="anthropic", max_attempts=0, http_client=m.AsyncClient(transport=m.MockTransport(h)))
    r = asyncio.run(c.call("haiku", "S", "U", 10))
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    # SDK 2.x sends the Anthropic key as a Bearer token; older SDKs used x-api-key.
    assert ANTHROPIC_SECRET in (seen["h"].get("x-api-key", ""), seen["h"].get("authorization", "").removeprefix("Bearer "))
    assert "provider" not in seen["b"]
    assert seen["b"]["model"] == "claude-haiku-5-5" and r.extra["billed_cost_usd"] is None


# ---------------- ledger uses billed cost ----------------

def run_lead_gen(fake, tmp_path, budget=10.0, items=1):
    spec = load_line(ROOT / "lines" / "lead_gen")
    spec.inspector = None
    spec.stations = [s for s in spec.stations if s.name == "score"]  # one cheap station
    spec.budget_usd = budget
    spec.concurrency = 1
    rd = asyncio.run(Line(spec, or_client(fake), runs_root=tmp_path).run([{"id": str(i)} for i in range(items)]))
    led = [json.loads(l) for l in (rd / "ledger.jsonl").read_text().splitlines()]
    return rd, json.loads((rd / "summary.json").read_text()), led


SCORE_OK = json.dumps({"fit_score": 7, "best_offer": "review_requests", "reasoning": "x"})


def test_ledger_records_billed_cost(keys, tmp_path):
    fake = FakeOpenRouter([(200, ok_body(text=SCORE_OK, cost=0.0123))])
    _, s, led = run_lead_gen(fake, tmp_path)
    assert led[0]["cost_source"] == "billed" and led[0]["cost_usd"] == 0.0123
    assert led[0]["estimated_cost_usd"] > 0 and s["total_cost_usd"] == 0.0123


def test_budget_enforced_on_billed_cost(keys, tmp_path):
    fake = FakeOpenRouter([(200, ok_body(text=SCORE_OK, cost=0.06))])
    _, s, led = run_lead_gen(fake, tmp_path, budget=0.10, items=10)
    assert "budget" in s["stopped_reason"]
    assert len(led) == 2 and s["total_cost_usd"] <= 0.10 + 0.06 + 1e-9  # at most one call past the line
    assert s["skipped"] == 8


# ---------------- errors ----------------

def test_bad_key_401_is_fatal(keys, tmp_path):
    fake = FakeOpenRouter([(401, err_body(401, "authentication_error", "Invalid credentials"))])
    _, s, led = run_lead_gen(fake, tmp_path, items=5)
    assert "fatal" in s["stopped_reason"] and len(fake.requests) == 1 and s["skipped"] == 4


def test_insufficient_credits_402_is_fatal(keys, tmp_path):
    fake = FakeOpenRouter([(402, err_body(402, "billing_error", "Insufficient credits. Add more using https://openrouter.ai/credits"))])
    _, s, led = run_lead_gen(fake, tmp_path, items=5)
    assert "402 insufficient credits" in s["stopped_reason"] and len(fake.requests) == 1


def test_key_spending_limit_403_is_fatal(keys, tmp_path):
    fake = FakeOpenRouter([(403, err_body(403, "permission_error", "Key limit exceeded",
                                          limit_source="openrouter_key_limit"))])
    _, s, _ = run_lead_gen(fake, tmp_path, items=5)
    assert "fatal" in s["stopped_reason"] and len(fake.requests) == 1


def test_rate_limit_is_retried_not_fatal(keys, tmp_path):
    fake = FakeOpenRouter([(429, err_body(429, "rate_limit_error", "slow down")),
                           (200, ok_body(text=SCORE_OK, cost=0.001))])
    _, s, led = run_lead_gen(fake, tmp_path)
    assert s["stopped_reason"] is None and s["finished"] == 1
    assert [e["ok"] for e in led] == [False, True] and "RateLimitError" in led[0]["error"]


def test_key_never_written_or_printed(keys, tmp_path, capsys):
    fake = FakeOpenRouter([(401, err_body(401, "authentication_error", "Invalid credentials"))])
    rd, _, _ = run_lead_gen(fake, tmp_path, items=2)
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    for f in rd.rglob("*"):
        if f.is_file():
            assert SECRET not in f.read_text(), f


# ---------------- free pre-flight ----------------

def models_list(prices=None, drop=None):
    prices = prices or {"anthropic/claude-haiku-5.5": ("0.0000001", "0.0000005"),
                        "anthropic/claude-sonnet-5.5": ("0.000002", "0.00001"),
                        "anthropic/claude-opus-5.5": ("0.000004", "0.00002")}
    return {"data": [{"id": k, "pricing": {"prompt": p, "completion": c}}
                     for k, (p, c) in prices.items() if k != drop]}


def key_status(limit=3.0, remaining=3.0):
    return {"data": {"label": "test", "limit": limit, "limit_remaining": remaining, "usage": 0}}


def fake_fetch(models=None, key=(200, None), seen=None):
    def f(url, k=None):
        if seen is not None:
            seen.append((url, k))
        if url == openrouter.MODELS_URL:
            assert k is None  # the public list is fetched without the key
            return 200, models if models is not None else models_list()
        return key[0], key[1] if key[1] is not None else key_status()
    return f


ALIASES = ["haiku", "sonnet", "opus"]


def test_preflight_ok():
    seen = []
    res = openrouter.preflight(SECRET, ALIASES, 0.72, fetch=fake_fetch(seen=seen))
    assert res["problems"] == [] and len(res["models"]) == 3
    assert [u for u, _ in seen] == [openrouter.MODELS_URL, openrouter.KEY_URL]


@pytest.mark.parametrize("kwargs,needle", [
    ({"models": models_list(drop="anthropic/claude-opus-5.5")}, "not in OpenRouter's model list"),
    ({"models": models_list(prices={"anthropic/claude-haiku-5.5": ("0.0000002", "0.0000005"),
                                    "anthropic/claude-sonnet-5.5": ("0.000002", "0.00001"),
                                    "anthropic/claude-opus-5.5": ("0.000004", "0.00002")})}, "budget math would be wrong"),
    ({"key": (401, {})}, "rejected the key"),
    ({"key": (200, key_status(remaining=0.50))}, "below this run's hard ceiling"),
])
def test_preflight_problems(kwargs, needle):
    res = openrouter.preflight(SECRET, ALIASES, 0.72, fetch=fake_fetch(**kwargs))
    assert any(needle in p for p in res["problems"]), res["problems"]


def test_preflight_output_hides_key(capsys):
    openrouter.print_preflight(openrouter.preflight(SECRET, ALIASES, 0.72, fetch=fake_fetch()))
    assert SECRET not in capsys.readouterr().out


# ---------------- experiment gates with --provider openrouter ----------------

@pytest.fixture
def no_real(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("a real client was created")
    monkeypatch.setattr(exp, "make_real_client", boom)
    monkeypatch.setitem(sys.modules, "dotenv", None)  # ignore any developer .env during tests
    return calls


def test_openrouter_run_refused_without_openrouter_key(monkeypatch, no_real):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_SECRET)  # wrong provider's key doesn't count
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--real", "--provider", "openrouter", "--leads", "3", "--approve-max-usd", "1"])
    assert "OPENROUTER_API_KEY" in str(e.value) and "Not falling back" in str(e.value)


def test_openrouter_preflight_problem_blocks_spending(monkeypatch, keys, no_real):
    monkeypatch.setattr(exp, "openrouter_preflight",
                        lambda key, aliases, ceiling: openrouter.preflight(key, aliases, ceiling,
                                                                          fetch=fake_fetch(key=(200, key_status(remaining=0.1)))))
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--real", "--provider", "openrouter", "--leads", "3", "--approve-max-usd", "1"])
    assert "pre-flight" in str(e.value) and not no_real


def test_openrouter_gates_order(monkeypatch, keys, no_real):
    """Approval is checked before the network pre-flight; confirmation after it."""
    pre = []
    monkeypatch.setattr(exp, "openrouter_preflight", lambda *a: pre.append(a) or {"models": [], "key": {}, "problems": []})
    with pytest.raises(SystemExit):
        exp.main(["run", "--real", "--provider", "openrouter", "--leads", "3", "--approve-max-usd", "0.10"])
    assert pre == []  # under-approved: didn't even contact OpenRouter
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as e:
        exp.main(["run", "--real", "--provider", "openrouter", "--leads", "3", "--approve-max-usd", "0.72"])
    assert len(pre) == 1 and "no terminal" in str(e.value) and not no_real
    assert pre[0][1] == ["haiku", "sonnet", "opus"]


def test_mock_openrouter_run_is_labeled_and_free(monkeypatch, keys, no_real, tmp_path):
    monkeypatch.setattr(exp, "HERE", tmp_path)
    (tmp_path / "data").symlink_to(ROOT / "experiments/quality/data")
    (tmp_path / "spec").symlink_to(ROOT / "experiments/quality/spec")
    out = exp.main(["run", "--mock", "--provider", "openrouter", "--leads", "3"])
    m = json.loads((out / "manifest.json").read_text())
    assert m["mode"] == "MOCK" and m["provider"] == "openrouter" and "MOCK-openrouter" in out.name
    assert not no_real


def test_full_pilot_through_fake_openrouter(keys, tmp_path):
    """All three arms, 3 pilot leads, through the real SDK + OpenRouter request path (fake HTTP)."""
    from assembly_line.client import fake_from_schema
    import random

    def respond(request):
        b = json.loads(request.content)
        schema = json.loads(b["system"][0]["text"].split("JSON Schema. No prose, no code fences.\n\n", 1)[1])
        out = fake_from_schema(schema, random.Random(len(request.content)))
        return hx().Response(200, json=ok_body(text=json.dumps(out), model=b["model"], cost=0.002))
    m = hx()
    client = ClaudeClient(provider="openrouter", max_attempts=0,
                          http_client=m.AsyncClient(transport=m.MockTransport(respond)))
    specs = exp.build_workflows()
    out = tmp_path / "run"
    exp.run_experiment(specs, exp.load_leads(3), client, out, "MOCK", 0.20, 1, "none", provider="openrouter")
    man = json.loads((out / "manifest.json").read_text())
    rows = exp.load_jsonl(out / "results.jsonl")
    assert len(rows) == 9 and all(r["status"] == "complete" for r in rows)
    assert man["model_versions_consistent"] and man["cost_sources"] == {"billed": sum(r["calls"] for r in rows)}
    assert set(man["served_models"]) == {"haiku", "sonnet"}
    exp.make_blind(out, 1)
    assert "anthropic/" not in (out / "review.md").read_text()  # blinding still holds


# ---------------- secrets hygiene ----------------

def test_env_file_is_gitignored():
    r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
    assert r.returncode == 0


def test_key_script_reads_silently():
    s = (ROOT / "scripts" / "set-openrouter-key.sh").read_text()
    assert "read -rs" in s and "chmod 600" in s and "echo \"$key\"" not in s
