# CLAUDE.md

Guidance for AI coding agents working in this repo.

## What this is

A framework for running Claude models as an assembly line. A **line** (`lines/<name>/line.yaml`)
is an ordered list of **stations**. Each station has one model, one instructions file, and one
JSON Schema its output must satisfy. An optional **inspector** samples finished items and stops the
line if the defect rate is too high. All engine code is in `assembly_line/`.

## When the user asks for a new line

1. Run `line new <name>` (or `python -m assembly_line new <name>`) to scaffold `lines/<name>/`.
2. Split the job into stations. Each station does ONE transformation. Prefer 2–4 stations.
3. Pick models by the job, not the importance of the project:
   - `haiku`: extraction, classification, lookup/research (with `tools: [web_search]`), rubric scoring
   - `sonnet`: writing people will read, multi-step reasoning
   - `opus`: the inspector, and genuinely hard judgment
   - `fable`: only if the user asks for it or opus clearly isn't enough
   Give every haiku or sonnet station an `escalate_to` one step up.
4. Write a tight `output_schema` for each station: `required` fields, `enum` for categories,
   `minimum`/`maximum` for scores, `maxLength` for short text. Avoid `null` types; use `"unknown"`.
5. Write each `prompts/<station>.md`: the station's single job, rules, and an explicit instruction
   not to invent facts. Don't describe the JSON format: the engine appends the schema automatically.
   Each station receives `{"id", "input", "<earlier_station>": {...}}`. Refer to earlier outputs by station name.
6. Write `prompts/inspector.md` as a concrete defect checklist. Say what is NOT a defect too.
7. Set `output_fields` (dotted paths like `score.fit_score`) for the CSV export.
8. Create a small `sample_input.csv` (5–12 rows) and verify with:
   `line run lines/<name> --input lines/<name>/sample_input.csv --mock`
9. Never run without `--mock` unless the user asks. Real runs cost money: suggest `--limit 5 --budget 0.50` first.

## Conventions

- Prices live only in `assembly_line/models.py`. Update there if Anthropic changes them.
- Model IDs: haiku=claude-haiku-5-5, sonnet=claude-sonnet-5-5, opus=claude-opus-5-5, fable=claude-fable-5-2.
- Run `pytest` after changing engine code. Tests use `MockClient`. Never call the real API in tests.
- Keep the engine dependency-light (anthropic, pyyaml, jsonschema, python-dotenv).
- Run outputs go in `runs/` (gitignored). Never commit `.env`.
