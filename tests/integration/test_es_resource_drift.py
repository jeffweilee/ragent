"""T0.8f / T-FB.5 — ES resource JSON files must equal their spec §5.x JSON blocks."""

import json
import re
from pathlib import Path

SPEC = Path(__file__).parents[2] / "docs" / "00_spec.md"
RES_DIR = Path(__file__).parents[2] / "resources" / "es"


def _extract_spec_json(section_anchor: str) -> dict:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index(section_anchor)
    section = text[start:]
    m = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    assert m, f"Could not find JSON code block under '{section_anchor}'"
    return json.loads(m.group(1))


def test_chunks_v1_resource_matches_spec() -> None:
    spec_json = _extract_spec_json("### 5.2 Elasticsearch")
    resource_json = json.loads((RES_DIR / "chunks_v1.json").read_text(encoding="utf-8"))
    assert resource_json == spec_json, "resources/es/chunks_v1.json has drifted from spec §5.2."


def test_feedback_v1_resource_matches_spec() -> None:
    spec_json = _extract_spec_json("### 5.4 Elasticsearch `feedback_v1`")
    resource_json = json.loads((RES_DIR / "feedback_v1.json").read_text(encoding="utf-8"))
    assert resource_json == spec_json, "resources/es/feedback_v1.json has drifted from spec §5.4."
