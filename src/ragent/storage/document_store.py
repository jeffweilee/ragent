"""DocumentStore — narrow Protocol for chat-attachment raw-byte storage (T-CAT.6).

Dependency Inversion: `chat_attachment_service` depends on this Protocol, not
directly on MinIO. `MinIODocumentStore` is the only implementation today.

All four methods are synchronous, blocking I/O (the MinIO SDK has no async
client). Callers invoking them from an `async def` MUST wrap each call in
`anyio.to_thread.run_sync()` — the Protocol cannot enforce this at the type
level, so every async call site is responsible for not blocking the event loop.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DocumentStore(Protocol):
    def put(self, object_key: str, data: bytes, *, content_type: str) -> None: ...
    def get(self, object_key: str) -> bytes: ...
    def delete(self, object_key: str) -> None: ...
    def exists(self, object_key: str) -> bool: ...
