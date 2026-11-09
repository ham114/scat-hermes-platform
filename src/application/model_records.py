"""Pure model task record helpers shared with the legacy compatibility tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from analysis_runs import (
    AnalysisArtifactRecord,
    AnalysisRunComponents,
    AnalysisRunRecord,
    AnalysisRunStatusEvent,
)
from core.settings import settings
from scat_model_runtime import (
    ComparisonTaskOutput,
    ModelRunStatus,
    ModelSpec,
    ModelTaskRequest,
    RegressionTaskOutput,
)
from schema.analysis import AnalysisResult


def _model_summary(spec: ModelSpec) -> dict[str, Any]:
    return {
        "model_key": spec.model_key,
        "version": spec.version,
        "display_name": spec.display_name,
        "description": spec.description,
        "task_type": spec.task_type.value,
        "supported_datasets": sorted(dataset.value for dataset in spec.supported_datasets),
        "execution_mode": spec.execution_mode.value,
        "availability": spec.availability.value,
        "unavailable_reason": spec.unavailable_reason,
        "limits": spec.resource_limits.model_dump(mode="json"),
        "input_schema": spec.input_schema,
        "output_schema": spec.output_schema,
        "allowed_evaluators": [item.model_dump(mode="json") for item in spec.allowed_evaluators],
        "default_evaluator": (
            spec.evaluator.model_dump(mode="json") if spec.evaluator is not None else None
        ),
    }


def _effective_run_scope(
    *,
    thread_id: str | None,
    user_id: str | None,
) -> tuple[str, str]:
    """Mirror the API's stable thread fallback for direct tool invocations."""

    effective_thread_id = thread_id or "direct-model-tool"
    effective_user_id = user_id or f"thread:{effective_thread_id}"
    return effective_thread_id, effective_user_id


def _run_components(spec: ModelSpec, request: ModelTaskRequest) -> AnalysisRunComponents:
    return AnalysisRunComponents(
        input_resolver=spec.input_resolver,
        feature_pipeline=spec.feature_pipeline,
        model_package=spec.model_package,
        execution_backend=spec.execution_backend,
        execution_release=spec.execution_release,
        evaluator=request.evaluator or spec.evaluator,
    )


def _new_analysis_run(
    *,
    run_id: str,
    request: ModelTaskRequest,
    spec: ModelSpec,
    thread_id: str | None,
    user_id: str | None,
) -> AnalysisRunRecord:
    now = datetime.now(UTC)
    effective_thread_id, effective_user_id = _effective_run_scope(
        thread_id=thread_id,
        user_id=user_id,
    )
    expires_at = None
    if settings.ANALYSIS_RUN_RETENTION_DAYS > 0:
        expires_at = now + timedelta(days=settings.ANALYSIS_RUN_RETENTION_DAYS)
    return AnalysisRunRecord(
        run_id=run_id,
        owner_user_id=effective_user_id,
        thread_id=effective_thread_id,
        task_type=request.task_type,
        execution_mode=spec.execution_mode,
        model_key=spec.model_key,
        model_version=spec.version,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        input_refs=request.references,
        parameters=request.parameters,
        components=_run_components(spec, request),
        code_version=settings.ANALYSIS_CODE_VERSION,
        status=ModelRunStatus.QUEUED,
        progress=0.0,
        status_history=(
            AnalysisRunStatusEvent(
                status=ModelRunStatus.QUEUED,
                progress=0.0,
                at=now,
                message="模型任务已创建。",
            ),
        ),
        created_at=now,
        updated_at=now,
        queued_at=now,
        expires_at=expires_at,
    )


