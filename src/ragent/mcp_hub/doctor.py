"""Static validator for tools.yaml.

Catches drift the runtime would only surface at first call: duplicate names,
path placeholders without a matching `location: path` parameter, body
parameters on non-body-accepting HTTP methods, unknown types, etc.

Run in CI:
    uv run python -m ragent.mcp_hub.doctor path/to/tools.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path
from string import Formatter

from .mcp_hub import _BODY_METHODS, load_tools_yaml


def _path_placeholders(path: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(path) if field}


def _is_absolute_url(path: str) -> bool:
    return path.startswith(("http://", "https://"))


def check_yaml(path: str | Path) -> tuple[list[str], int]:
    """Return (validation errors, tool count). Empty list of errors means OK."""
    try:
        defaults, tools = load_tools_yaml(path)
    except (OSError, ValueError) as exc:
        return [f"failed to load {path}: {exc}"], 0

    errors: list[str] = []

    if not defaults.get("base_url"):
        relative = [t.name for t in tools if not _is_absolute_url(t.path)]
        if relative:
            errors.append(
                f"missing 'base_url' in defaults but tools have relative paths: {relative}"
            )

    for tool in tools:
        placeholders = _path_placeholders(tool.path)
        path_params = {p.name for p in tool.params if p.location == "path"}

        missing = placeholders - path_params
        if missing:
            errors.append(
                f"{tool.name}: path placeholders {sorted(missing)} have no matching parameter"
            )

        extra = path_params - placeholders
        if extra:
            errors.append(
                f"{tool.name}: path parameters {sorted(extra)} not used in path template "
                f"{tool.path!r}"
            )

        body_params = {p.name for p in tool.params if p.location == "body"}
        if body_params and tool.method not in _BODY_METHODS:
            errors.append(
                f"{tool.name}: body parameters {sorted(body_params)} but method is "
                f"{tool.method} (only POST/PUT/PATCH accept a body)"
            )

    return errors, len(tools)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m ragent.mcp_hub.doctor <tools.yaml>", file=sys.stderr)
        return 2

    target = args[0]
    errors, count = check_yaml(target)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: {target} ({count} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
