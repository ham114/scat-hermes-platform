import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from scat.repositories import flight_repository as repo_module
from scat.repositories.flight_repository import ScatFlightRepository

_original_partitions = repo_module._GBAR_QUALITY_PARTITIONS


@pytest.fixture
def single_partition(monkeypatch):
    """默认用单分区跑管线形状断言，避免测试依赖并发合并。"""
    monkeypatch.setattr(repo_module, "_GBAR_QUALITY_PARTITIONS", 1)
    yield
    monkeypatch.setattr(repo_module, "_GBAR_QUALITY_PARTITIONS", _original_partitions)


class RepositoryUnderTest(ScatFlightRepository):
    def __init__(self, collection: Mock) -> None:
        self._collection = collection

    @property
    def collection(self) -> Mock:
        return self._collection

    @property
    def analytics_collection(self) -> Mock:
        return self._collection


@pytest.mark.asyncio
async def test_search_flights_uses_requested_stable_sort():
    cursor = Mock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[])
    collection = Mock()
    collection.find.return_value = cursor
    repository = RepositoryUnderTest(collection)

    await repository.search_flights(
        start_time=datetime(2016, 10, 20, tzinfo=UTC),
        end_time=datetime(2016, 10, 21, tzinfo=UTC),
        sort_by="callsign",
        sort_order="desc",
        limit=20,
        skip=40,
    )

    filters = collection.find.call_args.args[0]
    assert filters["identity.iobt_utc"] == {
        "$gte": datetime(2016, 10, 20, tzinfo=UTC),
        "$lt": datetime(2016, 10, 21, tzinfo=UTC),
    }
    cursor.sort.assert_called_once_with(
        [
            ("identity.callsign", -1),
            ("source.batch", -1),
            ("source.external_id", -1),
        ]
    )
    cursor.skip.assert_called_once_with(40)
    cursor.limit.assert_called_once_with(20)


@pytest.mark.asyncio
async def test_track_query_filters_before_bounded_sampling_window():
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[{"plots": [], "total_points": 0}])
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    await repository.get_track_points(
        batch="batch",
        external_id="1",
        start=10,
        limit=500,
        start_time="2016-10-20T18:00:00",
        end_time="2016-10-20T19:00:00",
        every_nth=20,
    )

    pipeline = collection.aggregate.await_args.args[0]
    filter_stage = pipeline[1]["$project"]["filtered_plots"]["$filter"]
    assert filter_stage["cond"] == {
        "$and": [
            {"$gte": ["$$plot.time_of_track", "2016-10-20T18:00:00"]},
            {"$lt": ["$$plot.time_of_track", "2016-10-20T19:00:00"]},
        ]
    }
    assert pipeline[2]["$project"]["plots"]["$slice"] == [
        "$filtered_plots",
        10,
        10000,
    ]


@pytest.mark.asyncio
async def test_gbar_candidate_query_projects_only_model_fields_and_bounds_tail():
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[{"points": []}])
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    result = await repository.get_gbar_input_candidates(
        batch="batch",
        external_id="1",
        end_time="2016-10-20T19:00:00",
        limit=50_000,
    )

    pipeline = collection.aggregate.await_args.args[0]
    assert pipeline[0]["$match"]["source.external_id"] == "1"
    assert pipeline[3] == {"$match": {"plots.time_of_track": {"$lte": "2016-10-20T19:00:00"}}}
    point_projection = pipeline[4]["$project"]["point"]
    assert set(point_projection) == {
        "source_index",
        "timestamp",
        "lat",
        "lon",
        "measured_flight_level",
        "vx",
        "vy",
        "rocd",
    }
    assert pipeline[6] == {"$limit": 2_000}
    assert result == {"points": []}


@pytest.mark.asyncio
async def test_random_gbar_sample_requires_observation_and_holdout_points():
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "selection": [
                    {"external_id": "100002"},
                    {"external_id": "100014"},
                ],
                "summary": [{"eligible_count": 27}],
            }
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    result = await repository.sample_gbar_flight_ids(
        batch="batch",
        sample_size=2,
        observation_points=300,
        aircraft_type="A320",
        adep="ESSA",
    )

    pipeline = collection.aggregate.await_args.args[0]
    assert pipeline[0]["$match"]["source.batch"] == "batch"
    assert pipeline[0]["$match"]["identity.aircraft_type"] == "A320"
    assert pipeline[0]["$match"]["identity.adep"] == "ESSA"
    assert pipeline[1]["$match"]["$expr"]["$gte"][1] == 400
    assert pipeline[2]["$facet"]["selection"][0] == {"$sample": {"size": 2}}
    assert result == {
        "external_ids": ["100002", "100014"],
        "eligible_count": 27,
    }


