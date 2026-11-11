# Benchmark: assembly line vs. single model (SIMULATED)

> **Read this first.** No real model was called. Failure rates, token counts and latencies come from `benchmarks/assumptions.json`, and those numbers are guesses. These results show what the framework's logic does *given* those assumptions. They are not evidence about real Claude performance or output quality.

Items per run: 200 · seeds: 5 · values are mean ± stdev across seeds · task: lead_gen (research with web search → score → draft) · inspector off for a like-for-like pipeline comparison.

## 1. Engine overhead (real measurement)

With a zero-latency fake model: 6,294 calls for 2,000 items in 6.46s → **1.03 ms of engine time per model call** (validation, scheduling, ledger writes). Real API calls take roughly 1–30 s, so engine overhead is negligible next to model latency.

## 2. Workflow comparison (simulated)

| Workflow | Success | Cost / finished item | Calls / item | Failed calls | Items recovered after a failure | Sim. wall time (s) |
|---|---|---|---|---|---|---|
| Assembly line (haiku→haiku→sonnet, escalation on) | 99.9% ± 0.2% | $0.0265 ± 0.0004 | 3.15 ± 0.03 | 29.6 ± 4.4 | 27.2 ± 4.4 / 27.4 | 849 ± 20 |
| Assembly line, escalation OFF | 99.1% ± 0.4% | $0.0263 ± 0.0003 | 3.12 ± 0.02 | 29.4 ± 4.6 | 25.6 ± 3.8 / 27.4 | 868 ± 24 |
| Single model: haiku (escalates to sonnet) | 99.9% ± 0.2% | $0.0261 ± 0.0013 | 1.17 ± 0.04 | 34.8 ± 7.7 | 29.8 ± 4.8 / 30.0 | 836 ± 48 |
| Single model: sonnet (escalates to opus) | 100.0% ± 0.0% | $0.0564 ± 0.0004 | 1.07 ± 0.01 | 13.4 ± 1.7 | 12.6 ± 1.9 / 12.6 | 1038 ± 45 |
| Single model: opus (escalates to fable) | 100.0% ± 0.0% | $0.0996 ± 0.0017 | 1.02 ± 0.02 | 4.2 ± 3.2 | 3.8 ± 3.0 / 3.8 | 1855 ± 147 |

Wall time is simulated: modeled latency is slept at a reduced scale with the line's real concurrency (5), then scaled back up. It reflects the latency *assumptions*, not measured API speed.

## 3. Where the money goes, and what the inspector costs (simulated)

Assembly line cost breakdown (seed 0):

| Component | Cost | Share |
|---|---|---|
| web search fees | $4.240 | 82% |
| draft (tokens) | $0.616 | 12% |
| enrich (tokens) | $0.316 | 6% |
| score (tokens) | $0.022 | 0% |

Adding the Opus inspector at a 25% sample rate raises total line cost from $5.298 to $6.231 per 200 items (**+18%**). (The line-stop threshold was disabled for this measurement so a simulated verdict couldn't halt the run.) Whether that buys anything depends on how often it catches real problems, which can't be simulated.

## 4. Sensitivity: when does the conclusion flip? (simulated)

Assembly line vs. single Sonnet, varying one assumption at a time.

### 4a. Haiku schema-failure rate

| Haiku fail rate | Line cost/item | Sonnet cost/item | Line ÷ Sonnet | Line success | Sonnet success |
|---|---|---|---|---|---|
| 2% | $0.0251 | $0.0560 | 0.45× | 100.0% | 100.0% |
| 8% | $0.0267 | $0.0560 | 0.48× | 99.7% | 100.0% |
| 20% | $0.0314 | $0.0560 | 0.56× | 99.7% | 100.0% |
| 40% | $0.0398 | $0.0560 | 0.71× | 98.7% | 100.0% |
| 60% | $0.0549 | $0.0560 | 0.98× | 97.3% | 100.0% |

### 4b. Tokens read per web search (the biggest cost driver)

| Tokens per search | Line cost/item | Sonnet cost/item | Line ÷ Sonnet |
|---|---|---|---|
| 0 | $0.0251 | $0.0305 | 0.82× |
| 1,000 | $0.0254 | $0.0347 | 0.73× |
| 6,000 | $0.0267 | $0.0560 | 0.48× |
| 20,000 | $0.0305 | $0.1155 | 0.26× |

### 4c. Combined-task penalty (do long multi-part prompts fail more than their parts?)

| Penalty | Line success | Sonnet success | Sonnet cost/item |
|---|---|---|---|
| 1× | 99.7% | 100.0% | $0.0560 |
| 2× | 99.7% | 99.7% | $0.0605 |
| 4× | 99.7% | 98.0% | $0.0728 |

## 5. What this does and doesn't show

- **Measured for real:** engine overhead; that retries, escalation and budget logic behave as designed under injected failures; the cost arithmetic.
- **Assumed, not measured:** every failure rate, token count, thinking-token count and latency.
- **Not measured at all:** output quality. A single strong model might write better emails, or the split might lose context between stations. Only real runs plus human or inspector review can answer that.
- **Reproduce:** `python benchmarks/bench.py` (seeded; same assumptions → same numbers except wall-clock overhead).
