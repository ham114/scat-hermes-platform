"""Identity propagated by the authenticated HTTP boundary, including async tasks."""

from contextvars import ContextVar
from typing import Any

from core import settings

request_owner: ContextVar[str | None] = ContextVar("authenticated_request_owner", default=None)


def current_owner() -> str | None:
    return request_owner.get()


def owner_filter() -> dict[str, Any]:
    """CLI workers have no request scope; legacy records belong to the legacy owner."""
    owner = current_owner()
    if owner is None:
        return {}
    if owner == settings.ANALYSIS_LEGACY_OWNER_ID:
        return {"$or": [{"owner_user_id": owner}, {"owner_user_id": None}]}
    return {"owner_user_id": owner}
