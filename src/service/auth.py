"""Shared bearer authentication for full and SCAT-only service profiles."""

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core import settings


def verify_bearer(
    http_auth: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(HTTPBearer(description="Please provide AUTH_SECRET api key.", auto_error=False)),
    ],
    request: Request = None,
) -> None:
    if (
        settings.PORTAL_SIGNING_KEY_FILE
        and request is not None
        and request.headers.get("x-portal-signature")
    ):
        from hermes_runtime.portal_identity import request_principal

        try:
            request_principal(
                request, settings.PORTAL_IDENTITIES_FILE, settings.PORTAL_SIGNING_KEY_FILE
            )
        except (ValueError, OSError):
            raise HTTPException(status_code=401, detail="Invalid portal assertion") from None
        return
    if not settings.AUTH_SECRET:
        return
    auth_secret = settings.AUTH_SECRET.get_secret_value()
    if not http_auth or not compare_digest(http_auth.credentials, auth_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
        )
