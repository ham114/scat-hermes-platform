"""Versioned contracts for durable, independently renderable analysis artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from scat_model_runtime.contracts import ModelContract
from schema.analysis import AnalysisResult

# 旧格式（随机 32 位 hex，M3 初版）与新语义格式兼容：
# 新格式 = 任务-模型-数据集-批次-对象-预测时域-点数-时间-短后缀，可读可追溯。
LEGACY_ARTIFACT_ID_PATTERN = r"^[0-9a-f]{32}$"
SEMANTIC_ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
ARTIFACT_ID_PATTERN = rf"^({LEGACY_ARTIFACT_ID_PATTERN[1:-1]}|{SEMANTIC_ARTIFACT_ID_PATTERN[1:-1]})$"

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug_segment(value: str, max_length: int = 48) -> str:
    """把 ID 组成字段清洗为安全字符并限长；空值回退为 x。"""
    cleaned = _SLUG_UNSAFE.sub("-", str(value)).strip("-.")
    return (cleaned or "x")[:max_length]


def build_semantic_artifact_id(
    *,
    task: str,
    model_key: str,
    dataset: str,
    batch: str | None,
    external_id: str,
    horizon_s: float,
    n_points: int,
    now: datetime,
    suffix: str,
) -> str:
    """生成可读的 artifact 标识：任务-模型-数据集-批次-对象-时域-点数-时间-后缀。

    短随机后缀保证同参数重复运行仍唯一；字段清洗后仅含 [A-Za-z0-9._:-]。
    """
    parts = [
        slug_segment(task, 24),
        slug_segment(model_key, 48),
        slug_segment(dataset, 24),
        slug_segment(batch or "", 48),
        slug_segment(external_id, 32),
        f"h{int(round(horizon_s))}",
        f"n{int(n_points)}",
        now.strftime("%Y%m%dT%H%M%S"),
        slug_segment(suffix, 8),
    ]
    artifact_id = "-".join(parts)
    if not re.fullmatch(ARTIFACT_ID_PATTERN, artifact_id):
        raise ValueError(f"generated artifact id is invalid: {artifact_id[:120]}")
    return artifact_id


class AnalysisArtifactRecord(ModelContract):
    """One bounded structured result produced by a persisted model run."""

    schema_version: Literal["1.0"] = "1.0"
    artifact_id: str = Field(min_length=8, max_length=200, pattern=ARTIFACT_ID_PATTERN)
    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    kind: Literal["analysis_result"] = "analysis_result"
    owner_user_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    model_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    model_version: str = Field(min_length=1, max_length=64)
    analysis_result: AnalysisResult
    payload_size_bytes: int = Field(ge=1, le=16_000_000)
    payload_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    expires_at: datetime | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("analysis artifact timestamps must include a timezone")
        return value

    @staticmethod
    def canonical_payload(analysis_result: AnalysisResult) -> bytes:
        payload = analysis_result.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def build(
        cls,
        *,
        artifact_id: str,
        run_id: str,
        owner_user_id: str,
        thread_id: str,
        model_key: str,
        model_version: str,
        analysis_result: AnalysisResult,
        created_at: datetime,
        expires_at: datetime | None = None,
    ) -> AnalysisArtifactRecord:
        payload = cls.canonical_payload(analysis_result)
        return cls(
            artifact_id=artifact_id,
            run_id=run_id,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            model_key=model_key,
            model_version=model_version,
            analysis_result=analysis_result,
            payload_size_bytes=len(payload),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            created_at=created_at,
            expires_at=expires_at,
        )

    def to_mongo_document(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude_none=True)

    @classmethod
    def from_mongo_document(cls, document: dict[str, Any]) -> AnalysisArtifactRecord:
        payload = {key: value for key, value in document.items() if key != "_id"}
        for field_name in ("created_at", "expires_at"):
            value = payload.get(field_name)
            if isinstance(value, datetime) and value.tzinfo is None:
                payload[field_name] = value.replace(tzinfo=UTC)
        return cls.model_validate(payload)
