"""Owner-scoped REST access to durable analysis results."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from analysis_runs.api_contracts import (
    AnalysisArtifactSummary,
    AnalysisArtifactSummaryPage,
    AnalysisArtifactView,
)
from analysis_runs.artifact_contracts import ARTIFACT_ID_PATTERN
from analysis_runs.artifact_repository import (
    AnalysisArtifactRepository,
    analysis_artifact_repository,
)
from service.errors import ErrorResponse
from service.identity import get_analysis_owner_id

router = APIRouter(
    prefix="/api/v1/analysis",
    tags=["Analysis results"],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)


def get_artifact_repository() -> AnalysisArtifactRepository:
    return analysis_artifact_repository


ArtifactRepository = Annotated[AnalysisArtifactRepository, Depends(get_artifact_repository)]
AnalysisOwnerId = Annotated[str, Depends(get_analysis_owner_id)]
ArtifactIdPath = Annotated[
    str,
    Path(
        min_length=8,
        max_length=200,
        pattern=ARTIFACT_ID_PATTERN,
        description="Opaque persisted analysis artifact identifier.",
    ),
]


def _not_found() -> HTTPException:
    # Deliberately use the same response for missing and foreign-owned IDs.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Analysis artifact was not found",
    )


@router.get(
    "/artifacts",
    response_model=AnalysisArtifactSummaryPage,
    operation_id="list_analysis_artifact_summaries",
)
async def list_analysis_artifact_summaries(
    owner_user_id: AnalysisOwnerId,
    repository: ArtifactRepository,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
    operation: Annotated[
        str | None,
        Query(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$"),
    ] = None,
    batch: Annotated[
        str | None,
        Query(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ] = None,
) -> AnalysisArtifactSummaryPage:
    records = await repository.list_for_owner(
        owner_user_id=owner_user_id,
        limit=limit,
        operation=operation,
        batch=batch,
    )
    now = datetime.now(UTC)
    summaries = [
        AnalysisArtifactSummary.from_record(record)
        for record in records
        if record.expires_at is None or record.expires_at > now
    ]
    return AnalysisArtifactSummaryPage(count=len(summaries), artifacts=summaries)


@router.get(
    "/artifacts/{artifact_id}",
    response_model=AnalysisArtifactView,
    operation_id="get_analysis_artifact",
)
async def get_analysis_artifact(
    artifact_id: ArtifactIdPath,
    owner_user_id: AnalysisOwnerId,
    repository: ArtifactRepository,
) -> AnalysisArtifactView:
    record = await repository.get_for_owner(artifact_id, owner_user_id=owner_user_id)
    if record is None:
        raise _not_found()
    if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
        raise _not_found()

    payload = record.canonical_payload(record.analysis_result)
    if len(payload) != record.payload_size_bytes or (
        hashlib.sha256(payload).hexdigest() != record.payload_sha256
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analysis artifact integrity check failed",
        )
    return AnalysisArtifactView.from_record(record)


__all__ = ["get_artifact_repository", "router"]