def test_search_filter_supports_inclusion_exclusion_prefix_and_data_shape():
    filters = ScatFlightRepository._build_search_filter(
        batch="batch",
        callsign_prefix="SAS",
        aircraft_types=("A319", "A321"),
        exclude_aircraft_types=("A320",),
        exclude_adeps=("ESSA",),
        external_ids=("100001", "100002"),
        min_track_points=300,
        max_track_points=900,
        has_fpl=True,
        has_scat_prediction=False,
    )

    assert filters["identity.callsign"] == {"$regex": "^SAS"}
    assert filters["identity.aircraft_type"] == {
        "$in": ["A319", "A321"],
        "$nin": ["A320"],
        "$exists": True,
    }
    assert filters["identity.adep"] == {"$nin": ["ESSA"], "$exists": True}
    assert filters["source.external_id"] == {"$in": ["100001", "100002"]}
    assert len(filters["$expr"]["$and"]) == 4


@pytest.mark.asyncio
async def test_prediction_page_projects_metadata_instead_of_route_arrays():
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "snapshots": [{"time_stamp": "2016-10-20T20:00:00", "route_point_count": 12}],
                "total_snapshots": 1,
            }
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    result = await repository.list_prediction_snapshots(
        batch="batch",
        external_id="1",
        start=0,
        limit=20,
    )

    pipeline = collection.aggregate.await_args.args[0]
    item_projection = pipeline[2]["$project"]["snapshots"]["$map"]["in"]
    assert set(item_projection) == {"time_stamp", "route_point_count"}
    assert "route" not in item_projection
    assert result["returned_snapshots"] == 1


@pytest.mark.asyncio
async def test_flight_volume_uses_bounded_utc_date_truncation():
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[])
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    await repository.get_flight_volume(batch="batch", granularity="hour")

    pipeline = collection.aggregate.await_args.args[0]
    assert pipeline[0]["$match"]["source.batch"] == "batch"
    assert pipeline[3]["$group"]["_id"]["$dateTrunc"] == {
        "date": "$iobt",
        "unit": "hour",
        "timezone": "UTC",
    }
    cursor.to_list.assert_awaited_once_with(length=None)


@pytest.mark.asyncio
async def test_airport_distributions_use_bounded_facets():
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[])
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    result = await repository.get_airport_distributions(batch="batch", limit=7)

    pipeline = collection.aggregate.await_args.args[0]
    facets = pipeline[1]["$facet"]
    assert facets["departures"][0] == {"$match": {"identity.adep": {"$type": "string"}}}
    assert facets["departures"][1]["$group"]["_id"] == "$identity.adep"
    assert facets["arrivals"][1]["$group"]["_id"] == "$identity.ades"
    assert facets["departures"][3] == {"$limit": 7}
    assert result == {"departures": [], "arrivals": []}


@pytest.mark.asyncio
async def test_batch_data_quality_returns_zero_shape_for_empty_batch():
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[])
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    result = await repository.get_batch_data_quality(batch="batch")

    assert result == {
        "missing_callsign_count": 0,
        "missing_aircraft_type_count": 0,
        "missing_departure_airport_count": 0,
        "missing_arrival_airport_count": 0,
        "missing_iobt_count": 0,
        "empty_trajectory_count": 0,
        "flights_without_prediction_count": 0,
    }


