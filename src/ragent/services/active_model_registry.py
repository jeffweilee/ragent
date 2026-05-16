"""ActiveModelRegistry (T-EM.9) — TTL-cached view of `system_settings.embedding.*`.

Single composition-root singleton. Ingest pipeline and chat pipeline read
model identity from here instead of from env or a hardcoded constant. State
moves performed by `/embedding/v1/*` are picked up by the next refresh
tick (default 10s); App restart is never required to swap models.

State is *derived* from the persisted rows:
- `embedding.candidate is null AND embedding.read == "stable"` → IDLE
- `embedding.candidate non-null AND embedding.read == "stable"` → CANDIDATE
- `embedding.candidate non-null AND embedding.read == "candidate"` → CUTOVER

On `refresh()` failure the last good cache is retained (logged as
`event=embedding.cache.stale`), so a transient DB blip does not strand
ingest or chat.
"""

from __future__ import annotations

from typing import Any

import structlog

from ragent.clients.embedding_model_config import EmbeddingModelConfig

logger = structlog.get_logger(__name__)


class ActiveModelRegistryNotReady(RuntimeError):
    """Raised when a read happens before the first refresh has succeeded."""


class ActiveModelRegistry:
    def __init__(self, settings_repo: Any, ttl_seconds: int = 10) -> None:
        self._repo = settings_repo
        self._ttl = ttl_seconds
        self._stable: EmbeddingModelConfig | None = None
        self._candidate: EmbeddingModelConfig | None = None
        self._read: str = "stable"
        self._retired: list[dict] = []
        self._ready: bool = False

    _KEYS = (
        "embedding.stable",
        "embedding.candidate",
        "embedding.read",
        "embedding.retired",
    )

    async def refresh(self) -> None:
        try:
            values = await self._repo.get_many(list(self._KEYS))
        except Exception as exc:
            logger.warning("embedding.cache.stale", error_type=type(exc).__name__)
            return
        stable = values.get("embedding.stable")
        candidate = values.get("embedding.candidate")
        self._stable = EmbeddingModelConfig.from_dict(stable) if stable else None
        self._candidate = EmbeddingModelConfig.from_dict(candidate) if candidate else None
        self._read = values.get("embedding.read") or "stable"
        self._retired = values.get("embedding.retired") or []
        self._ready = True

    def _require_ready(self) -> None:
        if not self._ready or self._stable is None:
            raise ActiveModelRegistryNotReady("ActiveModelRegistry.refresh() must succeed once")

    def derived_state(self) -> str:
        self._require_ready()
        if self._candidate is None:
            return "IDLE"
        return "CUTOVER" if self._read == "candidate" else "CANDIDATE"

    def read_model(self) -> EmbeddingModelConfig:
        self._require_ready()
        if self._read == "candidate" and self._candidate is not None:
            return self._candidate
        assert self._stable is not None
        return self._stable

    def write_models(self) -> list[EmbeddingModelConfig]:
        self._require_ready()
        assert self._stable is not None
        if self._candidate is None:
            return [self._stable]
        return [self._stable, self._candidate]

    def stable_model(self) -> EmbeddingModelConfig | None:
        return self._stable

    def candidate_model(self) -> EmbeddingModelConfig | None:
        return self._candidate

    @property
    def stable_dict(self) -> dict | None:
        return self._stable.to_dict() if self._stable else None

    @property
    def candidate_dict(self) -> dict | None:
        return self._candidate.to_dict() if self._candidate else None

    @property
    def retired_list(self) -> list[dict]:
        return list(self._retired)

    def snapshot(self) -> dict:
        self._require_ready()
        return {
            "state": self.derived_state(),
            "stable": self._stable.to_dict() if self._stable else None,
            "candidate": self._candidate.to_dict() if self._candidate else None,
            "read": self._read,
            "retired": self._retired,
            "cache_ttl_seconds": self._ttl,
        }
