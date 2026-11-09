"""SCAT Agent 工具输入模型。

这些模型定义 LangChain Tool 接收的结构化参数。

设计原则：
1. 禁止未声明的额外参数；
2. 对结果数量设置上限；
3. 对批次、航班编号和时间等关键参数进行校验；
4. 字段描述应让 LLM 能准确理解参数含义；
5. 不允许模型直接提交 MongoDB 查询语句。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

DEFAULT_BATCH = "scat20161015_20161021"


class ScatToolInput(BaseModel):
    """所有 SCAT 工具输入模型的基类。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EmptyInput(ScatToolInput):
    """不需要参数的工具输入。"""


class MaterializationJobPrepareInput(ScatToolInput):
    """准备一个批次物化任务（只统计影响面，不写库）。"""

    kind: Literal["flight_features", "phase_segments"] = Field(
        default="phase_segments",
        description=(
            "物化种类：phase_segments=段级阶段标注（RDP 分段 + 六类，回答“分段/转弯/阶段”类问题）；"
            "flight_features=航班级阶段特征（爬升/巡航/下降秒数）。"
        ),
    )
    batch: str = Field(default=DEFAULT_BATCH, min_length=1, max_length=200)
    force: bool = Field(
        default=False,
        description="True=强制重算整批；False（默认）=只补算尚无当前版本结果的航班（增量）。",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=100000,
        description="本次最多处理航班数；缺省为整批。",
    )


class MaterializationJobSubmitInput(ScatToolInput):
    """提交（入队）一个已准备的物化任务：仅写一条 pending 作业，不执行计算。"""

    confirmation_id: str = Field(
        min_length=8,
        max_length=64,
        description="prepare_materialization_job 返回的一次性确认 ID（15 分钟有效）。",
    )


class MaterializationJobStatusInput(ScatToolInput):
    """查询物化任务状态。"""

    job_id: str = Field(
        min_length=8,
        max_length=200,
        description="submit 返回或作业列表中的 job_id。",
    )


class ListMaterializationJobsInput(ScatToolInput):
    """List recent materialization jobs with bounded filters."""

    kind: Literal["flight_features", "phase_segments", "prediction_metrics"] | None = None
    batch: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["pending", "running", "succeeded", "partial", "failed"] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class WorstPredictionCasesInput(ScatToolInput):
    """查询最差预测案例（M5-07 最差案例检索）。"""

    batch: str = Field(default=DEFAULT_BATCH, min_length=1, max_length=200)
    metric: Literal["horizontal", "vertical", "along", "cross"] = Field(
        default="horizontal",
        description=(
            "排序维度：horizontal=水平误差（默认）、vertical=垂直误差、"
            "along=沿航向误差、cross=横向误差。"
        ),
    )
    horizon_s: float | None = Field(
        default=None,
        gt=0,
        le=3600,
        description="只查指定预测时域（秒）；缺省查全部时域。",
    )
    limit: int = Field(default=10, ge=1, le=100, description="返回条数（最多 100）。")


class PredictionMetricSummariesInput(ScatToolInput):
    """查询批次级预测指标汇总（M5-06/M5-08，与模型评估页同源）。"""

    batch: str = Field(default=DEFAULT_BATCH, min_length=1, max_length=200)
    group_by: Literal["overall", "phase", "aircraft_type"] = Field(
        default="overall",
        description="分组维度：overall=整批次（默认）、phase=按航段主导阶段、aircraft_type=按机型。",
    )
    horizon_s: float | None = Field(
        default=None,
        gt=0,
        le=3600,
        description=(
            "只看指定预测时域（秒，冻结口径 60/180/300/600/900/1200）；缺省返回全部六档。"
        ),
    )
    limit: int = Field(default=500, ge=1, le=500, description="返回条数（最多 500）。")


class ListBatchesInput(ScatToolInput):
    """查询可用 SCAT 数据批次。"""

    limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description=("最多返回多少个数据批次。通常不需要修改，默认返回100个。"),
    )