@pytest.mark.asyncio
async def test_model_data_quality_summary_is_one_server_side_full_scan(single_partition):
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "summary": [
                    {
                        "flight_count": 13_138,
                        "point_count": 100_000,
                        "flights_with_missing": 12,
                        "flights_with_outliers": 5,
                        "flights_with_spikes": 3,
                        "flights_with_any_problem": 20,
                        "clean_flight_count": 13_118,
                        "missing_lat_count": 9,
                        "outlier_lat_count": 2,
                        "spike_lat_count": 3,
                    }
                ],
                "batches": [],
                "clean_samples": [
                    {
                        "batch": "scat20161015_20161021",
                        "external_id": "100002",
                        "original_filename": "100002.json",
                        "callsign": "SAS001",
                        "point_count": 800,
                    }
                ],
            }
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    collection.count_documents = AsyncMock(return_value=13_138)
    repository = RepositoryUnderTest(collection)

    result = await repository.get_model_data_quality_summary()

    pipeline = collection.aggregate.await_args.args[0]
    assert pipeline[0]["$match"]["$and"][0] == {"source.system": "scat-public-dataset"}
    assert pipeline[0]["$match"]["$and"][1]["$or"] == [
        {"id": {"$mod": [1, 0]}},
        {"id": {"$not": {"$type": "number"}}},
    ]
    assert "$set" in pipeline[1]
    assert "$addFields" in pipeline[3]
    assert "$facet" in pipeline[-1]
    assert collection.aggregate.await_args.kwargs == {
        "allowDiskUse": True,
        "maxTimeMS": 180_000,
    }
    assert result["summary"]["flight_count"] == 13_138
    assert result["summary"]["missing_value_count"] == 9
    assert result["summary"]["spike_value_count"] == 3
    assert result["summary"]["clean_flight_count"] == 13_118
    assert result["clean_trajectory_samples"][0]["original_filename"] == "100002.json"
    assert result["clean_trajectory_samples"][0]["trajectory_key"].endswith("/100002")
    lat = next(item for item in result["features"] if item["feature"] == "lat")
    assert lat["out_of_range_value_count"] == 2
    assert lat["adjacent_point_spike_count"] == 3
    assert result["temporal_spikes_included"] is True
    assert result["partition_coverage_verified"] is True


@pytest.mark.asyncio
async def test_model_data_quality_problem_flight_listing_is_paginated(single_partition):
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "total": [{"count": 120}],
                "page": [
                    {
                        "batch": "scat20161015_20161021",
                        "external_id": "100016",
                        "point_count": 900,
                        "missing_point_count": 3,
                        "outlier_point_count": 7,
                        "spike_value_count": 2,
                    }
                ],
            }
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    result = await repository.list_model_data_quality_problem_flights(
        batch="scat20161015_20161021",
        limit=20,
        offset=0,
        sort_by="spike_value_count",
    )

    pipeline = collection.aggregate.await_args.args[0]
    assert pipeline[0]["$match"]["$and"][0]["source.batch"] == ("scat20161015_20161021")
    page_stages = pipeline[-1]["$facet"]["page"]
    assert page_stages[0] == {"$sort": {"spike_value_count": -1, "external_id": 1}}
    assert page_stages[1] == {"$limit": 20}
    assert result["total"] == 120
    assert result["has_more"] is True
    assert result["items"][0]["external_id"] == "100016"


@pytest.mark.asyncio
async def test_model_data_quality_summary_merges_partitions(monkeypatch):
    monkeypatch.setattr(repo_module, "_GBAR_QUALITY_PARTITIONS", 2)

    def partition_document(flight_count: int) -> dict:
        return {
            "summary": [
                {
                    "flight_count": flight_count,
                    "point_count": 1000,
                    "flights_with_any_problem": 1,
                    "clean_flight_count": flight_count - 1,
                    "missing_lat_count": 1,
                    "outlier_lat_count": 0,
                    "spike_lat_count": 0,
                }
            ],
            "batches": [
                {
                    "batch": "scat20161015_20161021",
                    "flight_count": flight_count,
                    "flights_with_any_problem": 1,
                    "clean_flight_count": flight_count - 1,
                }
            ],
        }

    cursor = Mock()
    cursor.to_list = AsyncMock(
        side_effect=[
            [partition_document(10)],
            [partition_document(20)],
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    collection.count_documents = AsyncMock(return_value=30)
    repository = RepositoryUnderTest(collection)

    result = await repository.get_model_data_quality_summary()

    assert collection.aggregate.await_count == 2
    first_match = collection.aggregate.await_args_list[0].args[0][0]["$match"]
    assert first_match["$and"][1]["$or"][0]["id"] == {"$mod": [2, 0]}
    assert result["summary"]["flight_count"] == 30
    assert result["summary"]["clean_flight_count"] == 28
    assert result["batches"][0]["flight_count"] == 30


@pytest.mark.asyncio
async def test_model_data_quality_clean_flight_listing_returns_identifiers(
    single_partition,
):
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "total": [{"count": 10_552}],
                "page": [
                    {
                        "batch": "scat20161015_20161021",
                        "external_id": "100002",
                        "original_filename": "100002.json",
                        "callsign": "SAS001",
                        "point_count": 800,
                    }
                ],
            }
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    repository = RepositoryUnderTest(collection)

    result = await repository.list_model_data_quality_clean_flights(limit=5)

    pipeline = collection.aggregate.await_args.args[0]
    assert pipeline[4]["$match"]["$expr"] == repository._quality_clean_condition()
    assert result["total"] == 10_552
    assert result["items"][0]["original_filename"] == "100002.json"
    assert result["items"][0]["trajectory_key"] == ("scat20161015_20161021/100002")


@pytest.mark.asyncio
async def test_model_data_quality_summary_rejects_incomplete_partition_coverage(
    single_partition,
):
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[{"summary": [{"flight_count": 1}], "batches": []}])
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    collection.count_documents = AsyncMock(return_value=2)

    with pytest.raises(RuntimeError, match="coverage mismatch"):
        await RepositoryUnderTest(collection).get_model_data_quality_summary()


