"""UAV 数据集通道目录：cmp 组 34 个特征的唯一事实来源。

该目录同时被以下模块引用：
- 导入脚本 `prepare_data/import_uav_to_mongo.py`（列头 -> 键名映射）
- REST API `/api/v1/uav/channels`（前端图表标题与单位渲染）
- `docs/UAV_DATA.md` 的通道表（手工维护，与本文件保持一致）

单位按数值范围推断，正式使用前请对照设备手册复核。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelSpec:
    """单个采样通道的定义。"""

    header: str  # xlsx 原始中文列头（含"激光捷联惯性导航设备发核心任务处理机A-"前缀）
    key: str  # ASCII 键名（数据库 samples[] 内的字段名）
    label: str  # 中文短标签（前端图表标题）
    unit: str  # 单位
    group: str  # 通道组：position/velocity/attitude/rates/accel/air


_HEADER_PREFIX = "激光捷联惯性导航设备发核心任务处理机A-"

CMP_CHANNELS: list[ChannelSpec] = [
    # 位置（9）
    ChannelSpec(_HEADER_PREFIX + "BDS经度", "cmp_bds_lon", "BDS经度", "deg", "position"),
    ChannelSpec(_HEADER_PREFIX + "BDS纬度", "cmp_bds_lat", "BDS纬度", "deg", "position"),
    ChannelSpec(_HEADER_PREFIX + "BDS高度", "cmp_bds_alt", "BDS高度", "m", "position"),
    ChannelSpec(_HEADER_PREFIX + "GPS经度", "cmp_gps_lon", "GPS经度", "deg", "position"),
    ChannelSpec(_HEADER_PREFIX + "GPS纬度", "cmp_gps_lat", "GPS纬度", "deg", "position"),
    ChannelSpec(_HEADER_PREFIX + "GPS高度", "cmp_gps_alt", "GPS高度", "m", "position"),
    ChannelSpec(_HEADER_PREFIX + "组合经度", "cmp_comb_lon", "组合经度", "deg", "position"),
    ChannelSpec(_HEADER_PREFIX + "组合纬度", "cmp_comb_lat", "组合纬度", "deg", "position"),
    ChannelSpec(_HEADER_PREFIX + "组合高度", "cmp_comb_alt", "组合高度", "m", "position"),
    # 速度（9）
    ChannelSpec(_HEADER_PREFIX + "BDS东向速度", "cmp_bds_ve", "BDS东向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "BDS北向速度", "cmp_bds_vn", "BDS北向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "BDS天向速度", "cmp_bds_vu", "BDS天向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "GPS东向速度", "cmp_gps_ve", "GPS东向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "GPS北向速度", "cmp_gps_vn", "GPS北向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "GPS天向速度", "cmp_gps_vu", "GPS天向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "组合东向速度", "cmp_comb_ve", "组合东向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "组合北向速度", "cmp_comb_vn", "组合北向速度", "m/s", "velocity"),
    ChannelSpec(_HEADER_PREFIX + "组合天向速度", "cmp_comb_vu", "组合天向速度", "m/s", "velocity"),
    # 姿态（3）
    ChannelSpec(_HEADER_PREFIX + "真航向", "cmp_heading", "真航向", "deg", "attitude"),
    ChannelSpec(_HEADER_PREFIX + "俯仰角", "cmp_pitch", "俯仰角", "deg", "attitude"),
    ChannelSpec(_HEADER_PREFIX + "横滚角", "cmp_roll", "横滚角", "deg", "attitude"),
    # 角速度（3）
    ChannelSpec(_HEADER_PREFIX + "横滚角速度", "cmp_roll_rate", "横滚角速度", "deg/s", "rates"),
    ChannelSpec(_HEADER_PREFIX + "偏航角速度", "cmp_yaw_rate", "偏航角速度", "deg/s", "rates"),
    ChannelSpec(_HEADER_PREFIX + "俯仰角速度", "cmp_pitch_rate", "俯仰角速度", "deg/s", "rates"),
    # 加速度（3）
    ChannelSpec(_HEADER_PREFIX + "前向加速度", "cmp_acc_fwd", "前向加速度", "m/s2", "accel"),
    ChannelSpec(_HEADER_PREFIX + "法向加速度", "cmp_acc_norm", "法向加速度", "m/s2", "accel"),
    ChannelSpec(_HEADER_PREFIX + "侧向加速度", "cmp_acc_lat", "侧向加速度", "m/s2", "accel"),
    # 气动与环境（7）
    ChannelSpec(_HEADER_PREFIX + "地速", "cmp_ground_speed", "地速", "m/s", "air"),
    ChannelSpec(_HEADER_PREFIX + "惯性迎角", "cmp_aoa", "惯性迎角", "deg", "air"),
    ChannelSpec(_HEADER_PREFIX + "惯性侧滑角", "cmp_ssa", "惯性侧滑角", "deg", "air"),
    ChannelSpec(_HEADER_PREFIX + "风向", "cmp_wind_dir", "风向", "deg", "air"),
    ChannelSpec(_HEADER_PREFIX + "风速", "cmp_wind_speed", "风速", "m/s", "air"),
    ChannelSpec(_HEADER_PREFIX + "偏流角", "cmp_drift_angle", "偏流角", "deg", "air"),
    ChannelSpec(_HEADER_PREFIX + "垂直航迹角", "cmp_flight_path_angle", "垂直航迹角", "deg", "air"),
]

CMP_CHANNEL_KEYS: list[str] = [channel.key for channel in CMP_CHANNELS]
CHANNEL_BY_KEY: dict[str, ChannelSpec] = {channel.key: channel for channel in CMP_CHANNELS}

# samples 端点 fields 参数允许的通道组
CHANNEL_GROUPS: frozenset[str] = frozenset(
    {"position", "velocity", "attitude", "rates", "accel", "air"}
)

GROUP_KEYS: dict[str, list[str]] = {
    group: [channel.key for channel in CMP_CHANNELS if channel.group == group]
    for group in sorted(CHANNEL_GROUPS)
}

# 摘要使用的通道
SUMMARY_ALT_KEY = "cmp_comb_alt"
SUMMARY_LON_KEY = "cmp_comb_lon"
SUMMARY_LAT_KEY = "cmp_comb_lat"
SUMMARY_SPEED_KEY = "cmp_ground_speed"
