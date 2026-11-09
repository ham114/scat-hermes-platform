"""Versioned structured results rendered by analysis clients.

The values in this protocol are produced by deterministic tools, not by the LLM.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

type AnalysisScalar = str | int | float | bool | None


class AnalysisModel(BaseModel):
    """Base model that rejects accidental protocol expansion."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MetricItem(AnalysisModel):
    key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    value: AnalysisScalar
    unit: str | None = Field(default=None, max_length=40)


class MetricsBlock(AnalysisModel):
    type: Literal["metrics"] = "metrics"
    title: str = Field(min_length=1, max_length=200)
    items: list[MetricItem] = Field(min_length=1, max_length=12)


class TableColumn(AnalysisModel):
    key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    unit: str | None = Field(default=None, max_length=40)


class TableBlock(AnalysisModel):
    type: Literal["table"] = "table"
    title: str = Field(min_length=1, max_length=200)
    columns: list[TableColumn] = Field(min_length=1, max_length=30)
    rows: list[dict[str, AnalysisScalar]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_columns(self) -> TableBlock:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("table column keys must be unique")
        return self


class TimeSeriesSeries(AnalysisModel):
    key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    unit: str | None = Field(default=None, max_length=40)


class TimeSeriesBlock(AnalysisModel):
    type: Literal["timeseries"] = "timeseries"
    title: str = Field(min_length=1, max_length=200)
    x_key: str = Field(min_length=1, max_length=100)
    x_label: str = Field(min_length=1, max_length=100)
    x_type: Literal["number", "datetime"]
    x_unit: str | None = Field(default=None, max_length=40)
    series: list[TimeSeriesSeries] = Field(min_length=1, max_length=8)
    points: list[dict[str, AnalysisScalar]] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_series_points(self) -> TimeSeriesBlock:
        series_keys = [item.key for item in self.series]
        if len(series_keys) != len(set(series_keys)):
            raise ValueError("timeseries series keys must be unique")
        if self.x_key in series_keys:
            raise ValueError("timeseries x_key cannot also be a series key")

        allowed_keys = {self.x_key, *series_keys}
        for point in self.points:
            if self.x_key not in point or point[self.x_key] is None:
                raise ValueError("every timeseries point must contain a non-null x value")
            x_value = point[self.x_key]
            if self.x_type == "number" and (
                isinstance(x_value, bool) or not isinstance(x_value, int | float)
            ):
                raise ValueError("numeric timeseries axes must contain numeric values")
            if self.x_type == "datetime" and not isinstance(x_value, str):
                raise ValueError("datetime timeseries axes must contain strings")
            if set(point) - allowed_keys:
                raise ValueError("timeseries point contains an undeclared key")
            for key in series_keys:
                value = point.get(key)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int | float)
                ):
                    raise ValueError("timeseries values must be numeric or null")
        return self


class Trajectory2dSeries(AnalysisModel):
    key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    unit: str | None = Field(default=None, max_length=40)
    role: Literal["color", "size"] = "color"


