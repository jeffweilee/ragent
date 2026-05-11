"""T0.11a — .env.example must stay symmetric with spec §4.6 env-var tables."""

import re
from pathlib import Path

SPEC = Path(__file__).parents[2] / "docs" / "00_spec.md"
EXAMPLE = Path(__file__).parents[2] / ".env.example"


def _spec_vars() -> dict[str, str]:
    """Return {VAR_NAME: default_text} from all §4.6 markdown tables."""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("### 4.6 Environment Variables")
    rest = text[start:]
    # Section ends at the next horizontal rule
    end = re.search(r"\n---\n", rest)
    section = rest[: end.start()] if end else rest
    result: dict[str, str] = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*(.*?)\s*\|", line)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result


def _example_vars() -> dict[str, str]:
    """Return {VAR_NAME: raw_line} from .env.example (non-comment, non-empty lines)."""
    result: dict[str, str] = {}
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            result[name] = stripped
    return result


def test_symmetric_var_set() -> None:
    spec = set(_spec_vars())
    example = set(_example_vars())
    missing = spec - example
    orphans = example - spec
    assert not missing, f"Vars in spec §4.6 missing from .env.example: {sorted(missing)}"
    assert not orphans, f"Vars in .env.example not declared in spec §4.6: {sorted(orphans)}"


def test_required_vars_marked_in_example() -> None:
    spec = _spec_vars()
    example = _example_vars()
    for name, default in spec.items():
        if default == "(required)":
            line = example.get(name, "")
            assert "# REQUIRED" in line, (
                f"Spec marks {name} as (required) but .env.example does not carry '# REQUIRED'"
            )
