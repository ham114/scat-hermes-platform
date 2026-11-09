"""M6 分组表现：按“观测截止点所在飞行阶段”解析阶段标签（纯计算）。

阶段来源：物化段级标注 `scat_flight_phase_segments`（M4：RDP 分段 + 六类阶段，
端点 ISO 字符串）。缺失或时间无法解析时返回 None——运行器把无法归组的样本计入
`coverage.phase_grouping.ungrouped_samples`，不阻塞整体评估。

确定性约定：段为闭区间 [start, end]；端点重叠时归属**先生成**的段（按物化顺序），
同一时刻永远解析为同一阶段。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

__all__ = ["phase_for_time"]


def phase_for_time(segments: list[dict[str, Any]], moment: datetime) -> str | None:
    """返回包含 `moment` 的段的阶段标签；无匹配段返回 None。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    for segment in segments:
        start = _parse_moment(segment.get("start_time"))
        end = _parse_moment(segment.get("end_time"))
        if start is None or end is None:
            continue
        if start <= moment <= end:
            phase = segment.get("phase")
            return str(phase) if phase else None
    return None


def _parse_moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