class ScatCollectionDataQualityInput(ScatToolInput):
    """全库或指定批次的GBAR六特征质量聚合。"""

    batch: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "可选批次名称；不提供时扫描全部SCAT航班。该工具在MongoDB端聚合，不逐航班调用Agent工具。"
        ),
        examples=[DEFAULT_BATCH],
    )

    @field_validator("batch", mode="before")
    @classmethod
    def normalize_batch(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ScatQualityProblemFlightsInput(ScatToolInput):
    """分页列出包含缺失、越界或跳变点的问题航班。"""

    batch: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=("可选批次名称；不提供时扫描全部SCAT航班。问题航班列表默认按越界点数降序。"),
        examples=[DEFAULT_BATCH],
    )

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="每页返回多少条问题航班，默认 20。",
    )

    offset: int = Field(
        default=0,
        ge=0,
        description="分页偏移；下一页用上一页 offset+limit。",
    )

    problem_type: Literal["any", "missing", "outlier", "spike"] = Field(
        default="any",
        description=(
            "问题类型过滤：any=任意问题，missing=缺失/非有限值，"
            "outlier=物理范围越界，spike=相邻点孤立跳变。"
        ),
    )

    sort_by: Literal[
        "outlier_point_count",
        "missing_point_count",
        "spike_value_count",
        "point_count",
        "external_id",
    ] = "outlier_point_count"

    @field_validator("batch", mode="before")
    @classmethod
    def normalize_batch(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ScatQualityCleanFlightsInput(ScatToolInput):
    """分页列出当前质量策略下没有问题的SCAT轨迹。"""

    batch: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="可选批次名称；不提供时查询全部SCAT航班。",
        examples=[DEFAULT_BATCH],
    )

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="每页返回多少条无缺失、越界或跳变的轨迹，默认20。",
    )

    offset: int = Field(
        default=0,
        ge=0,
        description="分页偏移；下一页用上一页offset+limit。",
    )

    @field_validator("batch", mode="before")
    @classmethod
    def normalize_batch(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class BatchOverviewInput(ScatToolInput):
    """查询一个 SCAT 数据批次的总体情况。"""

    batch: str = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        description=("SCAT 数据批次名称，例如 'scat20161015_20161021'。"),
        examples=[DEFAULT_BATCH],
    )

    distribution_limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description=("机型分布和航线分布中最多返回多少项。"),
    )


