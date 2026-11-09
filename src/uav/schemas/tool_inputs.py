"""UAV Agent 工具的受控输入模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uav.schemas.channels import CHANNEL_BY_KEY, CHANNEL_GROUPS, GROUP_KEYS


class UavToolInput(BaseModel):
    """拒绝未声明参数，避免 Agent 构造任意数据库查询。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class UavEmptyInput(UavToolInput):
    """不需要参数的 UAV 工具输入。"""


class UavSearchFlightsInput(UavToolInput):
    """按照业务字段搜索 UAV 架次。"""

    model: str | None = Field(default=None, min_length=1, max_length=20)
    tail_number: str | None = Field(default=None, min_length=1, max_length=20)
    mission: str | None = Field(default=None, min_length=1, max_length=20)
    flight_kind: Literal["normal", "anomaly_sample"] | None = None
    label: str | None = Field(default=None, min_length=1, max_length=100)
    takeoff_start: datetime | None = Field(
        default=None,
        description="起飞时间下界（含），必须带时区的 ISO 8601 时间。",
    )
    takeoff_end: datetime | None = Field(
        default=None,
        description="起飞时间上界（不含），必须带时区的 ISO 8601 时间。",
    )
    sort_by: Literal["takeoff_at", "external_id", "duration_s", "sample_count"] = (
        "takeoff_at"
    )
    sort_order: Literal[-1, 1] = 1
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @field_validator("mission", mode="before")
    @classmethod
    def normalize_mission(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper() or None
        return value

    @field_validator("takeoff_start", "takeoff_end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("起飞时间必须包含时区偏移")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> UavSearchFlightsInput:
        if (
            self.takeoff_start is not None
            and self.takeoff_end is not None
            and self.takeoff_start >= self.takeoff_end
        ):
            raise ValueError("takeoff_start 必须早于 takeoff_end")
        return self


class UavFlightReferenceInput(UavToolInput):
    """通过来源文件编号定位单个 UAV 架次。"""

    external_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$",
        description="UAV 来源文件标识，例如 20230330122740_DTC_53.1394B。",
    )


class UavSamplesInput(UavFlightReferenceInput):
    """分页读取 UAV 传感器样本。"""

    start: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)
    every_nth: int = Field(default=1, ge=1, le=20)
    fields: set[Literal["position", "velocity", "attitude", "rates", "accel", "air"]] | None = (
        Field(
            default=None,
            description=(
                "需要保留的通道组；可选值为 " + ", ".join(sorted(CHANNEL_GROUPS)) + "。"
            ),
        )
    )
    channels: set[str] | None = Field(
        default=None,
        description=(
            "需要返回的精确通道键，例如 cmp_comb_alt、cmp_ground_speed；"
            "缺省返回所选字段组的全部通道。"
        ),
    )

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: set[str] | None) -> set[str] | None:
        if value is None:
            return None
        unknown = value - set(CHANNEL_BY_KEY)
        if unknown:
            raise ValueError(f"不支持的 UAV 通道: {', '.join(sorted(unknown))}")
        return value

    @model_validator(mode="after")
    def validate_channels_match_groups(self) -> UavSamplesInput:
        if self.fields and self.channels:
            allowed = {
                key
                for group in self.fields
                for key in GROUP_KEYS[group]
            }
            outside = self.channels - allowed
            if outside:
                raise ValueError(
                    "channels 必须属于 fields 指定的通道组: "
                    + ", ".join(sorted(outside))
                )
        return self


class UavChannelStatsInput(UavToolInput):
    """跨架次聚合指定 UAV 通道的统计。"""

    channels: set[str] = Field(
        min_length=1,
        max_length=6,
        description=(
            "需要统计的通道键，例如 cmp_comb_alt、cmp_ground_speed、"
            "cmp_pitch。键名以 list_uav_channels 返回为准。"
        ),
        examples=[{"cmp_comb_alt", "cmp_ground_speed"}],
    )

    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="可选：只统计该机型（0001/0007/sample）的架次。",
    )

    flight_kind: Literal["normal", "anomaly_sample"] | None = Field(
        default=None,
        description="可选：只统计正常或异常样本架次。",
    )

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: set[str]) -> set[str]:
        unknown = value - set(CHANNEL_BY_KEY)
        if unknown:
            raise ValueError(f"不支持的 UAV 通道: {', '.join(sorted(unknown))}")
        return value


class UavNormalVsAnomalyInput(UavToolInput):
    """对比正常与异常样本架次的通道统计。"""

    channels: set[str] = Field(
        min_length=1,
        max_length=6,
        description=(
            "需要对比的通道键，例如 cmp_pitch、cmp_roll、cmp_comb_alt。"
        ),
    )

    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description=(
            "可选：只对比该机型的正常与异常架次。"
            "注意异常样本的机型为 sample。"
        ),
    )

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: set[str]) -> set[str]:
        unknown = value - set(CHANNEL_BY_KEY)
        if unknown:
            raise ValueError(f"不支持的 UAV 通道: {', '.join(sorted(unknown))}")
        return value
