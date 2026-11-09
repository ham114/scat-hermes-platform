"""M4-01/02/03 派生集合契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from scat.materialization.contracts import (
    MATERIALIZATION_BUSINESS_KEYS,
    MATERIALIZATION_COLLECTIONS,
    MATERIALIZATION_INDEXES,
    MATERIALIZATION_JOB_RETENTION_DAYS,
    MATERIALIZATION_RETENTION_DAYS,
    MATERIALIZATION_TTL_INDEXES,
    MATERIALIZATION_VERSION_KEYS,
    RECORD_MODELS,
    BatchStatisticsRecord,
    FlightFeaturesRecord,
    MaterializationJobRecord,
    PredictionMetricsRecord,
    RouteStatisticsRecord,
)


def version_fields() -> dict[str, object]:
    return {
        "source_version": "v1",
        "algorithm_version": "algo-1",
        "generated_at": datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    }


def test_batch_statistics_contract():
    record = BatchStatisticsRecord(
        **version_fields(),
        batch="scat20161015_20161021",
        flight_count=10,
        unique_callsign_count=9,
        aircraft_type_count=3,
        departure_airport_count=2,
        arrival_airport_count=2,
        total_track_points=100,
        average_track_points=10.0,
        minimum_track_points=5,
        maximum_track_points=20,
        flights_with_prediction=8,
        total_prediction_snapshots=40,
        missing_callsign_count=0,
        missing_aircraft_type_count=0,
        missing_departure_airport_count=0,
        missing_arrival_airport_count=0,
        missing_iobt_count=0,
        empty_trajectory_count=0,
        flights_without_prediction_count=2,
    )
    assert record.batch == "scat20161015_20161021"
    assert record.generated_at.tzinfo is not None


def test_prediction_metrics_contract():
    record = PredictionMetricsRecord(
        **version_fields(),
        batch="b",
        external_id="100002",
        snapshot_time=datetime(2016, 10, 15, 8, 0, tzinfo=UTC),
        horizon_s=120.0,
        horizontal_error_m=1.5,
        vertical_error_m=2.5,
        time_error_s=3.5,
        point_count=36,
    )
    assert record.horizon_s == 120.0


def test_job_applies_default_retention():
    record = MaterializationJobRecord(
        **version_fields(),
        job_id="job-1",
        kind="batch_statistics",
        batch="b",
    )
    assert record.expires_at == record.generated_at + timedelta(
        days=MATERIALIZATION_JOB_RETENTION_DAYS
    )


def test_records_reject_unknown_fields():
    with pytest.raises(ValidationError):
        BatchStatisticsRecord(
            **version_fields(),
            batch="b",
            flight_count=1,
            unique_callsign_count=1,
            aircraft_type_count=1,
            departure_airport_count=1,
            arrival_airport_count=1,
            total_track_points=1,
            average_track_points=1,
            minimum_track_points=1,
            maximum_track_points=1,
            flights_with_prediction=1,
            total_prediction_snapshots=1,
            missing_callsign_count=0,
            missing_aircraft_type_count=0,
            missing_departure_airport_count=0,
            missing_arrival_airport_count=0,
            missing_iobt_count=0,
            empty_trajectory_count=0,
            flights_without_prediction_count=0,
            not_a_field=True,
        )


def test_contract_declarations_are_consistent():
    # 每个集合都有：集合名、业务键、模型、保留策略；索引字段必须存在于对应模型。
    assert set(MATERIALIZATION_COLLECTIONS) == set(RECORD_MODELS)
    assert set(MATERIALIZATION_COLLECTIONS) == set(MATERIALIZATION_BUSINESS_KEYS)
    assert set(MATERIALIZATION_COLLECTIONS) == set(MATERIALIZATION_RETENTION_DAYS)
    for kind, indexes in MATERIALIZATION_INDEXES.items():
        model_fields = set(RECORD_MODELS[kind].model_fields)
        for index in indexes:
            assert set(index) <= model_fields, f"{kind} 索引字段 {index} 不在模型中"
    # 版本键必须是每个结果模型的公共字段
    for model in RECORD_MODELS.values():
        for key in MATERIALIZATION_VERSION_KEYS:
            assert key in model.model_fields


def test_ttl_fields_are_declared_separately_from_query_indexes():
    """TTL 字段不得同时出现在普通索引声明里。

    同一 key 上并存普通索引与 TTL 索引会被 MongoDB 报 IndexOptionsConflict
    （真实数据建索引时踩到），因此两类声明必须分开。
    """
    assert set(MATERIALIZATION_TTL_INDEXES) <= set(MATERIALIZATION_COLLECTIONS)
    for kind, ttl_fields in MATERIALIZATION_TTL_INDEXES.items():
        model_fields = set(RECORD_MODELS[kind].model_fields)
        single_field_indexes = {
            index[0] for index in MATERIALIZATION_INDEXES.get(kind, ()) if len(index) == 1
        }
        for field in ttl_fields:
            assert field in model_fields, f"{kind} TTL 字段 {field} 不在模型中"
            assert field not in single_field_indexes, (
                f"{kind} 的 TTL 字段 {field} 被重复声明为普通索引"
            )


def test_business_keys_and_version_fields_compose_unique_key():
    # 唯一性 = 业务键 + 版本键：不同算法版本共存，不覆盖（M4 约束）。
    business = MATERIALIZATION_BUSINESS_KEYS["prediction_metrics"]
    assert business == ["batch", "external_id", "snapshot_time", "horizon_s"]
    unique = business + MATERIALIZATION_VERSION_KEYS
    assert len(unique) == len(set(unique))


def test_flight_features_has_aircraft_type_for_filter_index():
    assert "aircraft_type" in FlightFeaturesRecord.model_fields
    assert ["batch", "aircraft_type"] in MATERIALIZATION_INDEXES["flight_features"]


def test_route_statistics_contract():
    record = RouteStatisticsRecord(
        **version_fields(),
        batch="b",
        adep="ZBAA",
        ades="ZSSS",
        flight_count=5,
        aircraft_type_count=2,
        total_track_points=500,
        average_duration_s=7200.0,
    )
    assert record.adep == "ZBAA"