class ScatFlightSelectionCriteria(ScatToolInput):
    """普通查询和模型批量选择共用的 SCAT 航班条件。"""

    batch: str | None = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        description=(f"可选的数据批次名称。当前默认批次为 '{DEFAULT_BATCH}'。"),
        examples=[DEFAULT_BATCH],
    )

    callsign: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description=("航班呼号，例如 'IBK8WB'、'SAS406'。不要填写航空公司名称。"),
        examples=["IBK8WB"],
    )
    callsigns: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="允许的多个航班呼号，采用 OR；与其他字段仍采用 AND。",
    )
    exclude_callsigns: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="需要排除的航班呼号。",
    )
    callsign_prefix: str | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="安全的呼号前缀匹配，例如 SAS。",
    )

    aircraft_type: str | None = Field(
        default=None,
        min_length=2,
        max_length=10,
        description=("ICAO 机型代码，例如 'B738'、'A320'、'AT76'。"),
        examples=["B738"],
    )
    aircraft_types: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="允许的多个 ICAO 机型代码。",
    )
    exclude_aircraft_types: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="需要排除的 ICAO 机型；例如非 A320 使用 ['A320']。",
    )

    adep: str | None = Field(
        default=None,
        min_length=3,
        max_length=4,
        description=("起飞机场 ICAO 代码，例如 'EGKK'、'EKCH'、'ESSA'。"),
        examples=["EGKK"],
    )
    adeps: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="允许的多个起飞机场 ICAO 代码。",
    )
    exclude_adeps: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="需要排除的起飞机场 ICAO 代码。",
    )

    ades: str | None = Field(
        default=None,
        min_length=3,
        max_length=4,
        description=("目的机场 ICAO 代码，例如 'ESSA'、'EKCH'、'ENGM'。"),
        examples=["ESSA"],
    )
    adess: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="允许的多个目的机场 ICAO 代码。",
    )
    exclude_adess: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="需要排除的目的机场 ICAO 代码。",
    )

    external_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="精确匹配一个 source.external_id。",
    )
    external_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="允许的多个 source.external_id。",
    )
    exclude_external_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description="需要排除的 source.external_id。",
    )

    candidate_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        description=(
            "系统生成的候选航班键，例如 "
            "'v1|IBK8WB|EGKK|2016-10-20T18:45:00Z'。"
            "该字段不是绝对唯一标识。"
        ),
    )

    takeoff_start: datetime | None = Field(
        default=None,
        description="计划起飞时间下界（含），必须带时区。",
    )

    takeoff_end: datetime | None = Field(
        default=None,
        description="计划起飞时间上界（不含），必须带时区。",
    )

    min_track_points: int | None = Field(
        default=None,
        ge=0,
        le=100_000,
        description="轨迹原始点数下界（含）。",
    )
    max_track_points: int | None = Field(
        default=None,
        ge=0,
        le=100_000,
        description="轨迹原始点数上界（含）。",
    )
    has_fpl: bool | None = Field(
        default=None,
        description="true=只要含 FPL 的航班；false=只要不含 FPL 的航班。",
    )
    has_scat_prediction: bool | None = Field(
        default=None,
        description="true=含原始 SCAT 空管预测快照；false=不含预测快照。",
    )

    @field_validator(
        "batch",
        "candidate_key",
        "external_id",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("takeoff_start", "takeoff_end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("计划起飞时间必须包含时区")
        return value

    @model_validator(mode="after")
    def validate_takeoff_window(self) -> ScatFlightSelectionCriteria:
        if (
            self.takeoff_start is not None
            and self.takeoff_end is not None
            and self.takeoff_start >= self.takeoff_end
        ):
            raise ValueError("takeoff_start 必须早于 takeoff_end")
        return self

    @field_validator(
        "callsign",
        "callsign_prefix",
        "aircraft_type",
        "adep",
        "ades",
        mode="before",
    )
    @classmethod
    def normalize_codes(
        cls,
        value: object,
    ) -> object:
        """将呼号、机型和机场代码统一转换为大写。"""
        if isinstance(value, str):
            stripped = value.strip()

            if not stripped:
                return None

            return stripped.upper()

        return value

    @field_validator(
        "callsigns",
        "exclude_callsigns",
        "aircraft_types",
        "exclude_aircraft_types",
        "adeps",
        "exclude_adeps",
        "adess",
        "exclude_adess",
        mode="before",
    )
    @classmethod
    def normalize_code_lists(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, list | tuple | set | frozenset):
            return value
        return tuple(
            dict.fromkeys(
                str(item).strip().upper()
                for item in value
                if item is not None and str(item).strip()
            )
        )

    @field_validator(
        "external_ids",
        "exclude_external_ids",
        mode="before",
    )
    @classmethod
    def normalize_external_id_lists(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, list | tuple | set | frozenset):
            return value
        return tuple(
            dict.fromkeys(
                str(item).strip() for item in value if item is not None and str(item).strip()
            )
        )

    @model_validator(mode="after")
    def validate_inclusion_and_exclusion(self) -> ScatFlightSelectionCriteria:
        exact_and_lists = (
            ("callsign", self.callsign, self.callsigns),
            ("aircraft_type", self.aircraft_type, self.aircraft_types),
            ("adep", self.adep, self.adeps),
            ("ades", self.ades, self.adess),
            ("external_id", self.external_id, self.external_ids),
        )
        for name, exact, allowed in exact_and_lists:
            if exact is not None and allowed:
                raise ValueError(f"{name} 与对应的多值包含条件不能同时使用")
        include_exclude = (
            ("callsign", self.callsigns, self.exclude_callsigns),
            ("aircraft_type", self.aircraft_types, self.exclude_aircraft_types),
            ("adep", self.adeps, self.exclude_adeps),
            ("ades", self.adess, self.exclude_adess),
            ("external_id", self.external_ids, self.exclude_external_ids),
        )
        for name, allowed, denied in include_exclude:
            if set(allowed) & set(denied):
                raise ValueError(f"{name} 的包含和排除条件存在冲突")
        exact_exclude = (
            ("callsign", self.callsign, self.exclude_callsigns),
            ("aircraft_type", self.aircraft_type, self.exclude_aircraft_types),
            ("adep", self.adep, self.exclude_adeps),
            ("ades", self.ades, self.exclude_adess),
            ("external_id", self.external_id, self.exclude_external_ids),
        )
        for name, exact, denied in exact_exclude:
            if exact is not None and exact in denied:
                raise ValueError(f"{name} 的精确匹配和排除条件存在冲突")
        if (
            self.min_track_points is not None
            and self.max_track_points is not None
            and self.min_track_points > self.max_track_points
        ):
            raise ValueError("min_track_points 不能大于 max_track_points")
        return self


class SearchFlightsInput(ScatFlightSelectionCriteria):
    """按照共享结构化条件搜索 SCAT 航班。"""

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="最多返回多少条航班记录。",
    )

    skip: int = Field(
        default=0,
        ge=0,
        description=("分页偏移量。第一页使用0，之后可按上一页返回数量增加。"),
    )

    sort_by: Literal["iobt", "callsign", "external_id"] = Field(
        default="iobt",
        description="排序字段：计划起飞时间、呼号或来源编号。",
    )

    sort_order: Literal["asc", "desc"] = Field(
        default="asc",
        description="排序方向。",
    )

    @model_validator(mode="after")
    def require_search_condition(
        self,
    ) -> SearchFlightsInput:
        """避免 Agent 无条件拉取大量航班。

        仅指定 batch 也视为有效查询，但返回数量仍受 limit 限制。
        """
        has_condition = any(
            value is not None
            for value in (
                self.batch,
                self.callsign,
                self.aircraft_type,
                self.adep,
                self.ades,
                self.candidate_key,
                self.takeoff_start,
                self.takeoff_end,
                self.external_id,
                self.callsigns,
                self.exclude_callsigns,
                self.callsign_prefix,
                self.aircraft_types,
                self.exclude_aircraft_types,
                self.adeps,
                self.exclude_adeps,
                self.adess,
                self.exclude_adess,
                self.external_ids,
                self.exclude_external_ids,
                self.min_track_points,
                self.max_track_points,
                self.has_fpl,
                self.has_scat_prediction,
            )
        )

        if not has_condition:
            raise ValueError("至少需要提供一个航班查询条件")

        return self


class FlightReferenceInput(ScatToolInput):
    """唯一定位一条来源航班。"""

    batch: str = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        description=(f"SCAT 数据批次名称，例如 '{DEFAULT_BATCH}'。"),
        examples=[DEFAULT_BATCH],
    )

    external_id: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "SCAT 来源数据编号，例如 '100016'。这是 source.external_id，不是 MongoDB _id。"
        ),
        examples=["100016"],
    )

    @field_validator(
        "batch",
        "external_id",
        mode="before",
    )
    @classmethod
    def normalize_required_text(
        cls,
        value: object,
    ) -> object:
        if isinstance(value, int):
            return str(value)

        if isinstance(value, str):
            return value.strip()

        return value


