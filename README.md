# Agent Assembly Line

Run AI agents like a factory: cheap models do small, standardized jobs at each
station, strict schemas make every output an interchangeable part, and a stronger
"manager" model inspects samples and can **stop the whole line** when quality drops.

It's Frederick Taylor + Henry Ford + Toyota, applied to Claude:

| Factory idea | In this repo |
|---|---|
| Division of labor (Ford) | A line is a list of **stations**, each doing one transformation |
| Interchangeable parts (Ford) | Every station must output JSON matching its **schema**, or it's rejected |
| Scientific selection of workers (Taylor) | Each station picks a **model**: Haiku for grunt work, Sonnet for writing, Opus for judgment |
| Time-and-motion study (Taylor) | Every call is logged to a **ledger** with tokens, cost and latency; each run gets a report |
| Separate planning from doing (Taylor) | You design the line; workers only execute their station |
| Jidoka / andon cord (Toyota) | Bad parts are **pulled off the line**; if the inspector's defect rate crosses a threshold, the **line stops** |
| Kaizen (Toyota) | `line kaizen` has Opus study a run's defects and propose prompt fixes |

## Setup (5 minutes)

```bash
git clone https://github.com/bradenlocsin/agent-assembly-line.git
cd agent-assembly-line
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Try it for free first. Mock mode uses fake workers, so no API key is needed and nothing is spent:

```bash
line run lines/lead_gen --input lines/lead_gen/sample_input.csv --mock
```

You'll see items move through the line, some get retried or escalated, a few get
pulled off as defective, and a report at the end. (Mock token counts and costs are
simulated. They show the mechanics, not real prices.)

When you're ready for real runs:

```bash
cp .env.example .env    # then paste your API key from platform.claude.com
line run lines/lead_gen --input my_businesses.csv --limit 5 --budget 0.50
```

**Always start with `--limit 5`.** Check the outputs and the report, then scale.

## How a line works

A line is a folder with a `line.yaml` and some prompt files:

```
lines/lead_gen/
├── line.yaml          # stations, models, schemas, inspector, budget
├── prompts/           # one instructions file per station
└── sample_input.csv
```

For each input item:

1. Each station gets the item plus every earlier station's output, as JSON.
2. The worker must return JSON matching the station's `output_schema`.
3. If it doesn't, it's retried once with the error as feedback (`max_retries`),
   then sent to the stronger `escalate_to` model.
4. If that fails too, the item is **pulled off the line** into `defects.jsonl`.
5. Finished items are randomly sampled (`sample_rate`) by the **inspector**.
6. If the inspector's defect rate goes above `stop_line_if_defect_rate_above`
   (after at least `min_inspected` checks), the **andon cord is pulled**: no new work starts.
7. The line also stops when spending hits `budget_usd`. The budget is checked before every
   call, including retries. Calls already in flight still finish, so the worst-case overshoot is
   roughly `concurrency` × the cost of one call.

Errors from the API itself:
- **Timeouts and network errors** are logged and count as a failed attempt, so retry and
  escalation still apply. Set the per-call limit with `settings.call_timeout_s` (default 300).
- **Errors that would hit every item** (bad API key, no permission, unknown model, malformed
  request) stop the line immediately instead of failing item by item.
- An unexpected crash on one item is recorded in `defects.jsonl` as `failed_at: engine`,
  and the rest of the run continues. Outputs and the report are always written.

## What you get in each run

`runs/<line>-<timestamp>/`
- `output.jsonl`: finished items, with every station's output
- `output.csv`: the columns listed in `output_fields`, ready for a spreadsheet
- `defects.jsonl`: what failed, where, and why
- `ledger.jsonl`: one row per model call (the time-and-motion data)
- `report.md`: cost by station, failures, escalations, cost per finished item

## Commands

| Command | What it does |
|---|---|
| `line run <line> --input <file>` | Run a line. Flags: `--mock`, `--limit N`, `--budget USD`, `--concurrency N` |
| `line report <run_dir>` | Re-print a run's report |
| `line new <name>` | Scaffold a new line in `lines/<name>/` |
| `line kaizen <line> <run_dir>` | Opus reads the defects and suggests specific prompt/schema fixes |
| `line models` | Show models and prices |

Input can be `.csv`, `.jsonl` or `.json`. An `id` column is used if present.

## Building your own line with AI

The repo includes a `CLAUDE.md`, so Claude Code knows how lines work. Open the repo in
Claude Code and say something like:

> Make a new line that takes a CSV of YouTube video URLs, pulls the key points,
> writes 3 TikTok hook ideas per video, and has Opus inspect the hooks.

It will scaffold the folder, write the prompts and schemas, and test it in mock mode.

## Costs to know (Nov 2026, per million tokens, input / output)

| Model | Input | Output | Good for |
|---|---|---|---|
| haiku (Haiku 5.5) | $0.10 | $0.50 | extraction, classification, research, scoring |
| sonnet (Sonnet 5.5) | $2 | $10 | writing, moderate reasoning |
| opus (Opus 5.5) | $4 | $20 | inspection, hard judgment, kaizen |
| fable (Fable 5.2) | $8 | $40 | the very hardest reasoning |

Haiku jumps to $0.50 / $2.50 when a single prompt exceeds 100K tokens, so keep work items small.
Web search adds $10 per 1,000 searches. Prices live in `assembly_line/models.py`.

## Design rules that keep lines cheap and reliable

- **One job per station.** If a prompt says "and then", it's two stations.
- **Tight schemas.** Use `enum`, `minimum`/`maximum` and `maxLength`. The schema is your quality gate.
- **Cheapest model that passes.** Start every station on Haiku, and only upgrade the ones whose failure rate the report shows is high.
- **Inspect, don't babysit.** The manager samples finished work, not every step.
- **Errors compound.** Ten stations at 95% each yield about 60% clean output. Fewer, sharper stations win.

## The example line: `lead_gen`

Turns a list of local businesses into scored leads with a drafted outreach email for an AI-automation service:

`enrich` (Haiku + web search) → `score` (Haiku, fixed rubric) → `draft` (Sonnet) → inspector (Opus, 25% sample)

It **only drafts** emails. Read them before sending anything, and follow CAN-SPAM rules
(real name and address, honest subject line, an easy way to opt out).

## Benchmarks

`python benchmarks/bench.py` compares the assembly line against a single model doing the
whole job. **It is a simulation:** failure rates, token counts and latencies come from
`benchmarks/assumptions.json`, and those numbers are guesses. Latest results are in
`benchmarks/results/latest.md`. With the current assumptions:

- Engine overhead is about 1 ms per call (actually measured), which is negligible.
- The line costs about half as much as a single Sonnet doing everything, mostly because Haiku,
  not Sonnet, reads the web-search results.
- The line is *not* measurably better than a single Haiku doing everything: they come out about
  the same on cost and success rate. Whether splitting the job improves output **quality** is
  unknown until it's tested on real runs.
- Web-search fees (~$0.02/item) are about 80% of the line's cost, so cutting searches matters
  more than model choice.

Replace the assumptions with numbers from your own `ledger.jsonl` to make the comparison real.
**That benchmark has known biases in the line's favor.** See the list in
`experiments/quality/PROTOCOL.md`; don't cite it as evidence that the line is better.

## Quality experiment (blind, pre-registered)

`experiments/quality/` tests whether the line actually beats one model doing the whole job:
single Haiku vs single Sonnet vs the assembly line, on 20 fixed leads with evidence packets,
scored by a blind human reviewer. Start with `PROTOCOL.md`. Commands:

```bash
python experiments/quality/exp.py estimate --leads 3     # free: cost ceiling
python experiments/quality/exp.py run --mock --leads 3   # free: dry run
python experiments/quality/exp.py run --real --leads 3 --approve-max-usd <ceiling>   # paid
```

Real runs refuse to start without an API key, an explicit `--approve-max-usd` at or above the
computed ceiling, and typing `SPEND`. They never fall back to mock, and mock never touches the API.

## Using OpenRouter instead of an Anthropic key

Add `--provider openrouter` to `line run`, `line kaizen`, or the experiment's `estimate`, `run`
and `check-openrouter`. It uses OpenRouter's Anthropic-compatible endpoint
(`https://openrouter.ai/api/v1/messages`) through the same SDK, with the same prompts, schemas and
budget rules. Requests are pinned to Anthropic as the serving provider with no fallbacks, so you
get the same model builds as the direct API.

| Alias | Anthropic ID | OpenRouter ID | $/M in / out |
|---|---|---|---|
| haiku | claude-haiku-5-5 | anthropic/claude-haiku-5.5 | 0.10 / 0.50 |
| sonnet | claude-sonnet-5-5 | anthropic/claude-sonnet-5.5 | 2 / 10 |
| opus | claude-opus-5-5 | anthropic/claude-opus-5.5 | 4 / 20 |

Fable has no verified OpenRouter ID yet and is refused on that provider.

```bash
bash scripts/set-openrouter-key.sh     # hidden input; writes OPENROUTER_API_KEY to .env (chmod 600)
python experiments/quality/exp.py check-openrouter   # free: checks IDs, live prices, key limit
```

On OpenRouter the ledger records the cost OpenRouter actually billed (`usage.cost`) and uses it for
the budget, with our own estimate kept next to it for comparison. A 401 (bad key), 402 (out of
credits) or 403 (key spending limit) stops the line on the first call.

## Notes

- The web search tool version is set in `.env` (`WEB_SEARCH_TOOL_VERSION`). If the API rejects it, update it from the docs.
- Run `pytest` to check that everything still works after changes. The tests use the mock client.
