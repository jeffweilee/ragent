"""Operational endpoints for immediate document state management."""

from __future__ import annotations

import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ragent.auth.deps import get_user_id
from ragent.services.ingest_service import IngestService

OPS_RETRY_BATCH_LIMIT = 500


class OpsStatusCount(BaseModel):
    before: int
    after: int


class OpsRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statuses: list[Literal["UPLOADED", "PENDING", "FAILED"]] = Field(min_length=1)
    source_app: str | None = None
    source_id: str | None = None
    created_after: datetime.datetime | None = None
    limit: int = Field(default=OPS_RETRY_BATCH_LIMIT, gt=0, le=OPS_RETRY_BATCH_LIMIT)
    dry_run: bool = False


class OpsRetryResponse(BaseModel):
    dry_run: bool
    counts: dict[str, OpsStatusCount]
    queued: int
    skipped: int
    # Rows reset for retry but NOT enqueued because the broker was unreachable
    # (T-RG.5). Separate from `queued` because this endpoint's contract is an
    # *immediate* re-queue: the worker sweep will still pick these up, later.
    deferred: int = 0


def create_admin_ops_router(svc: IngestService) -> APIRouter:
    router = APIRouter(prefix="/ops/v1")

    @router.post("/retry", status_code=200)
    async def ops_retry(
        body: OpsRetryRequest,
        user_id: Annotated[str, Depends(get_user_id)],
    ) -> OpsRetryResponse:
        before, after, queued, skipped, deferred = await svc.batch_rerun(
            statuses=body.statuses,
            source_app=body.source_app,
            source_id=body.source_id,
            created_after=body.created_after,
            limit=body.limit,
            dry_run=body.dry_run,
            operator_id=user_id,
        )
        counts = {
            s: OpsStatusCount(before=before.get(s, 0), after=after.get(s, 0)) for s in body.statuses
        }
        return OpsRetryResponse(
            dry_run=body.dry_run,
            counts=counts,
            queued=queued,
            skipped=skipped,
            deferred=deferred,
        )

    return router