class TrackPointsInput(FlightReferenceInput):
    """分页查询一条航班的标准化轨迹点。"""

    start: int = Field(
        default=0,
        ge=0,
        description=("从第几个轨迹点开始返回，索引从0开始。"),
    )

    limit: int = Field(
        default=100,
        ge=1,
        le=500,
        description=(
            "本次最多返回多少个轨迹点。"
            "为防止 Agent 上下文过大，最多500个；"
            "普通问答建议不超过100个。"
        ),
    )

    start_time: datetime | None = Field(
        default=None,
        description="轨迹时间下界（含），必须带时区的 ISO 8601 时间。",
    )

    end_time: datetime | None = Field(
        default=None,
        description="轨迹时间上界（不含），必须带时区的 ISO 8601 时间。",
    )

    every_nth: int = Field(
        default=1,
        ge=1,
        le=20,
        description="在查询窗口内每 N 个源轨迹点保留一个。",
    )

    fields: set[Literal["position", "motion", "mode_s", "quality"]] | None = Field(
        default=None,
        description="需要保留的轨迹字段组；缺省返回全部字段组。",
    )

    @field_validator("start_time", "end_time")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("轨迹时间必须包含时区偏移")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> TrackPointsInput:
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time >= self.end_time
        ):
            raise ValueError("start_time 必须早于 end_time")
        return self


