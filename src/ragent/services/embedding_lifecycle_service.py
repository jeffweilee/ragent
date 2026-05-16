"""EmbeddingLifecycleService (T-EM.11) — orchestrates the five admin actions.

Caller responsibilities:
- The registry must have `refresh()`'d successfully before any method call;
  the service does not re-fetch settings.
- Settings transitions are atomic at the repository layer
  (`SystemSettingsRepository.transition`), so partial failures cannot leave
  the state machine in an impossible state.

The service raises:
- `IllegalEmbeddingTransition` (from utility) — wrong state for the action.
- `EmbeddingFieldCollision` — promote attempts a field name already in mapping.
- `CutoverPreflightFailed` — cutover hard gates not satisfied.
- `InvalidEmbeddingModelConfig` (from EmbeddingModelConfig) — bad dim or name.

Router maps each exception to the corresponding HTTP error code.

Boundary logs: every public method emits `embedding.lifecycle.<action>.started`
on entry and `embedding.lifecycle.<action>.{completed,failed}` on exit, per
`00_rule.md` §Service Boundary Logs.
"""

from __future__ import annotations

from typing import Any

import structlog

from ragent.clients.embedding_model_config import EmbeddingModelConfig
from ragent.services.cutover_preflight import preflight as _preflight
from ragent.utility.datetime import from_iso, to_iso, utcnow
from ragent.utility.embedding_lifecycle import next_state

logger = structlog.get_logger(__name__)


class EmbeddingFieldCollision(Exception):
    """Raised when a promote target field name is already present in the ES mapping."""


class CutoverPreflightFailed(Exception):
    """Raised when cutover hard gates fail. `report` carries the structured details."""

    def __init__(self, report: dict) -> None:
        super().__init__("cutover preflight failed")
        self.report = report


def _iso_now() -> str:
    return to_iso(utcnow())


def _retired_entry(model_dict: dict) -> dict:
    return {
        "name": model_dict["name"],
        "dim": model_dict["dim"],
        "field": model_dict["field"],
        "retired_at": _iso_now(),
        "cleanup_done": False,
    }


def _log_failure(action: str, exc: Exception, **ctx: Any) -> None:
    logger.warning(
        f"embedding.lifecycle.{action}.failed",
        error_code=type(exc).__name__,
        error_reason=str(exc)[:128],
        **ctx,
    )


