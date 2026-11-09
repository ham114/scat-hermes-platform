"""Deployment identity boundary for browser-facing analysis result APIs."""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from core import settings
from core.settings import AnalysisIdentityMode

TRUSTED_USER_HEADER = "X-Authenticated-User"


def _valid_identity(value: str | None) -> bool:
    return bool(
        value is not None
        and value == value.strip()
        and 1 <= len(value) <= 200
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def get_analysis_owner_id(
    value: Annotated[str | None, Header(alias=TRUSTED_USER_HEADER)] = None,
    request: Request = None,
) -> str:
    """Resolve the configured owner without trusting a URL/body identity claim."""

    if settings.ANALYSIS_IDENTITY_MODE == AnalysisIdentityMode.SINGLE_USER:
        owner_id = settings.ANALYSIS_SINGLE_USER_ID
        if not _valid_identity(owner_id):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Single-user analysis identity is not configured",
            )
        return owner_id

    if settings.ANALYSIS_IDENTITY_MODE == AnalysisIdentityMode.DISABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analysis artifact identity is not configured",
        )

    if settings.PORTAL_SIGNING_KEY_FILE:
        from hermes_runtime.portal_identity import request_principal

        try:
            principal = request_principal(
                request, settings.PORTAL_IDENTITIES_FILE, settings.PORTAL_SIGNING_KEY_FILE
            )
        except (ValueError, OSError):
            raise HTTPException(
                status_code=401, detail="Verified portal identity required"
            ) from None
        return principal["owner_id"]

    if not _valid_identity(value):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user identity is missing",
        )
    assert value is not None
    return value


AnalysisOwnerId = Annotated[str, get_analysis_owner_id]


__all__ = ["TRUSTED_USER_HEADER", "get_analysis_owner_id"]
