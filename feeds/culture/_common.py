"""Shared helpers for the Culture ("The Weekend") generators.

Each generator writes a structured JSON file to
    data/culture/<section>/<YYYY-MM-DD>.json
which site/build_site.py renders into the Culture tabs. No raw_text here —
unlike the news feeds, Culture is structured data (tracks, movies, photos).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def write_culture(section: str, data: dict) -> Path:
    """Persist a Culture section's data for the site build."""
    today = datetime.now(timezone.utc).date().isoformat()
    data = {
        "section": section,
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    out = ROOT / "data" / "culture" / section / f"{today}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"culture: wrote {out}")
    return out


def extract_json(text: str) -> dict:
    """Pull a JSON object out of an LLM response, tolerating code fences/prose."""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