class EmbeddingLifecycleService:
    def __init__(
        self,
        settings_repo: Any,
        es_client: Any,
        *,
        index_name: str,
        registry: Any,
        cache_ttl_seconds: int = 10,
    ) -> None:
        self._repo = settings_repo
        self._es = es_client
        self._index = index_name
        self._registry = registry
        self._ttl = cache_ttl_seconds

    # ------------------------------------------------------------------
    # promote
    # ------------------------------------------------------------------

    async def promote(self, *, name: str, dim: int, api_url: str, model_arg: str) -> dict:
        logger.info("embedding.lifecycle.promote.started", name=name, dim=dim)
        try:
            result = await self._do_promote(
                name=name, dim=dim, api_url=api_url, model_arg=model_arg
            )
        except Exception as exc:
            _log_failure("promote", exc, name=name, dim=dim)
            raise
        logger.info(
            "embedding.lifecycle.promote.completed",
            name=name,
            dim=dim,
            field=result["candidate"]["field"],
        )
        return result

    async def _do_promote(self, *, name: str, dim: int, api_url: str, model_arg: str) -> dict:
        next_state(self._registry.derived_state(), "promote")
        cfg = EmbeddingModelConfig(name=name, dim=dim, api_url=api_url, model_arg=model_arg)

        mapping = await self._es.indices.get_mapping(index=self._index)
        props = mapping[self._index]["mappings"].get("properties", {})
        existing = props.get(cfg.field)
        if existing is not None and existing.get("dims") != cfg.dim:
            # Different-dim collision is a hard error; same-dim is the
            # retry-safe path (put_mapping is idempotent below).
            raise EmbeddingFieldCollision(
                f"field {cfg.field} already mapped with dim {existing.get('dims')}, "
                f"requested {cfg.dim}"
            )

        await self._es.indices.put_mapping(
            index=self._index,
            body={
                "properties": {
                    cfg.field: {
                        "type": "dense_vector",
                        "dims": cfg.dim,
                        "index": True,
                        "similarity": "cosine",
                        # Match `resources/es/chunks_v1.json` (B26 P1 choice).
                        "index_options": {"type": "flat"},
                    }
                }
            },
        )

        promoted_at = _iso_now()
        candidate_payload = {**cfg.to_dict(), "promoted_at": promoted_at}
        # Optimistic lock: only proceed if no other admin call slipped a
        # candidate in between our state check and our write.
        await self._repo.transition(
            {"embedding.candidate": candidate_payload},
            expect={"embedding.candidate": None},
        )
        return {"state": "CANDIDATE", "candidate": candidate_payload, "promoted_at": promoted_at}

    # ------------------------------------------------------------------
    # cutover
    # ------------------------------------------------------------------

    async def cutover(self, *, force: bool = False) -> dict:
        logger.info("embedding.lifecycle.cutover.started", force=force)
        try:
            result = await self._do_cutover(force=force)
        except Exception as exc:
            _log_failure("cutover", exc, force=force)
            raise
        logger.info("embedding.lifecycle.cutover.completed", read="candidate")
        return result

    async def _do_cutover(self, *, force: bool) -> dict:
        next_state(self._registry.derived_state(), "cutover")
        candidate_dict = self._registry.candidate_dict or {}
        promoted_at_iso = candidate_dict.get("promoted_at")
        promoted_at = from_iso(promoted_at_iso) if promoted_at_iso else utcnow()

        report = await _preflight(
            registry=self._registry,
            es_client=self._es,
            index_name=self._index,
            promoted_at=promoted_at,
            cache_ttl_seconds=self._ttl,
        )
        if not report["pass"] and not force:
            raise CutoverPreflightFailed(report)

        await self._repo.transition(
            {"embedding.read": "candidate"},
            expect={"embedding.read": "stable"},
        )
        return {
            "state": "CUTOVER",
            "read": "candidate",
            "cutover_at": _iso_now(),
            "preflight": report,
        }

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------

    async def rollback(self) -> dict:
        logger.info("embedding.lifecycle.rollback.started")
        try:
            next_state(self._registry.derived_state(), "rollback")
            await self._repo.transition(
                {"embedding.read": "stable"},
                expect={"embedding.read": "candidate"},
            )
        except Exception as exc:
            _log_failure("rollback", exc)
            raise
        logger.info("embedding.lifecycle.rollback.completed", read="stable")
        return {"state": "CANDIDATE", "read": "stable", "rolled_back_at": _iso_now()}

    # ------------------------------------------------------------------
    # commit
    # ------------------------------------------------------------------

    async def commit(self) -> dict:
        logger.info("embedding.lifecycle.commit.started")
        try:
            result = await self._do_commit()
        except Exception as exc:
            _log_failure("commit", exc)
            raise
        logger.info("embedding.lifecycle.commit.completed", new_stable=result["stable"]["name"])
        return result

    async def _do_commit(self) -> dict:
        next_state(self._registry.derived_state(), "commit")
        # `expect` snapshots come from the *live* settings (which include
        # transient fields like `promoted_at`), not from the registry's
        # projected EmbeddingModelConfig view (5 keys only).
        live_stable = await self._repo.get("embedding.stable")
        live_candidate = await self._repo.get("embedding.candidate")
        if live_stable is None or live_candidate is None:
            raise RuntimeError("commit requires both stable and candidate populated")

        retired = list(self._registry.retired_list)
        retired.append(_retired_entry(live_stable))

        new_stable = {
            k: live_candidate[k] for k in ("name", "dim", "api_url", "model_arg", "field")
        }
        await self._repo.transition(
            {
                "embedding.stable": new_stable,
                "embedding.candidate": None,
                "embedding.read": "stable",
                "embedding.retired": retired,
            },
            expect={
                "embedding.read": "candidate",
                "embedding.stable": live_stable,
                "embedding.candidate": live_candidate,
            },
        )
        return {"state": "IDLE", "stable": new_stable, "committed_at": _iso_now()}

    # ------------------------------------------------------------------
    # abort
    # ------------------------------------------------------------------

    async def abort(self) -> dict:
        logger.info("embedding.lifecycle.abort.started")
        try:
            result = await self._do_abort()
        except Exception as exc:
            _log_failure("abort", exc)
            raise
        logger.info("embedding.lifecycle.abort.completed", aborted=result["aborted"])
        return result

    async def _do_abort(self) -> dict:
        next_state(self._registry.derived_state(), "abort")
        live_candidate = await self._repo.get("embedding.candidate")
        if live_candidate is None:
            raise RuntimeError("abort requires candidate populated")

        retired = list(self._registry.retired_list)
        retired.append(_retired_entry(live_candidate))
        await self._repo.transition(
            {"embedding.candidate": None, "embedding.retired": retired},
            expect={"embedding.candidate": live_candidate, "embedding.read": "stable"},
        )
        return {"state": "IDLE", "aborted": live_candidate["name"], "aborted_at": _iso_now()}
