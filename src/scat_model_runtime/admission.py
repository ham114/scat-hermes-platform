"""Reviewable admission record for a future independent model service.

Validation is a contract check, not a trust signature or permission to deploy.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelServiceAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["model-service-admission/v1"] = "model-service-admission/v1"
    model_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    model_version: str = Field(min_length=1, max_length=64)
    release_id: str = Field(pattern=r"^[a-z0-9._-]+:[a-z0-9][a-z0-9._-]{0,100}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sbom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_schema: str = Field(min_length=1, max_length=120)
    output_schema: str = Field(min_length=1, max_length=120)
    max_batch_size: int = Field(ge=1, le=100)
    active_inference_limit: Literal[1] = 1
    application_queue_limit: Literal[0] = 0
    inference_timeout_seconds: float = Field(gt=0, le=120)
    main_app_imports_model_dependencies: Literal[False] = False
    service_requires_business_database: Literal[False] = False
    license_distribution_review_passed: bool = False
    contract_tests_passed: bool = False
    numerical_equivalence_passed: bool = False
    performance_gate_passed: bool = False
    security_admission_passed: bool = False
    trusted_signature_verified: bool = False
    rollback_drill_passed: bool = False
    gbar_pilot_completed: bool = False

    def admission_errors(self, metadata):
        errors = []
        if self.model_key == "scat-comparison-v1":
            errors.append("business_orchestration_stays_in_main_service")
        if self.release_id.endswith(":latest") or self.model_version == "latest":
            errors.append("mutable_version")
        for key in [
            "image_digest",
            "checkpoint_sha256",
            "source_sha256",
            "dependency_lock_sha256",
            "sbom_sha256",
        ]:
            if set(getattr(self, key).removeprefix("sha256:")) == {"0"}:
                errors.append("placeholder_" + key)
        for key in [
            "model_key",
            "model_version",
            "release_id",
            "input_schema",
            "output_schema",
            "image_digest",
            "checkpoint_sha256",
            "max_batch_size",
        ]:
            if metadata.get(key) != getattr(self, key):
                errors.append("metadata_mismatch_" + key)
        for key in [
            "license_distribution_review_passed",
            "contract_tests_passed",
            "numerical_equivalence_passed",
            "performance_gate_passed",
            "security_admission_passed",
            "trusted_signature_verified",
            "rollback_drill_passed",
            "gbar_pilot_completed",
        ]:
            if not getattr(self, key):
                errors.append(key)
        return errors
