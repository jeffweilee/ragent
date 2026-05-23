"""T0.8f / T-FB.5 / T-EI.3 — ES resource JSON files must equal their spec §5.x JSON blocks."""

import json
import re
from pathlib import Path

SPEC = Path(__file__).parents[2] / "docs" / "spec" / "data_structures.md"
RES_DIR = Path(__file__).parents[2] / "resources" / "es"
PIPELINES_DIR = RES_DIR / "pipelines"


def _extract_spec_json(section_anchor: str) -> dict:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index(section_anchor)
    section = text[start:]
    m = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    assert m, f"Could not find JSON code block under '{section_anchor}'"
    return json.loads(m.group(1))


def _extract_nth_spec_json(section_anchor: str, n: int) -> dict:
    """Extract the n-th (0-indexed) JSON block under the given anchor."""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index(section_anchor)
    section = text[start:]
    blocks = re.findall(r"```json\n(.*?)```", section, re.DOTALL)
    assert len(blocks) > n, (
        f"Section '{section_anchor}' has {len(blocks)} JSON block(s); requested index {n}"
    )
    return json.loads(blocks[n])


def test_chunks_v1_resource_matches_spec() -> None:
    spec_json = _extract_spec_json("### 5.2 Elasticsearch")
    resource_json = json.loads((RES_DIR / "chunks_v1.json").read_text(encoding="utf-8"))
    assert resource_json == spec_json, "resources/es/chunks_v1.json has drifted from spec §5.2."


def test_chunks_default_pipeline_resource_matches_spec() -> None:
    """T-EI.3 / B59 — `chunks_default` ingest pipeline JSON is the single
    source of truth alongside the index mapping; spec §5.2 mirrors it."""
    # §5.2 holds the index JSON first (block 0), the pipeline JSON second (block 1).
    spec_json = _extract_nth_spec_json("### 5.2 Elasticsearch", 1)
    resource_json = json.loads((PIPELINES_DIR / "chunks_default.json").read_text(encoding="utf-8"))
    assert resource_json == spec_json, (
        "resources/es/pipelines/chunks_default.json has drifted from spec §5.2 pipeline block."
    )


def test_chunks_default_pipeline_has_single_indexed_at_set_processor() -> None:
    """T-EI.3 / B59 — pipeline contract: exactly one `set` processor that
    fills `indexed_at` from ES's own `_ingest.timestamp`. Any extra processor
    or a different field/value is a drift."""
    resource_json = json.loads((PIPELINES_DIR / "chunks_default.json").read_text(encoding="utf-8"))
    processors = resource_json["processors"]
    assert len(processors) == 1, f"expected 1 processor, got {len(processors)}"
    proc = processors[0]
    assert "set" in proc, f"expected `set` processor, got keys={list(proc)}"
    assert proc["set"]["field"] == "indexed_at"
    assert proc["set"]["value"] == "{{{_ingest.timestamp}}}"


def test_feedback_v1_resource_matches_spec() -> None:
    spec_json = _extract_spec_json("### 5.4 Elasticsearch `feedback_v1`")
    resource_json = json.loads((RES_DIR / "feedback_v1.json").read_text(encoding="utf-8"))
    assert resource_json == spec_json, "resources/es/feedback_v1.json has drifted from spec §5.4."
