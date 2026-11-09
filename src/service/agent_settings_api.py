"""Separate maintenance author surface, never mounted in the business agent router."""

import json
import os
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hermes_runtime.agent_settings import AgentSettingsStore


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    section: str = Field(min_length=1, max_length=32)
    changes: dict = Field(min_length=1, max_length=8)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str
    confirmed: bool = False


def install_settings(app, root, authorize, release_authorize):
    def store():
        filename = os.environ.get("SCAT_AGENT_SETTINGS_MANIFEST")
        if not filename:
            raise HTTPException(503, "settings_targets_not_registered")
        try:
            return AgentSettingsStore(root, filename)
        except (ValueError, OSError):
            raise HTTPException(503, "settings_targets_unavailable") from None

    owner_dependency = Depends(authorize)

    @app.get("/v1/settings")
    def snapshot(actor=owner_dependency):
        try:
            return store().snapshot(actor)
        except (ValueError, OSError):
            raise HTTPException(503, "settings_targets_unavailable") from None

    def validate(body):
        if len(json.dumps(body.changes)) > 32768:
            raise HTTPException(422, "settings_body_too_large")
        UUID(body.request_id)
        current, _ = store().validate(body.section, body.changes)
        if current["config_sha256"] != body.expected_sha256:
            raise ValueError("settings_revision_conflict")
        return current

    @app.post("/v1/settings/preview")
    def preview(body: Proposal, actor=owner_dependency):
        try:
            return {
                "before": validate(body)["value"],
                "changes": body.changes,
                "expected_sha256": body.expected_sha256,
                "application": "pending_maintenance",
            }
        except ValueError:
            raise HTTPException(409, "settings_validation_or_revision_conflict") from None

    @app.post("/v1/settings/proposals", dependencies=[Depends(release_authorize)])
    def stage(body: Proposal, actor=owner_dependency):
        if not body.confirmed:
            raise HTTPException(422, "explicit_configuration_confirmation_required")
        try:
            # The store checks the stable request first, including after a prior apply.
            if len(json.dumps(body.changes)) > 32768:
                raise ValueError("settings_body_too_large")
            return store().stage(
                actor, body.section, body.changes, body.expected_sha256, body.request_id
            )
        except ValueError:
            raise HTTPException(409, "settings_validation_or_revision_conflict") from None
