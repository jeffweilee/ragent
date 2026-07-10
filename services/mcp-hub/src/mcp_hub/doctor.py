"""Static validator for tool YAML config files.

Catches drift the runtime would only surface at first call: duplicate names,
path placeholders without a matching `location: path` parameter, body
parameters on non-body-accepting HTTP methods, unknown types, etc.

Run in CI (--placeholder-ok skips secret env resolution):
    uv run mcp-hub-doctor path/to/tools.d --placeholder-ok

Run in production verification (validates secrets are in env):
    uv run mcp-hub-doctor path/to/tools.d
"""

from __future__ import annotations

import sys
from pathlib import Path
from string import Formatter

from ._render import PLACEHOLDER_ENV, validate_placeholder_syntax
from .mcp_hub import _BODY_METHODS, _YAML_SUFFIXES, load_tools_yaml


def _path_placeholders(path: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(path) if field}


def _is_absolute_url(path: str) -> bool:
    return path.startswith(("http://", "https://"))


def check_yaml(
    path: str | Path, *, placeholder_ok: bool = False
) -> tuple[list[str], int]:
    """Return (validation errors, tool count). Empty errors list means OK.

    placeholder_ok=True: validates placeholder key format + YAML structure
    without resolving secrets from env. Use in CI where secrets are absent.

    placeholder_ok=False (default): renders secrets from os.environ before
    validation. Fails if any {% .Secrets.KEY %} key is missing from env.
    """
    import os

    p = Path(path)
    raw_errors: list[str] = []

    if placeholder_ok:
        # Validate placeholder syntax on raw content before substitution
        files = (
            sorted(fp for fp in p.iterdir() if fp.suffix in _YAML_SUFFIXES)
            if p.is_dir()
            else [p]
        )
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
            except OSError as exc:
                raw_errors.append(f"{f}: {exc}")
                continue
            for err in validate_placeholder_syntax(content):
                raw_errors.append(f"{f}: {err}")
        env_arg = PLACEHOLDER_ENV
    else:
        env_arg = dict(os.environ)

    try:
        result = load_tools_yaml(path, strict=False, env=env_arg)
    except FileNotFoundError as exc:
        return [f"failed to load {path}: {exc}"] + raw_errors, 0

    errors: list[str] = raw_errors + [f"{f.source}: {f.reason}" for f in result.failures]

    for tool in result.tools:
        if not tool.base_url and not _is_absolute_url(tool.path):
            errors.append(
                f"{tool.name}: relative path {tool.path!r} with no base_url"
                f" (system or per-tool)"
            )
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
                f"{tool.name}: path parameters {sorted(extra)} not used in path "
                f"template {tool.path!r}"
            )

        body_params = {p.name for p in tool.params if p.location == "body"}
        if body_params and tool.method not in _BODY_METHODS:
            errors.append(
                f"{tool.name}: body parameters {sorted(body_params)} but method is "
                f"{tool.method} (only POST/PUT/PATCH accept a body)"
            )

    return errors, len(result.tools)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    placeholder_ok = "--placeholder-ok" in args
    positional = [a for a in args if not a.startswith("--")]

    if len(positional) != 1:
        print(
            "usage: mcp-hub-doctor <tools.yaml|tools.d/> [--placeholder-ok]",
            file=sys.stderr,
        )
        return 2

    target = positional[0]
    errors, count = check_yaml(target, placeholder_ok=placeholder_ok)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    mode = " (placeholder-ok)" if placeholder_ok else ""
    print(f"OK{mode}: {target} ({count} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