def _new_analysis_artifact(
    *,
    artifact_id: str,
    run_record: AnalysisRunRecord,
    analysis_result: AnalysisResult,
) -> AnalysisArtifactRecord:
    now = datetime.now(UTC)
    expires_at = None
    if settings.ANALYSIS_ARTIFACT_RETENTION_DAYS > 0:
        expires_at = now + timedelta(days=settings.ANALYSIS_ARTIFACT_RETENTION_DAYS)
    return AnalysisArtifactRecord.build(
        artifact_id=artifact_id,
        run_id=run_record.run_id,
        owner_user_id=run_record.owner_user_id,
        thread_id=run_record.thread_id,
        model_key=run_record.model_key,
        model_version=run_record.model_version,
        analysis_result=analysis_result,
        created_at=now,
        expires_at=expires_at,
    )


def _public_run_record(record: AnalysisRunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "status": record.status.value,
        "progress": record.progress,
        "task_type": record.task_type.value,
        "execution_mode": record.execution_mode.value,
        "model": {"key": record.model_key, "version": record.model_version},
        "input_refs": [
            item.model_dump(mode="json", exclude_none=True) for item in record.input_refs
        ],
        "components": record.components.model_dump(mode="json", exclude_none=True),
        "code_version": record.code_version,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "result_summary": record.result_summary,
        "result_artifact_ids": list(record.result_artifact_ids),
        "result_schema_version": record.result_schema_version,
        "error": record.error.model_dump(mode="json") if record.error else None,
    }


def _public_artifact_record(record: AnalysisArtifactRecord) -> dict[str, Any]:
    action_link: str | None = None
    for block in record.analysis_result.blocks:
        payload = block.model_dump(mode="json")
        if payload.get("type") == "action_link" and isinstance(payload.get("url"), str):
            action_link = payload["url"]
            break
    return {
        "artifact_id": record.artifact_id,
        "run_id": record.run_id,
        "kind": record.kind,
        "model": {"key": record.model_key, "version": record.model_version},
        "title": record.analysis_result.title,
        "dataset": record.analysis_result.dataset,
        "operation": record.analysis_result.operation,
        "payload_size_bytes": record.payload_size_bytes,
        "created_at": record.created_at.isoformat(),
        "action_link": action_link,
    }


def _regression_summary(output: RegressionTaskOutput) -> dict[str, Any]:
    predictions = output.series
    first_prediction = predictions[0]
    first = dict(zip(output.target_names, first_prediction.values[0], strict=True))
    last = dict(zip(output.target_names, first_prediction.values[-1], strict=True))
    quality = first_prediction.annotations.get("quality")
    return {
        "output_schema": output.schema_version,
        "model_key": output.model_key,
        "model_version": output.model_version,
        "batch_size": output.batch_size,
        "point_count": sum(len(item.values) for item in predictions),
        "points_per_series": [len(item.values) for item in predictions],
        "forecast_origin": first_prediction.forecast_origin.isoformat(),
        "forecast_horizon_seconds": max(item.time_offsets_seconds[-1] for item in predictions),
        "target_names": list(output.target_names),
        "target_units": output.target_units,
        "first_prediction": first,
        "last_prediction": last,
        "quality": quality,
        "items": [
            {
                "external_id": item.input_ref.external_id,
                "batch": item.input_ref.batch,
                "point_count": len(item.values),
                "forecast_origin": item.forecast_origin.isoformat(),
                "quality": item.annotations.get("quality"),
            }
            for item in predictions
        ],
    }


def _comparison_summary(output: ComparisonTaskOutput) -> dict[str, Any]:
    return {
        "output_schema": output.schema_version,
        "protocol_version": output.protocol_version,
        "model_key": output.model_key,
        "model_version": output.model_version,
        "sample_set_sha256": output.sample_set_sha256,
        "sample_count": output.batch_size,
        "covered_flights": len({item.input_ref.external_id for item in output.samples}),
        "excluded_counts": output.excluded_counts,
        "methods": [
            {
                "method_id": item.method_id,
                "method_version": item.method_version,
                "sample_count": item.sample_count,
                "covered_flights": item.covered_flights,
                "ade_metres": item.ade_metres.model_dump(mode="json"),
                "fde_metres": item.fde_metres.model_dump(mode="json"),
            }
            for item in output.method_summaries
        ],
    }
