"""Free OpenRouter checks, done before any paid call.

- GET https://openrouter.ai/api/v1/models  (public, no key): confirm our model IDs exist and that
  their prices match the ones the budget math uses.
- GET https://openrouter.ai/api/v1/key     (your key, no model call, no charge): confirm the key works
  and its spending limit has room for the run's hard ceiling.

Nothing here prints or stores the key.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .models import get_model

MODELS_URL = "https://openrouter.ai/api/v1/models"
KEY_URL = "https://openrouter.ai/api/v1/key"


def fetch_json(url: str, key: str | None = None, timeout: float = 15.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        return e.code, body


def check_models(models_json: dict, aliases: list[str]) -> tuple[list[dict], list[str]]:
    """Returns (rows describing each model, problems)."""
    by_id = {m.get("id"): m for m in models_json.get("data", [])}
    rows, problems = [], []
    for alias in aliases:
        m = get_model(alias)
        if not m.openrouter_id:
            problems.append(f"{alias}: no verified OpenRouter ID")
            continue
        live = by_id.get(m.openrouter_id)
        if not live:
            problems.append(f"{alias}: {m.openrouter_id} not in OpenRouter's model list")
            continue
        pr = live.get("pricing", {})
        try:
            live_in, live_out = float(pr["prompt"]) * 1e6, float(pr["completion"]) * 1e6
        except (KeyError, TypeError, ValueError):
            problems.append(f"{alias}: OpenRouter pricing missing or unreadable")
            continue
        rows.append({"alias": alias, "id": m.openrouter_id, "live_in": live_in, "live_out": live_out,
                     "ours_in": m.price.input, "ours_out": m.price.output})
        for name, live_v, ours in [("input", live_in, m.price.input), ("output", live_out, m.price.output)]:
            if abs(live_v - ours) > 1e-6 * max(1.0, ours):
                problems.append(f"{alias}: live {name} price ${live_v:.4f}/M != ours ${ours:.4f}/M "
                                f"(budget math would be wrong)")
    return rows, problems


def check_key(status: int, key_json: dict, ceiling: float) -> tuple[dict, list[str]]:
    if status == 401:
        return {}, ["OpenRouter rejected the key (401). Check OPENROUTER_API_KEY in .env."]
    if status != 200:
        return {}, [f"Couldn't read key status (HTTP {status})."]
    d = key_json.get("data", {})
    info = {"label": d.get("label"), "limit": d.get("limit"), "limit_remaining": d.get("limit_remaining"),
            "usage": d.get("usage")}
    problems = []
    rem = d.get("limit_remaining")
    if rem is not None and rem < ceiling:
        problems.append(f"Key limit remaining ${rem:.2f} is below this run's hard ceiling ${ceiling:.2f}.")
    return info, problems


def preflight(key: str, aliases: list[str], ceiling: float, fetch=fetch_json) -> dict:
    status, models_json = fetch(MODELS_URL)
    problems = []
    rows = []
    if status != 200:
        problems.append(f"Couldn't read OpenRouter's model list (HTTP {status}).")
    else:
        rows, p = check_models(models_json, aliases)
        problems += p
    kstatus, key_json = fetch(KEY_URL, key)
    key_info, p = check_key(kstatus, key_json, ceiling)
    problems += p
    return {"models": rows, "key": key_info, "problems": problems}


def print_preflight(res: dict):
    print("OpenRouter pre-flight (free: no model calls)")
    for r in res["models"]:
        print(f"  {r['alias']:7} {r['id']:30} live ${r['live_in']:.2f}/${r['live_out']:.2f} per M "
              f"| ours ${r['ours_in']:.2f}/${r['ours_out']:.2f}")
    k = res["key"]
    if k:
        lim = "none" if k["limit"] is None else f"${k['limit']:.2f}"
        rem = "unlimited" if k["limit_remaining"] is None else f"${k['limit_remaining']:.2f}"
        print(f"  key limit {lim}, remaining {rem}, used so far ${k.get('usage') or 0:.2f}")
    for p in res["problems"]:
        print(f"  PROBLEM: {p}")
    if not res["problems"]:
        print("  OK")
