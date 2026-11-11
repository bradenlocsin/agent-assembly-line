"""Load and validate a line definition (lines/<name>/line.yaml)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import MODELS


@dataclass
class StationSpec:
    name: str
    model: str
    instructions: str
    output_schema: dict
    escalate_to: str | None = None
    max_retries: int = 1
    max_tokens: int = 1024
    tools: list[str] = field(default_factory=list)


@dataclass
class InspectorSpec:
    model: str
    instructions: str
    sample_rate: float = 0.2
    min_inspected: int = 5
    stop_line_if_defect_rate_above: float = 0.3
    max_tokens: int = 3000


@dataclass
class LineSpec:
    name: str
    path: Path
    stations: list[StationSpec]
    inspector: InspectorSpec | None
    concurrency: int = 5
    budget_usd: float = 1.00
    output_fields: list[str] | None = None


def _read_prompt(base: Path, value: str) -> str:
    """A prompt can be inline text or a path to a .md file in the line folder."""
    p = base / value
    if value.endswith((".md", ".txt")) and p.exists():
        return p.read_text()
    return value


def _check_model(alias: str | None, where: str):
    if alias is not None and alias not in MODELS:
        raise ValueError(f"{where}: unknown model '{alias}' (use {', '.join(MODELS)})")


def load_line(line_dir: str | Path) -> LineSpec:
    base = Path(line_dir)
    cfg_path = base / "line.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"No line.yaml in {base}")
    raw = yaml.safe_load(cfg_path.read_text())

    stations = []
    names = set()
    for s in raw.get("stations", []):
        name = s["name"]
        if name in names:
            raise ValueError(f"Duplicate station name '{name}'")
        names.add(name)
        _check_model(s["model"], f"station '{name}'")
        _check_model(s.get("escalate_to"), f"station '{name}' escalate_to")
        stations.append(StationSpec(
            name=name,
            model=s["model"],
            instructions=_read_prompt(base, s["instructions"]),
            output_schema=s["output_schema"],
            escalate_to=s.get("escalate_to"),
            max_retries=s.get("max_retries", 1),
            max_tokens=s.get("max_tokens", 1024),
            tools=s.get("tools", []),
        ))
    if not stations:
        raise ValueError("A line needs at least one station")

    insp = None
    if "inspector" in raw and raw["inspector"]:
        i = raw["inspector"]
        _check_model(i["model"], "inspector")
        insp = InspectorSpec(
            model=i["model"],
            instructions=_read_prompt(base, i["instructions"]),
            sample_rate=i.get("sample_rate", 0.2),
            min_inspected=i.get("min_inspected", 5),
            stop_line_if_defect_rate_above=i.get("stop_line_if_defect_rate_above", 0.3),
            max_tokens=i.get("max_tokens", 3000),
        )

    settings = raw.get("settings", {})
    return LineSpec(
        name=raw.get("name", base.name),
        path=base,
        stations=stations,
        inspector=insp,
        concurrency=settings.get("concurrency", 5),
        budget_usd=settings.get("budget_usd", 1.00),
        output_fields=raw.get("output_fields"),
    )
