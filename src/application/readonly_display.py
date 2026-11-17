"""Deterministic display projection of an already authorized, verified receipt.

No database, model, script or additional query is run while opening a source.
The original payload and its digest remain unchanged, including historical receipts.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from pydantic import ValidationError

from agents import analysis_results as builders
from schema.analysis import AnalysisResult

DIAGNOSTICS = {
    "compare_scat_batch_profiles": "scat",
    "diagnose_uav_channels": "uav",
    "diagnose_scat_track": "scat",
    "diagnose_scat_prediction_errors": "scat",
}

DISPLAY_BUILDERS = {
    "get_prediction_metric_summaries": builders.build_cfpl_materialized_analysis,
    "get_worst_prediction_cases": builders.build_cfpl_materialized_analysis,
    "get_scat_batch_overview": builders.build_scat_batch_analysis,
    "search_scat_flights": builders.build_scat_flight_search_analysis,
    "get_scat_flight_overview": builders.build_scat_flight_overview_analysis,
    "get_scat_track_points": builders.build_scat_track_analysis,
    "list_scat_quality_problem_flights": builders.build_scat_quality_problem_flights_analysis,
    "list_scat_quality_clean_flights": builders.build_scat_quality_clean_flights_analysis,
    "inspect_scat_model_data_quality": builders.build_scat_model_quality_analysis,
    "get_scat_airport_stats": builders.build_scat_airport_stats_analysis,
    "get_scat_flight_phase_stats": builders.build_scat_flight_phase_stats_analysis,
    "get_scat_prediction_error_stats": builders.build_scat_prediction_error_analysis,
    "list_uav_models": builders.build_uav_model_analysis,
    "search_uav_flights": builders.build_uav_flight_search_analysis,
    "get_uav_flight_overview": builders.build_uav_flight_overview_analysis,
    "get_uav_samples": builders.build_uav_samples_analysis,
    "get_uav_channel_stats": builders.build_uav_channel_stats_analysis,
    "get_uav_normal_vs_anomaly_stats": builders.build_uav_normal_vs_anomaly_analysis,
    "get_uav_phase_stats": builders.build_uav_flight_phase_stats_analysis,
}


def receipt_display(receipt: dict) -> dict:
    builder = DISPLAY_BUILDERS.get(receipt["operation"])
    operation = receipt["operation"]
    if builder is None and operation not in DIAGNOSTICS:
        return receipt
    result = receipt["result"]
    if result.get("status") != "ok" or result.get("representation") == "page_summary":
        return receipt
    data = result.get("data")
    if not isinstance(data, dict):
        return receipt
    if receipt["operation"] == "get_scat_track_points" and not isinstance(data.get("points"), list):
        return receipt
    if operation in DIAGNOSTICS:
        try:
            analysis = AnalysisResult.model_validate(result.get("analysis_result"))
        except ValidationError:
            return receipt
        if analysis.operation != operation or analysis.dataset != DIAGNOSTICS[operation]:
            return receipt
    else:
        analysis = builder(deepcopy(result))
    if analysis is None:
        return receipt
    origin = result.get("display_time_origin") if result.get("display_only") else None
    if origin and analysis.start_time:
        try:
            delta = (
                datetime.fromisoformat(analysis.start_time) - datetime.fromisoformat(origin)
            ).total_seconds()
            if delta >= 0:
                for block in analysis.blocks:
                    key = (
                        block.time_key
                        if block.type == "trajectory_2d"
                        else block.x_key
                        if block.type == "timeseries" and block.x_key == "time_step_s"
                        else None
                    )
                    if key:
                        for point in block.points:
                            if isinstance(point.get(key), (int, float)):
                                point[key] += delta
                analysis.start_time = origin
        except (ValueError, TypeError):
            pass
    analysis.context.update(
        readonly_result_id=receipt["result_id"],
        readonly_payload_sha256=receipt["payload_sha256"],
        display_scope=(
            "fetched_page"
            if receipt["operation"]
            in {
                "get_scat_track_points",
                "get_uav_samples",
                "search_scat_flights",
                "search_uav_flights",
                "list_scat_quality_problem_flights",
                "list_scat_quality_clean_flights",
                "get_prediction_metric_summaries",
                "get_worst_prediction_cases",
                "diagnose_uav_channels",
                "diagnose_scat_track",
                "diagnose_scat_prediction_errors",
            }
            else "authorized_query"
        ),
    )
    return receipt | {"analysis_result": analysis.model_dump(mode="json")}
