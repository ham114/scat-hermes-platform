"""Explicitly synthetic protocol fixtures for the composed-skill acceptance probe."""

from __future__ import annotations

from application.readonly import ReadOnlyApplication
from application.readonly_extensions import ALL_READ_ONLY_TOOLS


def build_synthetic_skill_application():
    def operation(name):
        async def execute(**kwargs):
            data = {"echo": kwargs}
            if name == "search_scat_flights":
                data = {
                    "total": 1,
                    "count": 1,
                    "items": [
                        {
                            "source": {
                                "batch": kwargs["batch"],
                                "external_id": kwargs["batch"] + "-001",
                            },
                            "identity": {"callsign": kwargs.get("callsign") or "TEST123"},
                        }
                    ],
                }
            elif name == "inspect_scat_collection_data_quality":
                data = {
                    "batch": kwargs["batch"],
                    "total_flights": 1,
                    "total_points": 3,
                    "missing_point_count": 0,
                    "outlier_point_count": 0,
                    "policy_version": "SYNTH-quality-v1",
                }
            elif name == "get_scat_track_points":
                data = {
                    "pagination": {
                        "start": 0,
                        "returned_points": min(kwargs["limit"], 3),
                        "total_points": 3,
                        "has_more": False,
                        "next_start": None,
                    },
                    "points": [
                        {
                            "sequence": i,
                            "timestamp": f"2020-01-01T00:00:0{i}Z",
                            "position": {
                                "lat_deg": 30 + i * 0.01,
                                "lon_deg": 120 + i * 0.01,
                                "altitude_m": 1000 + i * 10,
                            },
                        }
                        for i in range(min(kwargs["limit"], 3))
                    ],
                }
            elif name == "search_scat_knowledge":
                data = {
                    "hits": [
                        {
                            "source_id": "SYNTH-QUALITY-001",
                            "text": "合成测试口径：缺失率按缺失点数除以检查点数，必须标明范围。",
                        }
                    ]
                }
            elif name == "list_uav_models":
                data = {"models": [{"model_code": "SYNTH-UAV", "flight_count": 1}]}
            elif name == "get_uav_samples":
                data = {
                    "samples": [
                        {"sequence": i, "channels": {"cmp_comb_alt": 1000 + i}}
                        for i in range(min(kwargs["limit"], 3))
                    ],
                    "synthetic_unit": "m",
                }
            return {
                "status": "ok",
                "synthetic": True,
                "operation": name,
                "query": kwargs,
                "data": data,
            }

        return execute

    return ReadOnlyApplication(
        {name: operation(name) for name in ALL_READ_ONLY_TOOLS}, specs=ALL_READ_ONLY_TOOLS
    )
