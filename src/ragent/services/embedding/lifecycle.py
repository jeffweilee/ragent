"""EmbeddingLifecycleService (T-EM.11) — orchestrates the five admin actions.

Caller responsibilities:
- The registry must have `refresh()`'d successfully before any method call;
  the service does not re-fetch settings.
- Settings transitions are atomic at the repository layer
  (`SystemSettingsRepository.transition`), so partial failures cannot leave
  the state machine in an impossible state.

The service raises:
- `IllegalEmbeddingTransition` (from utility) — wrong state for the action.
- `CutoverPreflightFailed` — cutover hard gates not satisfied.
- `InvalidEmbeddingModelConfig` (from EmbeddingModelConfig) — bad dim or name.

Router maps each exception to the corresponding HTTP error code.

Boundary logs: every public method emits `embedding.lifecycle.<action>.started`
on entry and `embedding.lifecycle.<action>.{completed,failed}` on exit, per
`00_rule.md` §Service Boundary Logs.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import structlog

from ragent.clients.embedding_model_config import EmbeddingModelConfig
from ragent.services.embedding.backfill import backfill as _do_backfill
from ragent.services.embedding.preflight import preflight as _preflight
from ragent.utility.datetime import from_iso, to_iso, utcnow
from ragent.utility.embedding_lifecycle import next_state

logger = structlog.get_logger(__name__)

_ES_RESOURCES = Path(__file__).parents[3] / "resources" / "es"


class EmbeddingFieldCollision(Exception):
    """Kept for router + test compatibility; promote no longer raises it (index-per-model)."""


_VERSION_SUFFIX_RE = re.compile(r"_v(\d+)$")


def _next_index_name(current: str) -> str:
    """'chunks_v1' → 'chunks_v2', 'chunks_v10' → 'chunks_v11', 'chunks' → 'chunks_v2'."""
    m = _VERSION_SUFFIX_RE.search(current)
    if not m:
        return f"{current}_v2"
    return f"{current[: m.start()]}_v{int(m.group(1)) + 1}"


def _chunk_index_body(dim: int) -> dict:
    """Load canonical index template and substitute the embedding dimension."""
    resources_dir = Path(os.environ.get("RAGENT_ES_RESOURCES_DIR") or _ES_RESOURCES)
    body = json.loads((resources_dir / "chunks_v1.json").read_text(encoding="utf-8"))
    body["mappings"]["properties"]["embedding"]["dims"] = dim
    return body


class CutoverPreflightFailed(Exception):
    """Raised when cutover hard gates fail. `report` carries the structured details."""

    def __init__(self, report: dict) -> None:
        super().__init__("cutover preflight failed")
        self.report = report


def _iso_now() -> str:
    return to_iso(utcnow())


def _retired_entry(model_dict: dict) -> dict:
    entry: dict = {
        "name": model_dict["name"],
        "dim": model_dict["dim"],
        "retired_at": _iso_now(),
        "cleanup_done": False,
    }
    if "index_name" in model_dict:
        entry["index_name"] = model_dict["index_name"]
    return entry


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

    async def _swap_read_alias(self, *, remove_from: str, add_to: str) -> None:
        alias = self._registry.read_alias
        await self._es.indices.update_aliases(
            body={
                "actions": [
                    {"remove": {"index": remove_from, "alias": alias}},
                    {"add": {"index": add_to, "alias": alias}},
                ]
            }
        )

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
            index=result["candidate"]["index_name"],
        )
        return result

    async def _do_promote(self, *, name: str, dim: int, api_url: str, model_arg: str) -> dict:
        next_state(self._registry.derived_state(), "promote")
        new_index = _next_index_name(self._registry.stable_index)
        cfg = EmbeddingModelConfig(
            name=name, dim=dim, api_url=api_url, model_arg=model_arg, index_name=new_index
        )
        await self._es.indices.create(index=new_index, body=_chunk_index_body(dim))
        promoted_at = _iso_now()
        candidate_payload = {**cfg.to_dict(), "promoted_at": promoted_at}
        try:
            await self._repo.transition(
                {"embedding.candidate": candidate_payload},
                expect={"embedding.candidate": None},
            )
        except Exception as transition_exc:
            try:
                await self._es.indices.delete(index=new_index)
            except Exception:
                # Delete failed; log and move on — original exception is the one that matters.
                logger.warning("es.promote_rollback_failed", index=new_index)
            raise transition_exc
        await self._registry.refresh(force=True)
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
        # The dual-write warmup gate compares `promoted_at` to `now`. Use the
        # registry's raw cached payload — the projected `candidate_dict` drops
        # `promoted_at`, which would make the warmup elapsed=0 and the gate
        # always fail.
        candidate_raw = self._registry.candidate_raw or {}
        promoted_at_iso = candidate_raw.get("promoted_at")
        promoted_at = from_iso(promoted_at_iso) if promoted_at_iso else utcnow()

        report = await _preflight(
            registry=self._registry,
            es_client=self._es,
            promoted_at=promoted_at,
            cache_ttl_seconds=self._ttl,
        )
        if not report["pass"] and not force:
            raise CutoverPreflightFailed(report)

        await self._repo.transition(
            {"embedding.read": "candidate"},
            expect={"embedding.read": "stable"},
        )
        candidate_index = self._registry.candidate_index
        if candidate_index:
            await self._swap_read_alias(
                remove_from=self._registry.stable_index,
                add_to=candidate_index,
            )
        await self._registry.refresh(force=True)
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
            candidate_index = self._registry.candidate_index
            if candidate_index:
                await self._swap_read_alias(
                    remove_from=candidate_index,
                    add_to=self._registry.stable_index,
                )
            await self._registry.refresh(force=True)
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
        # Snapshots come from the registry's cached raw payloads — they
        # include transient fields like `promoted_at` so the optimistic-
        # lock `expect=` precondition matches the live JSON byte-for-byte.
        stable_raw = self._registry.stable_raw
        candidate_raw = self._registry.candidate_raw
        if stable_raw is None or candidate_raw is None:
            raise RuntimeError("commit requires both stable and candidate populated")

        retired = list(self._registry.retired_list)
        retired.append(_retired_entry(stable_raw))

        _base = ("name", "dim", "api_url", "model_arg")
        _extra = ("index_name",) if "index_name" in candidate_raw else ()
        new_stable = {k: candidate_raw[k] for k in (*_base, *_extra)}
        await self._repo.transition(
            {
                "embedding.stable": new_stable,
                "embedding.candidate": None,
                "embedding.read": "stable",
                "embedding.retired": retired,
            },
            expect={
                "embedding.read": "candidate",
                "embedding.stable": stable_raw,
                "embedding.candidate": candidate_raw,
            },
        )
        await self._registry.refresh(force=True)
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

    # ------------------------------------------------------------------
    # backfill
    # ------------------------------------------------------------------

    async def backfill(self, *, broker: Any) -> dict:
        return await _do_backfill(self._registry, broker=broker)

    async def _do_abort(self) -> dict:
        next_state(self._registry.derived_state(), "abort")
        candidate_raw = self._registry.candidate_raw
        if candidate_raw is None:
            raise RuntimeError("abort requires candidate populated")

        # None only for legacy candidates promoted before index-per-model; skip silently.
        candidate_index = self._registry.candidate_index
        if candidate_index:
            await self._es.indices.delete(index=candidate_index)

        retired = list(self._registry.retired_list)
        retired.append(_retired_entry(candidate_raw))
        await self._repo.transition(
            {"embedding.candidate": None, "embedding.retired": retired},
            expect={"embedding.candidate": candidate_raw, "embedding.read": "stable"},
        )
        await self._registry.refresh(force=True)
        return {"state": "IDLE", "aborted": candidate_raw["name"], "aborted_at": _iso_now()}