@pytest.mark.asyncio
async def test_model_data_quality_summary_can_return_compact_snapshot_rows(single_partition):
    quality_row = {
        "batch": "scat20161015_20161021",
        "external_id": "100002",
        "original_filename": "100002.json",
        "callsign": "SAS94M",
        "point_count": 524,
        "missing_point_count": 0,
        "outlier_point_count": 0,
        "spike_value_count": 0,
    }
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "summary": [
                    {
                        "flight_count": 1,
                        "point_count": 524,
                        "flights_with_any_problem": 0,
                        "clean_flight_count": 1,
                    }
                ],
                "batches": [],
                "clean_samples": [quality_row],
                "flight_rows": [quality_row],
            }
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    collection.count_documents = AsyncMock(return_value=1)

    result = await RepositoryUnderTest(collection).get_model_data_quality_summary(
        include_flight_rows=True
    )

    facet = collection.aggregate.await_args.args[0][-1]["$facet"]
    assert "flight_rows" in facet
    assert result["_quality_flight_rows"][0]["original_filename"] == "100002.json"
    assert result["_quality_flight_rows"][0]["trajectory_key"].endswith("/100002")


@pytest.mark.asyncio
async def test_gbar_future_projection_contains_all_six_regression_targets():
    cursor = Mock()
    cursor.to_list = AsyncMock(return_value=[])
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)

    await RepositoryUnderTest(collection).get_gbar_future_positions(
        batch="batch",
        external_id="100002",
        after_time="2016-10-20T20:08:00.000000",
    )

    pipeline = collection.aggregate.await_args.args[0]
    projection = pipeline[-1]["$project"]
    assert {
        "lat",
        "lon",
        "measured_flight_level",
        "vx",
        "vy",
        "rocd",
    } <= set(projection)


@pytest.mark.asyncio
async def test_model_data_quality_summary_rejects_classification_mismatch(single_partition):
    cursor = Mock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "summary": [
                    {
                        "flight_count": 2,
                        "flights_with_any_problem": 1,
                        "clean_flight_count": 0,
                    }
                ],
                "batches": [],
            }
        ]
    )
    collection = Mock()
    collection.aggregate = AsyncMock(return_value=cursor)
    collection.count_documents = AsyncMock(return_value=2)

    with pytest.raises(RuntimeError, match="classification mismatch"):
        await RepositoryUnderTest(collection).get_model_data_quality_summary()


@pytest.mark.asyncio
async def test_model_data_quality_problem_flight_listing_rejects_unknown_sort():
    repository = RepositoryUnderTest(Mock())

    with pytest.raises(ValueError, match="sort_by"):
        await repository.list_model_data_quality_problem_flights(sort_by="not_a_field")


def test_quality_spike_expression_guards_types_ranges_and_time_gaps():
    expression = ScatFlightRepository._quality_spike_middle_cond(
        {"$ifNull": ["$plots", []]},
        {"$ifNull": ["$_quality_times", []]},
        "$$i",
        path="I062/185.vx",
        threshold=80.0,
    )
    serialized = json.dumps(
        [
            ScatFlightRepository._quality_time_array({"$ifNull": ["$plots", []]}),
            expression,
        ]
    )

    assert '"$isNumber"' in serialized
    assert '"$convert"' in serialized
    assert "time_of_track" in serialized
    assert str(int(repo_module.SCAT_GBAR_MAXIMUM_SOURCE_GAP_SECONDS * 1000)) in (serialized)
