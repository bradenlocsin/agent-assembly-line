# Quality experiment protocol

**Question:** does splitting the lead-gen task across specialized stations produce better, cheaper,
or more reliable output than one model doing the whole thing?

**Null hypothesis:** the assembly line is no better than single Haiku. This experiment is designed so
that this hypothesis can survive. Nothing here assumes the line wins.

## Arms

| Arm | What runs | Retry / escalation |
|---|---|---|
| `single_haiku` | one Haiku call does research, scoring and the email | 1 retry, then Sonnet |
| `single_sonnet` | one Sonnet call does all three | 1 retry, then Opus |
| `assembly_line` | lead_gen's stations: Haiku research → Haiku score → Sonnet draft | each station: 1 retry, then one model up |

All three get:
- **the same input:** a business plus a fixed evidence packet (`data/leads.jsonl`), and no web search.
  Live search would make each arm see different facts and make fact-checking impossible.
- **the same task text:** every arm's instructions are assembled from the same files in `spec/`.
  The line gets one section per station; single models get all three sections in one prompt.
- **the same output schemas** (copied from `lines/lead_gen/line.yaml`) and the same validation.
- **the same engine, ledger and pricing**, with concurrency 1, a 300-second call timeout, and the same budget.

**Gold labels** (`data/gold.jsonl`) are kept in a separate file and used only for scoring. A test
checks that no gold-only content ever reaches a prompt.

## Outcomes (decided before any real data)

**Primary**
1. **Acceptance rate**: the share of leads where the blind reviewer would send the email with at
   most minor edits. A lead with no output counts as not accepted.
2. **Overall human score**: the mean of accuracy, personalization, writing and offer fit (each 1–5).

**Secondary**
- Unsupported claims per email (human count), plus automatic proxies: numbers not in the evidence,
  and greeting names not in the evidence
- Gold-label accuracy on the three research facts, fit score in the gold range, and offer in the acceptable set
- Cost per accepted output (total spend, failures included, divided by accepted outputs)
- Latency per lead, and failed-call, retry and escalation rates

**Decision rule, applied at 20 leads only:** the assembly line is *supported* over single Haiku if
acceptance rate or overall score is higher with a 95% bootstrap CI that excludes zero, **and** its cost
per accepted output is at most 2× single Haiku's. Any other result means *not supported*.
Comparing the line with single Sonnet answers a different question: can a cheap pipeline match a
strong single model?

**Expected power:** with 20 paired leads, only large differences are detectable. For example, a
sign test needs about 15 wins out of 20 non-tied leads to reach p < 0.05. A null result at n=20 means
"no large effect found", not "no effect".

## Pilot first

`--leads 3` runs a pre-registered set: L01 (clear fit, owner named), L12 (almost no evidence, a trap
for invented details), and L20 (conflicting evidence). The pilot checks that real calls work end to end,
and calibrates cost. It cannot answer the research question, and the report says so.

## Fairness audit: known biases and how they're handled

| Risk | Which way it biases | Handling |
|---|---|---|
| Line gets more retry attempts in total (3 per station, so up to 9 calls) vs 3 for a single model | toward line on completion | Same per-call policy, reported openly. Completion rate is shown next to calls per lead and cost, and cost per accepted output charges for every retry. |
| Line mixes models (its email is written by Sonnet), so "line vs single Haiku" isn't purely about splitting the work | toward line on writing quality | Single Sonnet is included so model strength and task structure can be separated: if line ≈ single Sonnet > single Haiku, the gain is Sonnet, not the stations. |
| Spec was written by the line's author, and the single-model prompt is the station prompts stitched together | toward line (the prompts were designed for stations) | Spec rewritten neutrally ("step", not "station"). It's still author-written; flagged as a remaining risk. |
| An inspector filtering only the line's outputs would hide its failures | toward line | Inspector off by default. With `--inspector all`, the same inspector runs on every arm and never removes outputs from review. |
| Reviewer can tell responses apart by style or length | either | Responses are shown in identical format, with random IDs and random order per lead and lead order shuffled. Length and voice can't be hidden. Second reviewer recommended. |
| Reviewer is the project's author | toward line | Blinding plus a second reviewer; the score command reports agreement. |
| Synthetic evidence packets were written by an AI and may be easier or cleaner than real web pages | unknown | Stated limitation. Later: rerun on real businesses with saved page snapshots. |
| Small samples look convincing | toward whatever happens first | Confidence intervals are suppressed below n=10, every report carries an n-warning, and the decision rule only applies at 20. |
| Mock outputs or synthetic reviews mistaken for evidence | toward any conclusion | Run folders are tagged MOCK/REAL, and the report carries banners. Synthetic review files are detected by name and by column. |

## Biases found in the earlier simulated benchmark (`benchmarks/bench.py`)

That benchmark should not be read as evidence for the line. Specifically:
1. **It didn't measure quality at all.** "Success" just meant valid JSON.
2. **Retry asymmetry:** the line got up to 9 attempts per item vs 3 for a single model, which inflated
   the line's success rate.
3. **One-sided assumptions:** the "combined task penalty" only modeled a downside for single models.
   There was no matching parameter for the line's weaknesses (context lost between stations, errors
   compounding, extra prompt overhead per station).
4. **The failure rates and task difficulties were chosen by the line's author** and never calibrated.
5. **The model-mix confound:** the line's Sonnet draft station was compared with a single Haiku, so
   structure and model strength were mixed together.
6. **The "items recovered" metric** rewards workflows that make more calls, since they have more
   chances to fail and recover.
7. **The inspector was excluded** from the line's cost, though the production line includes it (+18%).
8. **The thinking-token assumption** charged only Opus, making single Opus look worse.

The benchmark's one robust result is engine overhead (~1 ms per call), which was actually measured.

## Commands

```bash
# Free
python experiments/quality/exp.py estimate --leads 3
python experiments/quality/exp.py run --mock --leads 3
python -m pytest tests/test_experiment.py

# Paid pilot: requires an API key in .env, explicit --approve-max-usd and typing SPEND
python experiments/quality/exp.py run --real --leads 3 --approve-max-usd <ceiling printed by estimate>

# Review and score
#   open runs/<run>/review.md, fill in runs/<run>/review_sheet.csv
python experiments/quality/exp.py score experiments/quality/runs/<run> --reviews experiments/quality/runs/<run>/review_sheet.csv

# Full experiment: only after the pilot works
python experiments/quality/exp.py estimate --leads 20 --budget-per-workflow 1.00
```
