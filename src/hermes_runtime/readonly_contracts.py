"""Versioned readonly result references, revalidated against scoped receipt storage."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from hermes_runtime.skill_policy import StrictModel

OUTPUTS = {
    "scat-flight-query": "scat.query-result.v1",
    "scat-data-quality": "scat.quality-observation.v1",
    "scat-trajectory-analysis": "scat.trajectory-observation.v1",
    "aviation-knowledge": "aviation.cited-answer.v1",
    "uav-flight-analysis": "uav.observation.v1",
}


class ReadonlyReference(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["readonly-result"] = "readonly-result"
    contract: str
    owner_scope: str
    producer_run: str
    result_id: str
    operation: str
    payload_sha256: str
    query_sha256: str
    bundle_sha256: str
    units_protocol: Literal["operation-schema-v1"] = "operation-schema-v1"
    time_protocol: Literal["operation-schema-v1"] = "operation-schema-v1"


def result_reference(store, release, scope, run_id, result_id):
    row = store.get(scope, run_id, result_id)
    operation = row["operation"]
    candidates = [
        name
        for name, entry in release.entries.items()
        if "mcp__scat__" + operation in entry["allowed_operations"] and name in OUTPUTS
    ]
    if len(candidates) != 1:
        raise ValueError("ambiguous_readonly_contract")
    contract = OUTPUTS[candidates[0]]
    if contract not in release.entries[candidates[0]]["contract"]["outputs"]:
        raise ValueError("readonly_contract_not_published")
    query = row["result"].get("query", {})
    return ReadonlyReference(
        contract=contract,
        owner_scope=scope,
        producer_run=run_id,
        result_id=result_id,
        operation=operation,
        payload_sha256=row["payload_sha256"],
        query_sha256=hashlib.sha256(
            json.dumps(query, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        bundle_sha256=release.bundle_sha256,
    )


def resolve_reference(store, release, scope, run_id, reference, *, expected_contract):
    reference = ReadonlyReference.model_validate(reference)
    if reference.owner_scope != scope or reference.producer_run != run_id:
        raise ValueError("readonly_reference_scope_mismatch")
    if reference.contract != expected_contract:
        raise ValueError("readonly_reference_contract_mismatch")
    actual = result_reference(store, release, scope, run_id, reference.result_id)
    if actual != reference:
        raise ValueError("readonly_reference_integrity_mismatch")
    return store.get(scope, run_id, reference.result_id)["result"]
