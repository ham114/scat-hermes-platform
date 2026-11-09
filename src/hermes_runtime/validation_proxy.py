"""Optional aggregate QA reservation for model relay calls; baseline caps remain active."""

import json
import os
from pathlib import Path
from uuid import uuid4

from hermes_runtime.skills import checked_path
from hermes_runtime.validation_campaign import CampaignExhausted, ValidationCampaign


def validation_reserver(root):
    filename = os.environ.get("SCAT_VALIDATION_PROXY_SCOPE_FILE")
    if not filename:
        return None
    descriptor = json.loads(checked_path(Path(root), filename).read_text())
    if set(descriptor) != {"scope", "campaign_id"}:
        raise ValueError("invalid_proxy_validation_descriptor")

    def reserve(units, role):
        active = os.environ.get("SCAT_VALIDATION_CAMPAIGN_FILE")
        if not active:
            raise ValueError("proxy_validation_policy_missing")
        policy = json.loads(checked_path(Path(root), active).read_text())
        if policy == {"schema_version": 1, "status": "retired"}:
            return
        campaign = ValidationCampaign.for_scope(descriptor["scope"])
        if campaign is None or campaign.policy["id"] != descriptor["campaign_id"]:
            raise ValueError("proxy_validation_scope_not_selected")
        try:
            campaign.reserve(descriptor["scope"], "proxy_" + role + "_" + uuid4().hex, units)
        except CampaignExhausted:
            raise ValueError("validation_campaign_exhausted") from None

    return reserve
