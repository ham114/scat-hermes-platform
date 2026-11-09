"""UAV 无人机架次业务服务。

位于 Repository 与 REST API 之间，负责参数校验、结果标准化与降采样。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from uav.repositories.uav_repository import SORT_FIELDS, UavFlightRepository
from uav.schemas.channels import (
    CHANNEL_BY_KEY,
    CHANNEL_GROUPS,
    GROUP_KEYS,
)

SAMPLE_LIMIT_DEFAULT = 200
SAMPLE_LIMIT_MAX = 1000
EVERY_NTH_MAX = 20


class UavFlightService:
    """UAV 架次查询服务。"""

    def __init__(self, repository: UavFlightRepository | None = None) -> None:
        self.repository = repository or UavFlightRepository()

    # =========================================================
    # 公共查询方法
    # =========================================================

    async def list_models(self) -> dict[str, Any]:
        models = await self.repository.list_models()
        items = [
            {
                "model": row["_id"] if row.get("_id") is not None else "unknown",
                "flight_count": int(row.get("flight_count", 0)),
                "earliest_takeoff_utc": self._iso_z(row.get("earliest_takeoff")),
                "latest_takeoff_utc": self._iso_z(row.get("latest_takeoff")),
            }
            for row in models
        ]
        return {
            "status": "ok",
            "data": {
                "pagination": {
                    "offset": 0,
                    "limit": len(items),
                    "returned": len(items),
                    "total": len(items),
                    "has_more": False,
                },
                "items": items,
            },
        }

    async def list_flights(
        self,
        *,
        model: str | None = None,
        tail_number: str | None = None,
        mission: str | None = None,
        flight_kind: str | None = None,
        label: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        sort_by: str = "takeoff_at",
        sort_order: int = 1,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        sort_by = sort_by if sort_by in SORT_FIELDS else "takeoff_at"
        filters = self.repository.build_list_filter(
            model=model,
            tail_number=tail_number,
            mission=mission,
            flight_kind=flight_kind,
            label=label,
            start_time=start_time,
            end_time=end_time,
        )
        safe_offset = max(offset, 0)
        safe_limit = min(max(limit, 1), 100)
        total, rows = await self._gather(
            self.repository.count_all(filters),
            self.repository.list_flights(
                filters=filters,
                sort_field=sort_by,
                sort_order=sort_order,
                limit=safe_limit,
                skip=safe_offset,
            ),
        )
        items = [self._flatten_flight(row) for row in rows]
        return {
            "status": "ok",
            "query": {
                "sort_by": sort_by,
                "sort_order": sort_order,
                "limit": safe_limit,
                "offset": safe_offset,
            },
            "data": {
                "pagination": {
                    "offset": safe_offset,
                    "limit": safe_limit,
                    "returned": len(items),
                    "total": total,
                    "has_more": safe_offset + len(items) < total,
                },
                "items": items,
            },
        }

    async def get_flight(self, external_id: str) -> dict[str, Any]:
        doc = await self.repository.get_by_external_id(external_id.strip())
        if doc is None:
            return {
                "status": "not_found",
                "message": f"UAV flight '{external_id}' was not found",
            }
        return {"status": "ok", "data": self._build_detail(doc)}

    async def get_samples(
        self,
        external_id: str,
        *,
        start: int = 0,
        limit: int = SAMPLE_LIMIT_DEFAULT,
        every_nth: int = 1,
        fields: set[str] | None = None,
        channels: set[str] | None = None,
    ) -> dict[str, Any]:
        safe_start = max(start, 0)
        safe_limit = min(max(limit, 1), SAMPLE_LIMIT_MAX)
        safe_nth = min(max(every_nth, 1), EVERY_NTH_MAX)
        selected_groups = self._validate_field_groups(fields)
        kept_keys = self._select_channel_keys(selected_groups, channels)

        doc = await self.repository.get_sample_window(
            external_id.strip(),
            start=safe_start,
            limit=safe_limit,
        )
        if doc is None:
            return {
                "status": "not_found",
                "message": f"UAV flight '{external_id}' was not found",
            }

        window: list[dict[str, Any]] = doc.get("samples") or []
        total = int(doc.get("identity", {}).get("sample_count") or 0)

        samples: list[dict[str, Any]] = []
        for index, sample in enumerate(window):
            if index % safe_nth != 0:
                continue
            samples.append(self._project_sample(sample, kept_keys))

        identity = doc.get("identity") or {}
        start_time = self._sample_window_start(identity.get("takeoff_at"), samples)
        has_more = safe_start + safe_limit < total
        return {
            "status": "ok",
            "query": {
                "start": safe_start,
                "limit": safe_limit,
                "every_nth": safe_nth,
                "fields": sorted(selected_groups),
                "channels": sorted(channels) if channels else None,
            },
            "data": {
                "external_id": external_id.strip(),
                "start_time": self._iso_z(start_time),
                "start": safe_start,
                "limit": safe_limit,
                "every_nth": safe_nth,
                "returned": len(samples),
                "has_more": has_more,
                "next_start": (safe_start + safe_limit) if has_more else None,
                "samples": samples,
            },
        }

    def list_channels(self) -> dict[str, Any]:
        from uav.schemas.channels import CMP_CHANNELS

        return {
            "status": "ok",
            "data": {
                "items": [
                    {
                        "key": channel.key,
                        "label": channel.label,
                        "unit": channel.unit,
                        "group": channel.group,
                    }
                    for channel in CMP_CHANNELS
                ]
            },
        }

    # =========================================================
    # 统计分析
    # =========================================================

    @staticmethod
    def _validate_channels(channels: set[str], *, maximum: int = 6) -> list[str]:
        unknown = channels - set(CHANNEL_BY_KEY)
        if unknown:
            raise ValueError(f"不支持的 UAV 通道: {', '.join(sorted(unknown))}")
        if not channels:
            raise ValueError("channels 不能为空")
        if len(channels) > maximum:
            raise ValueError(f"一次最多统计 {maximum} 个通道")
        return sorted(channels)

    async def get_channel_stats(
        self,
        *,
        channels: set[str],
        model: str | None = None,
        flight_kind: str | None = None,
    ) -> dict[str, Any]:
        """跨架次聚合指定通道的统计（可选机型/样本类型过滤）。"""
        channel_keys = self._validate_channels(channels)
        rows = await self.repository.aggregate_channel_stats(
            channels=channel_keys,
            model=model,
            flight_kind=flight_kind,
        )

        items = []
        for row in rows:
            spec = CHANNEL_BY_KEY.get(row.get("channel"))
            items.append(
                {
                    "channel": row.get("channel"),
                    "label": spec.label if spec else row.get("channel"),
                    "unit": spec.unit if spec else None,
                    "group": spec.group if spec else None,
                    "flight_count": int(row.get("flight_count") or 0),
                    "total_samples": int(row.get("total_samples") or 0),
                    "weighted_mean": row.get("weighted_mean"),
                    "min": row.get("min"),
                    "max": row.get("max"),
                    "missing_total": int(row.get("missing_total") or 0),
                }
            )

        return {
            "status": "ok",
            "query": {
                "channels": channel_keys,
                "model": model,
                "flight_kind": flight_kind,
            },
            "data": {
                "flights_included": (
                    max((item["flight_count"] for item in items), default=0)
                ),
                "items": items,
                "note": "加权均值为 Σ(架次均值×样本数)/Σ样本数；不含标准差。",
            },
        }

    async def get_normal_vs_anomaly_stats(
        self,
        *,
        channels: set[str],
        model: str | None = None,
    ) -> dict[str, Any]:
        """按正常/异常样本类型对比指定通道的跨架次统计。"""
        channel_keys = self._validate_channels(channels)
        rows = await self.repository.aggregate_kind_channel_stats(
            channels=channel_keys,
            model=model,
        )

        grouped: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            channel = row.get("channel")
            kind = row.get("flight_kind") or "unknown"
            if channel not in grouped:
                grouped[channel] = {}
            grouped[channel][kind] = {
                "flight_count": int(row.get("flight_count") or 0),
                "total_samples": int(row.get("total_samples") or 0),
                "weighted_mean": row.get("weighted_mean"),
                "min": row.get("min"),
                "max": row.get("max"),
                "missing_total": int(row.get("missing_total") or 0),
            }

        items = []
        for channel in channel_keys:
            spec = CHANNEL_BY_KEY.get(channel)
            normal = grouped.get(channel, {}).get("normal")
            anomaly = grouped.get(channel, {}).get("anomaly_sample")
            normal_mean = (
                normal.get("weighted_mean") if normal else None
            )
            anomaly_mean = (
                anomaly.get("weighted_mean") if anomaly else None
            )
            delta_mean = None
            delta_relative = None
            if (
                isinstance(normal_mean, int | float)
                and isinstance(anomaly_mean, int | float)
                and normal_mean != 0
            ):
                delta_mean = round(anomaly_mean - normal_mean, 4)
                delta_relative = round(
                    (anomaly_mean - normal_mean) / abs(normal_mean),
                    4,
                )
            items.append(
                {
                    "channel": channel,
                    "label": spec.label if spec else channel,
                    "unit": spec.unit if spec else None,
                    "group": spec.group if spec else None,
                    "normal": normal,
                    "anomaly": anomaly,
                    "delta_mean": delta_mean,
                    "delta_relative": delta_relative,
                }
            )

        return {
            "status": "ok",
            "query": {
                "channels": channel_keys,
                "model": model,
            },
            "data": {
                "items": items,
                "note": (
                    "delta_mean = 异常均值 - 正常均值；"
                    "delta_relative = delta_mean / |正常均值|。"
                ),
            },
        }

    async def get_flight_phase_stats(
        self,
        external_id: str,
    ) -> dict[str, Any]:
        """返回单架次按爬升/平飞/下降阶段划分的统计。

        阶段划分基于组合天向速度 cmp_comb_vu：> +1.0 m/s 爬升，
        < -1.0 m/s 下降，其余平飞。
        """
        rows = await self.repository.get_flight_phase_stats(
            external_id=external_id.strip()
        )
        if not rows:
            return {
                "status": "not_found",
                "message": f"UAV flight '{external_id}' was not found",
            }

        total_points = sum(int(row.get("point_count") or 0) for row in rows)
        phases = []
        for row in rows:
            start_t = row.get("start_t_s")
            end_t = row.get("end_t_s")
            duration = None
            if (
                isinstance(start_t, int | float)
                and isinstance(end_t, int | float)
            ):
                duration = round(float(end_t) - float(start_t), 3)
            phases.append(
                {
                    "phase": row.get("_id"),
                    "point_count": int(row.get("point_count") or 0),
                    "point_ratio": (
                        round(int(row.get("point_count") or 0) / total_points, 4)
                        if total_points
                        else None
                    ),
                    "start_t_s": start_t,
                    "end_t_s": end_t,
                    "duration_seconds": duration,
                    "altitude": {
                        "minimum_m": row.get("min_altitude_m"),
                        "maximum_m": row.get("max_altitude_m"),
                        "average_m": row.get("average_altitude_m"),
                    },
                    "ground_speed": {
                        "average_mps": row.get("average_ground_speed_mps"),
                        "maximum_mps": row.get("maximum_ground_speed_mps"),
                    },
                    "vertical_speed": {
                        "average_mps": row.get("average_vertical_speed_mps"),
                    },
                }
            )

        return {
            "status": "ok",
            "query": {"external_id": external_id.strip()},
            "data": {
                "total_points": total_points,
                "phases": phases,
                "phase_threshold_mps": 1.0,
            },
        }

    # =========================================================
    # 内部辅助
    # =========================================================

    async def _gather(self, *coros: Any) -> tuple[Any, ...]:
        import asyncio

        return await asyncio.gather(*coros)

    @staticmethod
    def _iso_z(value: Any) -> str | None:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return None

    @staticmethod
    def _sample_window_start(
        takeoff_at: Any,
        samples: list[dict[str, Any]],
    ) -> datetime | None:
        if not isinstance(takeoff_at, datetime):
            return None
        offset = samples[0].get("t_s") if samples else 0
        if isinstance(offset, bool) or not isinstance(offset, int | float):
            offset = 0
        return takeoff_at + timedelta(seconds=float(offset))

    @staticmethod
    def _validate_field_groups(fields: set[str] | None) -> set[str]:
        if fields is None:
            return set()
        unknown = fields - set(CHANNEL_GROUPS)
        if unknown:
            raise ValueError(f"unknown channel groups: {sorted(unknown)}")
        return fields

    @staticmethod
    def _channel_keys_for_groups(groups: set[str]) -> set[str] | None:
        if not groups:
            return None
        keys: set[str] = set()
        for group in groups:
            keys.update(GROUP_KEYS[group])
        return keys

    @classmethod
    def _select_channel_keys(
        cls,
        groups: set[str],
        channels: set[str] | None,
    ) -> set[str] | None:
        if not channels:
            return cls._channel_keys_for_groups(groups)
        unknown = channels - set(CHANNEL_BY_KEY)
        if unknown:
            raise ValueError(f"unknown channels: {sorted(unknown)}")
        group_keys = cls._channel_keys_for_groups(groups)
        if group_keys is not None:
            outside = channels - group_keys
            if outside:
                raise ValueError(f"channels outside selected groups: {sorted(outside)}")
        return channels

    @staticmethod
    def _project_sample(
        sample: dict[str, Any],
        kept_keys: set[str] | None,
    ) -> dict[str, Any]:
        if kept_keys is None:
            return dict(sample)
        return {
            "seq": sample.get("seq"),
            "t_s": sample.get("t_s"),
            **{key: sample[key] for key in kept_keys if key in sample},
        }

    def _flatten_flight(self, row: dict[str, Any]) -> dict[str, Any]:
        source = row.get("source") or {}
        identity = row.get("identity") or {}
        return {
            "external_id": source.get("external_id"),
            "model": source.get("model"),
            "tail_number": source.get("tail_number"),
            "mission": identity.get("mission"),
            "flight_seq": identity.get("flight_seq"),
            "takeoff_at": self._iso_z(identity.get("takeoff_at")),
            "duration_s": identity.get("duration_s"),
            "sample_count": identity.get("sample_count", 0),
            "sample_rate_hz": identity.get("sample_rate_hz"),
            "flight_kind": identity.get("flight_kind"),
            "label": identity.get("label"),
        }

    def _build_detail(self, doc: dict[str, Any]) -> dict[str, Any]:
        identity = doc.get("identity") or {}
        ingest = doc.get("ingest") or {}
        summary = doc.get("summary") or {}
        bounds = summary.get("position_bounds") or {}

        def range_stat(value: Any) -> dict[str, Any]:
            value = value or {}
            return {
                "min": value.get("min"),
                "max": value.get("max"),
            }

        return {
            **self._flatten_flight(doc),
            "platform": identity.get("platform"),
            "original_filename": ingest.get("original_filename"),
            "uploaded_at": self._iso_z(ingest.get("uploaded_at")),
            "header_layout_note": ingest.get("header_layout_note"),
            "extra_column_count": int(ingest.get("extra_column_count") or 0),
            "summary": {
                "altitude_m": range_stat(summary.get("altitude_m")),
                "ground_speed_mps": range_stat(summary.get("ground_speed_mps")),
                "position_bounds": {
                    "min_lat": bounds.get("min_lat"),
                    "max_lat": bounds.get("max_lat"),
                    "min_lon": bounds.get("min_lon"),
                    "max_lon": bounds.get("max_lon"),
                },
            },
            "channel_stats": summary.get("channel_stats") or [],
        }