class ScatModelDataQualityInput(FlightReferenceInput):
    """检查指定航班在某个截止时刻前的模型输入质量。"""

    end_time: datetime | None = Field(
        default=None,
        description=(
            "可选的轨迹截止时间（含）；缺省使用该航班最新轨迹点。"
            "用于检查与实际模型调用相同的输入窗口。"
        ),
    )

    source_limit: int = Field(
        default=500,
        ge=71,
        le=2000,
        description=(
            "从截止时间向前读取的最大源轨迹点数。"
            "GBAR v35 至少需要71点，默认500点用于窗口前异常识别和插值。"
        ),
    )

    @field_validator("end_time")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("end_time 必须包含时区")
        return value


class PredictionSnapshotInput(FlightReferenceInput):
    """查询指定时刻之前最近的 SCAT 预测快照。"""

    timestamp: str = Field(
        min_length=10,
        max_length=40,
        description=(
            "目标时间，使用 ISO 8601 格式，例如 "
            "'2016-10-20T21:00:00'。"
            "工具返回该时间之前最近的一份 "
            "SCAT 空管预测轨迹快照。"
        ),
        examples=["2016-10-20T21:00:00"],
    )

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(
        cls,
        value: str,
    ) -> str:
        normalized = value.strip()

        candidate = normalized

        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"

        try:
            datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("timestamp 必须是有效的 ISO 8601 时间") from exc

        return normalized


class AirportStatsInput(ScatToolInput):
    """按批次查询机场航班量与起飞延误统计。"""

    batch: str = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        description=("SCAT 数据批次名称，例如 'scat20161015_20161021'。"),
        examples=[DEFAULT_BATCH],
    )

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description=("最多返回多少个起飞机场（按航班量降序）。"),
    )


class PredictionErrorStatsInput(FlightReferenceInput):
    """计算 SCAT 预测快照与实际轨迹之间的误差。"""

    timestamp: str | None = Field(
        default=None,
        description=(
            "可选：只使用该时刻（含）之前最近的预测快照；缺省使用最新快照。ISO 8601 时间字符串。"
        ),
        examples=["2016-10-20T21:00:00"],
    )

    match_window_seconds: float = Field(
        default=300.0,
        ge=10.0,
        le=3600.0,
        description=("航迹点匹配窗口：实际轨迹点与预测 ETO 的最大时间差（秒），超过则不匹配。"),
    )

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        candidate = normalized
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("timestamp 必须是有效的 ISO 8601 时间") from exc
        return normalized