class Trajectory2dBlock(AnalysisModel):
    """二维轨迹块：点按 x/y 坐标连线，时间键可选（用于排序与提示）。"""

    type: Literal["trajectory_2d"] = "trajectory_2d"
    title: str = Field(min_length=1, max_length=200)
    x_key: str = Field(min_length=1, max_length=100)
    x_label: str = Field(min_length=1, max_length=100)
    x_unit: str | None = Field(default=None, max_length=40)
    y_key: str = Field(min_length=1, max_length=100)
    y_label: str = Field(min_length=1, max_length=100)
    y_unit: str | None = Field(default=None, max_length=40)
    time_key: str | None = Field(default=None, max_length=100)
    path_key: str | None = Field(default=None, max_length=100)
    path_label: str | None = Field(default=None, max_length=100)
    segment_key: str | None = Field(default=None, max_length=100)
    segment_label: str | None = Field(default=None, max_length=100)
    snapshot_key: str | None = Field(default=None, max_length=100)
    snapshot_label: str | None = Field(default=None, max_length=100)
    series: list[Trajectory2dSeries] = Field(default_factory=list, max_length=3)
    points: list[dict[str, AnalysisScalar]] = Field(default_factory=list, max_length=5_000)

    @model_validator(mode="after")
    def validate_points(self) -> Trajectory2dBlock:
        if self.x_key == self.y_key or self.time_key in {self.x_key, self.y_key}:
            raise ValueError("trajectory_2d axis keys must be distinct")
        series_keys = [item.key for item in self.series]
        if len(series_keys) != len(set(series_keys)):
            raise ValueError("trajectory_2d series keys must be unique")
        if self.x_key in series_keys or self.y_key in series_keys:
            raise ValueError("trajectory_2d series key collides with an axis key")
        if (self.path_key is None) != (self.path_label is None):
            raise ValueError("trajectory_2d path_key and path_label must be declared together")
        if (self.segment_key is None) != (self.segment_label is None):
            raise ValueError(
                "trajectory_2d segment_key and segment_label must be declared together"
            )
        if (self.snapshot_key is None) != (self.snapshot_label is None):
            raise ValueError(
                "trajectory_2d snapshot_key and snapshot_label must be declared together"
            )
        grouping_keys = {key for key in (self.path_key, self.segment_key, self.snapshot_key) if key}
        if grouping_keys & {self.x_key, self.y_key, self.time_key, *series_keys}:
            raise ValueError("trajectory_2d grouping key collides with another key")
        allowed = {self.x_key, self.y_key, *series_keys, *grouping_keys}
        if self.time_key:
            allowed.add(self.time_key)
        for point in self.points:
            if set(point) - allowed:
                raise ValueError("trajectory_2d point contains an undeclared key")
            for key in (self.x_key, self.y_key):
                value = point.get(key)
                if value is None or isinstance(value, bool) or not isinstance(value, int | float):
                    raise ValueError("trajectory_2d points require numeric x/y values")
            if self.time_key:
                value = point.get(self.time_key)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int | float)
                ):
                    raise ValueError("trajectory_2d time values must be numeric or null")
            for key in series_keys:
                value = point.get(key)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int | float)
                ):
                    raise ValueError("trajectory_2d series values must be numeric or null")
            for key in grouping_keys:
                value = point.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError("trajectory_2d grouping values must be non-empty strings")
        return self


class ClassificationItem(AnalysisModel):
    label: str = Field(min_length=1, max_length=100)
    count: int = Field(default=1, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    sample_ref: str | None = Field(default=None, max_length=200)


class ClassificationBlock(AnalysisModel):
    """分类结果块：类别集合与样本归属统计。"""

    type: Literal["classification"] = "classification"
    title: str = Field(min_length=1, max_length=200)
    class_labels: list[str] = Field(min_length=1, max_length=20)
    items: list[ClassificationItem] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_items(self) -> ClassificationBlock:
        labels = set(self.class_labels)
        if len(labels) != len(self.class_labels):
            raise ValueError("classification class_labels must be unique")
        for item in self.items:
            if item.label not in labels:
                raise ValueError("classification item label must be one of class_labels")
        return self


class PredictionPoint(AnalysisModel):
    time_step_s: float
    value: float
    lower_bound: float | None = None
    upper_bound: float | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> PredictionPoint:
        if self.lower_bound is not None and self.value < self.lower_bound:
            raise ValueError("prediction value below lower_bound")
        if self.upper_bound is not None and self.value > self.upper_bound:
            raise ValueError("prediction value above upper_bound")
        if (
            self.lower_bound is not None
            and self.upper_bound is not None
            and self.lower_bound > self.upper_bound
        ):
            raise ValueError("prediction lower_bound above upper_bound")
        return self


class PredictionBlock(AnalysisModel):
    """预测结果块：来自注册模型/确定性来源的带边界预测序列。"""

    type: Literal["prediction"] = "prediction"
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=100)
    target_label: str = Field(min_length=1, max_length=100)
    target_unit: str | None = Field(default=None, max_length=40)
    points: list[PredictionPoint] = Field(default_factory=list, max_length=200)


