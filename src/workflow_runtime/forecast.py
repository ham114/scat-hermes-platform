"""Point-exact prediction and explicit historical truth matching, without fabricated errors."""

import math

from application.task_store import TaskConflict
from scat.prediction.alignment import build_track, haversine_metres, interpolate_track
from scat.prediction.baselines import ConstantVelocityHeadingPredictor
from scat.prediction.interfaces import PredictionRequest
from schema.analysis import AnalysisResult, Trajectory2dBlock, Trajectory2dSeries
from workflow_runtime.windows import verified_windows


def analysis_page(observed, fixes, *, external_id, model, mode, requested_as_of=None):
    start = observed[0].time
    rows = []
    # The last observed point anchors the forecast line but is not a predicted target.
    for segment, points in [("observed", observed), ("predicted", (observed[-1], *fixes))]:
        rows.extend(
            {
                "longitude": p.lon,
                "latitude": p.lat,
                "altitude_m": p.altitude_m,
                "time_step_s": (p.time - start).total_seconds(),
                "trajectory_id": model,
                "segment": segment,
                "snapshot_id": external_id + ":" + observed[-1].time.isoformat(),
            }
            for p in points
        )
    return AnalysisResult(
        schema_version="1.6",
        dataset="scat",
        operation="point_exact_prediction",
        title="未来预测（无未来真值，不计算误差）" if mode == "future_forecast" else "历史回测预测",
        start_time=start.isoformat(),
        context={
            "prediction_mode": mode,
            "external_id": external_id,
            "forecast_origin": observed[-1].time.isoformat(),
            "observation_points": len(observed),
            "observation_span_s": (observed[-1].time - start).total_seconds(),
            "truth_status": "not_available" if mode == "future_forecast" else "matched_separately",
            **(
                {
                    "requested_as_of": requested_as_of.isoformat(),
                    "horizon_reference": "requested_as_of",
                }
                if requested_as_of
                else {}
            ),
        },
        blocks=[
            Trajectory2dBlock(
                title="观测与预测轨迹",
                x_key="longitude",
                x_label="经度",
                x_unit="degree",
                y_key="latitude",
                y_label="纬度",
                y_unit="degree",
                time_key="time_step_s",
                path_key="trajectory_id",
                path_label="模型",
                segment_key="segment",
                segment_label="观测/预测",
                snapshot_key="snapshot_id",
                snapshot_label="航班与预测起点",
                series=[Trajectory2dSeries(key="altitude_m", label="高度", unit="m")],
                points=rows,
            )
        ],
    ).model_dump(mode="json")


def run_windowed_trial(frozen, proposal, trial):
    from workflow_runtime.numerics import predictor

    mode = "future_forecast" if proposal.kind == "forecast" else "historical_backtest"
    model = predictor(trial["configuration"])
    records, predictions, pages, failed, exclusions = [], [], [], [], {}
    for flight in frozen["flights"]:
        try:
            track = build_track(flight["data"]["plots"])[0]
            for observed, targets in verified_windows(flight, proposal):
                # An exact count is the model's complete observation input, not a seconds alias.
                current = model
                if (
                    proposal.observation_last_n
                    and model.predictor_id == "constant-velocity-heading"
                ):
                    current = ConstantVelocityHeadingPredictor(
                        window_s=(observed[-1].time - observed[0].time).total_seconds()
                    )
                result = current.predict(PredictionRequest(observed=observed, target_times=targets))
                if len(result.fixes) != len(targets) or any(
                    fix.time != target
                    or not math.isfinite(fix.lat)
                    or not math.isfinite(fix.lon)
                    or not -90 <= fix.lat <= 90
                    or not -180 <= fix.lon <= 180
                    for fix, target in zip(result.fixes, targets, strict=True)
                ):
                    raise ValueError("Predictor output does not match the approved targets")
                page = analysis_page(
                    observed,
                    result.fixes,
                    external_id=flight["external_id"],
                    model=model.predictor_id,
                    mode=mode,
                    requested_as_of=proposal.as_of,
                )
                item = {
                    "external_id": flight["external_id"],
                    "forecast_origin": observed[-1].time.isoformat(),
                    "observation_points": len(observed),
                    "points": [],
                }
                reference_time = proposal.as_of or observed[-1].time
                if proposal.as_of:
                    item.update(
                        requested_as_of=proposal.as_of.isoformat(),
                        horizon_reference="requested_as_of",
                    )
                for horizon, fix in zip(proposal.horizons_s, result.fixes, strict=True):
                    point = {
                        "timestamp": fix.time.isoformat(),
                        "horizon_s": horizon,
                        "lat": fix.lat,
                        "lon": fix.lon,
                        "altitude_m": fix.altitude_m,
                    }
                    if mode == "historical_backtest":
                        actual, reason = interpolate_track(
                            track, fix.time, max_gap_s=proposal.max_track_gap_s
                        )
                        if actual is not None:
                            records.append(
                                {
                                    "external_id": flight["external_id"],
                                    "cutoff_time": reference_time.isoformat(),
                                    "horizon_s": horizon,
                                    "error_m": haversine_metres(
                                        fix.lat, fix.lon, actual.lat, actual.lon
                                    ),
                                }
                            )
                            point["truth_status"] = "matched"
                        else:
                            key = reason.value if reason else "actual_unavailable"
                            exclusions[key] = exclusions.get(key, 0) + 1
                            point["truth_status"] = key
                    else:
                        point["truth_status"] = "not_available_future"
                    item["points"].append(point)
                predictions.append(item)
                pages.append(page)
        except (ValueError, ArithmeticError, TaskConflict):
            failed.append(
                {"external_id": flight["external_id"], "reason": "predictor_or_window_rejected"}
            )
            if proposal.failure_policy == "stop":
                break
    return {
        "trial_id": trial["trial_id"],
        "configuration": trial["configuration"],
        "repeat": trial["repeat"],
        "protocol": "point-exact-prediction/v1",
        "prediction_mode": mode,
        "units": "m",
        "samples": records,
        "predictions": predictions,
        "prediction_count": sum(len(p["points"]) for p in predictions),
        "analysis_results": pages,
        "failed_flights": failed,
        "exclusions": exclusions,
        "selected_flights": len(frozen["flights"]),
        "status": "partial" if predictions and failed else "succeeded" if predictions else "failed",
    }
