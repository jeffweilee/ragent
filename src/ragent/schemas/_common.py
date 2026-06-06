"""Shared filter-field validation helpers for source_app / source_meta."""

from __future__ import annotations

from ragent.schemas.ingest import SOURCE_META_MAX

FILTER_MAX_LEN = 64
FILTER_META_MAX_LEN = SOURCE_META_MAX


def validate_filter_str(v: str | None, *, name: str, max_len: int) -> str | None:
    if v is None:
        return v
    if v == "" or len(v) > max_len:
        raise ValueError(f"{name} must be 1–{max_len} chars")
    return v
