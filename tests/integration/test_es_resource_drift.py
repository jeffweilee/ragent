"""T0.8f — resources/es/chunks_v1.json must equal the JSON block in spec §5.2."""

import json
import re
from pathlib import Path

SPEC = Path(__file__).parents[2] / "docs" / "00_spec.md"
RESOURCE = Path(__file__).parents[2] / "resources" / "es" / "chunks_v1.json"


def _extract_spec_json() -> dict:
    text = SPEC.read_text()
    start = text.index("### 5.2 Elasticsearch")
    section = text[start:]
    m = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    assert m, "Could not find JSON code block in spec §5.2"
    return json.loads(m.group(1))


def test_chunks_v1_resource_matches_spec() -> None:
    spec_json = _extract_spec_json()
    resource_json = json.loads(RESOURCE.read_text())
    assert resource_json == spec_json, (
        "resources/es/chunks_v1.json has drifted from the spec §5.2 JSON block. "
        "Update one to match the other."
    )
