"""Plugin Protocol v1 — frozen in Phase 1 (spec §6.2).

The Protocol is intentionally minimal. New fields require a failing test in the
same commit (journal 2026-05-03 Architecture rule).
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExtractorPlugin(Protocol):
    name: str
    required: bool
    queue: str

    def extract(self, doc_id: str) -> None: ...
    def delete(self, doc_id: str) -> None: ...
    def health(self) -> bool: ...
