"""Unit tests for the UAV flight service (no database required)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from uav.schemas.channels import CMP_CHANNELS
from uav.services.uav_service import UavFlightService


def _flight_row(
    external_id: str = "20230330122740_DTC_53.1394B",
    **identity_overrides,
) -> dict:
    identity = {
        "mission": "DTC",
        "flight_seq": 53,
        "takeoff_at": datetime(2023, 3, 30, 4, 27, 40, tzinfo=UTC),
        "duration_s": 4505.0,
        "sample_count": 4506,
        "sample_rate_hz": 1.0,
        "flight_kind": "normal",
        "label": None,
        "platform": "fixed-wing-uav",
    }
    identity.update(identity_overrides)
    return {
        "source": {
            "system": "uav-internal-sensor",
            "external_id": external_id,
            "model": "0001",
            "tail_number": None,
        },
        "identity": identity,
    }


@pytest.mark.asyncio
async def test_list_flights_flattens_rows_and_normalizes_time():
    repository = Mock()
    repository.build_list_filter = Mock(return_value={"_filter": True})
    repository.count_all = AsyncMock(return_value=1)
    repository.list_flights = AsyncMock(return_value=[_flight_row()])
    service = UavFlightService(repository=repository)

    result = await service.list_flights(model="0001", limit=20, offset=0)

    assert result["status"] == "ok"
    page = result["data"]
    assert page["pagination"] == {
        "offset": 0,
        "limit": 20,
        "returned": 1,
        "total": 1,
        "has_more": False,
    }
    item = page["items"][0]
    assert item["external_id"] == "20230330122740_DTC_53.1394B"
    assert item["takeoff_at"] == "2023-03-30T04:27:40Z"
    assert item["flight_seq"] == 53
    assert item["label"] is None


@pytest.mark.asyncio
async def test_list_flights_rejects_unknown_sort_field():
    repository = Mock()
    repository.build_list_filter = Mock(return_value={})
    repository.count_all = AsyncMock(return_value=0)
    repository.list_flights = AsyncMock(return_value=[])
    service = UavFlightService(repository=repository)

    result = await service.list_flights(sort_by="not_a_field")

    assert result["query"]["sort_by"] == "takeoff_at"


@pytest.mark.asyncio
async def test_get_flight_not_found():
    repository = Mock()
    repository.get_by_external_id = AsyncMock(return_value=None)
    service = UavFlightService(repository=repository)

    result = await service.get_flight("missing_id")

    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_get_samples_downsamples_and_filters_groups():
    repository = Mock()
    repository.get_sample_window = AsyncMock(
        return_value={
            "source": {"external_id": "x"},
            "identity": {
                "sample_count": 10,
                "takeoff_at": datetime(2023, 3, 30, 4, 27, 40, tzinfo=UTC),
            },
            "samples": [
                {"seq": 4, "t_s": 4.0, "cmp_comb_alt": 100.0, "cmp_pitch": 1.0},
                {"seq": 5, "t_s": 5.0, "cmp_comb_alt": 101.0, "cmp_pitch": 2.0},
                {"seq": 6, "t_s": 6.0, "cmp_comb_alt": 102.0, "cmp_pitch": 3.0},
                {"seq": 7, "t_s": 7.0, "cmp_comb_alt": 103.0, "cmp_pitch": 4.0},
            ],
        }
    )
    service = UavFlightService(repository=repository)

    result = await service.get_samples(
        "x", start=4, limit=4, every_nth=2, fields={"position"}
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["returned"] == 2
    assert data["start_time"] == "2023-03-30T04:27:44Z"
    assert data["has_more"] is True
    assert data["next_start"] == 8
    first = data["samples"][0]
    assert first["seq"] == 4
    assert "cmp_comb_alt" in first
    assert "cmp_pitch" not in first
    assert "t_s" in first


@pytest.mark.asyncio
async def test_get_samples_rejects_unknown_field_group():
    repository = Mock()
    repository.get_sample_window = AsyncMock()
    service = UavFlightService(repository=repository)

    with pytest.raises(ValueError):
        await service.get_samples("x", fields={"bogus"})

    with pytest.raises(ValueError, match="unknown channels"):
        await service.get_samples("x", channels={"bogus"})

    repository.get_sample_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_samples_filters_exact_channels_and_validates_group_membership():
    repository = Mock()
    repository.get_sample_window = AsyncMock(
        return_value={
            "identity": {
                "sample_count": 1,
                "takeoff_at": datetime(2023, 3, 30, 4, 27, 40, tzinfo=UTC),
            },
            "samples": [
                {
                    "seq": 0,
                    "t_s": 0.0,
                    "cmp_comb_alt": 100.0,
                    "cmp_comb_lon": 116.0,
                }
            ],
        }
    )
    service = UavFlightService(repository=repository)

    result = await service.get_samples(
        "x",
        fields={"position"},
        channels={"cmp_comb_alt"},
    )

    assert result["data"]["samples"] == [
        {"seq": 0, "t_s": 0.0, "cmp_comb_alt": 100.0}
    ]
    assert result["query"]["channels"] == ["cmp_comb_alt"]

    with pytest.raises(ValueError, match="outside selected groups"):
        await service.get_samples(
            "x",
            fields={"position"},
            channels={"cmp_pitch"},
        )


@pytest.mark.asyncio
async def test_list_models_normalizes_rows():
    repository = Mock()
    repository.list_models = AsyncMock(
        return_value=[
            {
                "_id": "0001",
                "flight_count": 14,
                "earliest_takeoff": datetime(2022, 7, 23, 4, 40, 28, tzinfo=UTC),
                "latest_takeoff": None,
            }
        ]
    )
    service = UavFlightService(repository=repository)

    result = await service.list_models()

    data = result["data"]
    assert data["items"][0]["model"] == "0001"
    assert data["items"][0]["flight_count"] == 14
    assert data["items"][0]["earliest_takeoff_utc"] == "2022-07-23T04:40:28Z"


def test_channel_catalog_is_consistent():
    assert len(CMP_CHANNELS) == 34
    keys = [channel.key for channel in CMP_CHANNELS]
    assert len(set(keys)) == 34
    assert all(channel.group for channel in CMP_CHANNELS)


@pytest.mark.asyncio
async def test_channel_stats_aggregates_rows_with_catalog():
    repository = Mock()
    repository.aggregate_channel_stats = AsyncMock(
        return_value=[
            {
                "channel": "cmp_comb_alt",
                "flight_count": 42,
                "total_samples": 300000,
                "weighted_mean": 800.5,
                "min": 10.0,
                "max": 2500.0,
                "missing_total": 3,
            },
        ]
    )
    service = UavFlightService(repository=repository)

    result = await service.get_channel_stats(
        channels={"cmp_comb_alt"}, model="0007"
    )

    repository.aggregate_channel_stats.assert_awaited_once_with(
        channels=["cmp_comb_alt"], model="0007", flight_kind=None
    )
    item = result["data"]["items"][0]
    assert item["channel"] == "cmp_comb_alt"
    assert item["label"] == "组合高度"
    assert item["unit"] == "m"
    assert item["weighted_mean"] == 800.5
    assert result["data"]["flights_included"] == 42


@pytest.mark.asyncio
async def test_channel_stats_rejects_unknown_channel():
    repository = Mock()
    service = UavFlightService(repository=repository)

    with pytest.raises(ValueError, match="不支持的 UAV 通道"):
        await service.get_channel_stats(channels={"not_a_channel"})


@pytest.mark.asyncio
async def test_normal_vs_anomaly_stats_computes_deltas():
    repository = Mock()
    repository.aggregate_kind_channel_stats = AsyncMock(
        return_value=[
            {
                "channel": "cmp_pitch",
                "flight_kind": "normal",
                "flight_count": 39,
                "total_samples": 250000,
                "weighted_mean": 2.0,
                "min": -5.0,
                "max": 8.0,
                "missing_total": 0,
            },
            {
                "channel": "cmp_pitch",
                "flight_kind": "anomaly_sample",
                "flight_count": 3,
                "total_samples": 9000,
                "weighted_mean": 6.0,
                "min": -2.0,
                "max": 20.0,
                "missing_total": 0,
            },
        ]
    )
    service = UavFlightService(repository=repository)

    result = await service.get_normal_vs_anomaly_stats(channels={"cmp_pitch"})

    item = result["data"]["items"][0]
    assert item["normal"]["weighted_mean"] == 2.0
    assert item["anomaly"]["weighted_mean"] == 6.0
    assert item["delta_mean"] == 4.0
    assert item["delta_relative"] == 2.0


@pytest.mark.asyncio
async def test_flight_phase_stats_formats_phases():
    repository = Mock()
    repository.get_flight_phase_stats = AsyncMock(
        return_value=[
            {
                "_id": "climb",
                "point_count": 100,
                "start_t_s": 10.0,
                "end_t_s": 60.0,
                "min_altitude_m": 100.0,
                "max_altitude_m": 800.0,
                "average_altitude_m": 450.0,
                "average_ground_speed_mps": 30.0,
                "maximum_ground_speed_mps": 35.0,
                "average_vertical_speed_mps": 3.5,
            },
            {
                "_id": "level",
                "point_count": 400,
                "start_t_s": 61.0,
                "end_t_s": 460.0,
                "min_altitude_m": 790.0,
                "max_altitude_m": 810.0,
                "average_altitude_m": 800.0,
                "average_ground_speed_mps": 28.0,
                "maximum_ground_speed_mps": 30.0,
                "average_vertical_speed_mps": 0.1,
            },
        ]
    )
    service = UavFlightService(repository=repository)

    result = await service.get_flight_phase_stats(
        "20230330122740_DTC_53.1394B"
    )

    data = result["data"]
    assert data["total_points"] == 500
    assert data["phase_threshold_mps"] == 1.0
    climb = data["phases"][0]
    assert climb["phase"] == "climb"
    assert climb["duration_seconds"] == 50.0
    assert climb["point_ratio"] == 0.2
    assert climb["altitude"]["maximum_m"] == 800.0


@pytest.mark.asyncio
async def test_flight_phase_stats_not_found():
    repository = Mock()
    repository.get_flight_phase_stats = AsyncMock(return_value=None)
    service = UavFlightService(repository=repository)

    result = await service.get_flight_phase_stats("missing")

    assert result["status"] == "not_found"