class ActionLinkBlock(AnalysisModel):
    """动作跳转块：整页跳转到其他页面（如三维可视化），不携带敏感信息。"""

    type: Literal["action_link"] = "action_link"
    label: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    kind: Literal["visualization_3d", "visualization_2d", "external", "back_to_chat"] = "external"
    artifact_id: str | None = Field(default=None, max_length=200)
    thread_id: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError("action_link contains unsafe characters")
        parsed = urlsplit(value)
        if value.startswith("//") or parsed.username or parsed.password:
            raise ValueError("action_link cannot contain authority overrides or credentials")
        if not (
            (parsed.scheme in {"http", "https"} and bool(parsed.hostname))
            or (value.startswith("/") and not parsed.netloc)
        ):
            raise ValueError("action_link url must be http(s) or a relative path")
        return value


AnalysisBlock = Annotated[
    MetricsBlock
    | TableBlock
    | TimeSeriesBlock
    | Trajectory2dBlock
    | ClassificationBlock
    | PredictionBlock
    | ActionLinkBlock,
    Field(discriminator="type"),
]


class AnalysisResult(AnalysisModel):
    """A complete, independently renderable analysis result."""

    schema_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6"] = "1.0"
    dataset: Literal["scat", "uav", "cross_dataset"]
    operation: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    start_time: str | None = Field(default=None, max_length=64)
    context: dict[str, AnalysisScalar] = Field(default_factory=dict)
    blocks: list[AnalysisBlock] = Field(min_length=1, max_length=20)

    @field_validator("start_time")
    @classmethod
    def normalize_start_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("start_time must be an ISO 8601 datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("start_time must include a timezone")
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @model_validator(mode="after")
    def require_protocol_version(self) -> AnalysisResult:
        def finite(value):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("analysis values must be finite")
            if isinstance(value, dict):
                for child in value.values():
                    finite(child)
            elif isinstance(value, list):
                for child in value:
                    finite(child)

        finite(self.model_dump(mode="python"))
        timeseries = [block for block in self.blocks if isinstance(block, TimeSeriesBlock)]
        if timeseries and self.schema_version == "1.0":
            raise ValueError("timeseries blocks require schema_version 1.1 or newer")
        if timeseries and self.schema_version in {"1.2", "1.3", "1.4", "1.5", "1.6"}:
            if self.start_time is None:
                raise ValueError(
                    f"schema_version {self.schema_version} timeseries require start_time"
                )
            if any(block.x_type != "number" or block.x_unit != "s" for block in timeseries):
                raise ValueError(
                    f"schema_version {self.schema_version} timeseries axes must be numeric seconds"
                )
        new_block_types = (
            Trajectory2dBlock,
            ClassificationBlock,
            PredictionBlock,
            ActionLinkBlock,
        )
        if any(
            isinstance(block, new_block_types) for block in self.blocks
        ) and self.schema_version not in {"1.3", "1.4", "1.5", "1.6"}:
            raise ValueError(
                "trajectory_2d/classification/prediction/action_link blocks "
                "require schema_version 1.3 or newer"
            )
        if any(
            isinstance(block, Trajectory2dBlock) and block.snapshot_key is not None
            for block in self.blocks
        ) and self.schema_version not in {"1.5", "1.6"}:
            raise ValueError("trajectory_2d snapshot grouping requires schema_version 1.5")
        if (
            any(
                isinstance(block, Trajectory2dBlock) and len(block.series) > 2
                for block in self.blocks
            )
            and self.schema_version != "1.6"
        ):
            raise ValueError("trajectory_2d third series requires schema_version 1.6")
        return self


_ANALYSIS_RESULT_ADAPTER = TypeAdapter(AnalysisResult)


def extract_analysis_result(payload: Any) -> AnalysisResult | None:
    """Read an AnalysisResult from a tool payload or serialized tool content."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None

    if not isinstance(payload, dict):
        return None

    candidate = payload.get("analysis_result")
    if candidate is None:
        return None

    try:
        return _ANALYSIS_RESULT_ADAPTER.validate_python(candidate)
    except ValidationError:
        return None


__all__ = [
    "ActionLinkBlock",
    "AnalysisBlock",
    "AnalysisResult",
    "ClassificationBlock",
    "ClassificationItem",
    "MetricItem",
    "MetricsBlock",
    "PredictionBlock",
    "PredictionPoint",
    "TableBlock",
    "TableColumn",
    "TimeSeriesBlock",
    "TimeSeriesSeries",
    "Trajectory2dBlock",
    "Trajectory2dSeries",
    "extract_analysis_result",
]
