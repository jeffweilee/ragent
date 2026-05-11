"""Testcontainers registry helper — reads hub.image.name.prefix from project-level
.testcontainers.properties so intranet registries work without code changes."""

from pathlib import Path


def _load_prefix() -> str:
    props = Path(__file__).parents[1] / ".testcontainers.properties"
    if not props.exists():
        return ""
    for line in props.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "hub.image.name.prefix":
            prefix = v.strip()
            return prefix.rstrip("/") + "/" if prefix else ""
    return ""


_PREFIX = _load_prefix()


def tc_image(image: str) -> str:
    """Return image name prepended with hub.image.name.prefix if configured."""
    return _PREFIX + image