class PredictFlightTrajectoryInput(FlightReferenceInput):
    """用自研预测器（M6）基于实飞观测做单航班即时轨迹预测。"""

    predictor_id: str = Field(
        default="kalman-cv",
        min_length=1,
        max_length=100,
        description=(
            "预测器 id：kalman-cv=Kalman 常速度（默认）；"
            "constant-velocity-heading=匀速-恒航向。未知 id 会返回可用列表。"
        ),
        examples=["kalman-cv", "constant-velocity-heading"],
    )

    as_of: str | None = Field(
        default=None,
        description=(
            "观测截止时刻（ISO 8601，UTC）。给定中途时刻=只看到该时刻为止的实飞数据，"
            "可同时给出与实飞真值的水平误差对照；缺省=使用整条轨迹（从轨迹末点向后"
            "预测，无实飞对照）。"
        ),
        examples=["2016-10-20T21:00:00"],
    )

    horizons_s: list[float] | None = Field(
        default=None,
        max_length=6,
        description=(
            "目标时域列表（秒，最多 6 档，每档不超过 3600）；缺省六档 60/180/300/600/900/1200。"
        ),
        examples=[[300.0, 600.0, 1200.0]],
    )

    compare_actual: bool = Field(
        default=True,
        description="True（默认）=目标时刻在实飞轨迹覆盖范围内时给出实际位置与水平误差（米）。",
    )

    @field_validator("as_of")
    @classmethod
    def validate_as_of(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        candidate = normalized
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("as_of 必须是有效的 ISO 8601 时间") from exc
        return normalized

    @field_validator("horizons_s")
    @classmethod
    def validate_horizons(
        cls,
        value: list[float] | None,
    ) -> list[float] | None:
        return _validate_horizon_values(value)


class PreparePredictionExperimentInput(ScatToolInput):
    """准备一次自研预测器批量评估实验（只统计影响面，不写库、不入队）。"""

    batch: str = Field(
        default=DEFAULT_BATCH,
        min_length=1,
        max_length=200,
        description=(f"SCAT 数据批次名称，例如 '{DEFAULT_BATCH}'。"),
        examples=[DEFAULT_BATCH],
    )
    predictor_id: str = Field(
        default="kalman-cv",
        min_length=1,
        max_length=100,
        description="预测器 id：kalman-cv（默认）或 constant-velocity-heading。",
        examples=["kalman-cv"],
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=20000,
        description="最多处理的航班数（缺省=整批；用于快速验证或抽样对比）。",
    )
    stride_s: float = Field(
        default=60.0,
        gt=0,
        le=3600,
        description="观测截止点采样步长（秒）；越小样本越多、耗时越长。",
    )
    max_cutoffs_per_flight: int = Field(
        default=12,
        ge=1,
        le=200,
        description="单航班最多采样多少个观测截止点（超出时均匀重采样）。",
    )
    horizons_s: list[float] | None = Field(
        default=None,
        max_length=6,
        description=("目标时域列表（秒，最多 6 档）；缺省六档 60/180/300/600/900/1200。"),
        examples=[[300.0, 600.0, 1200.0]],
    )

    @field_validator("horizons_s")
    @classmethod
    def validate_horizons(
        cls,
        value: list[float] | None,
    ) -> list[float] | None:
        return _validate_horizon_values(value)


class SubmitPredictionExperimentInput(ScatToolInput):
    """提交已准备的实验（只入队 pending 记录，不执行计算）。"""

    confirmation_id: str = Field(
        min_length=1,
        max_length=100,
        description="prepare_prediction_experiment 返回的确认 ID（15 分钟有效、单次消费）。",
    )


class GetExperimentRunInput(ScatToolInput):
    """按 experiment_id 查询实验记录。"""

    experiment_id: str = Field(
        min_length=1,
        max_length=140,
        description="实验 id，例如 'm6eval-kalman-cv-scat20161015_20161021-...'。",
    )


class ListPredictionExperimentsInput(ScatToolInput):
    """列出最近的自研预测器实验（摘要）。"""

    batch: str | None = Field(default=None, min_length=1, max_length=200)
    predictor_id: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=10, ge=1, le=100)


def _validate_horizon_values(
    value: list[float] | None,
) -> list[float] | None:
    if value is None:
        return None
    if not value:
        raise ValueError("horizons_s 不能为空列表（缺省即六档）")
    if len(value) > 6:
        raise ValueError("horizons_s 最多 6 档")
    if any(not 0 < item <= 3600 for item in value):
        raise ValueError("horizons_s 每档必须在 (0, 3600] 秒内")
    return value
