"""M6-04/M6-05 基线预测器：匀速/恒航向与大圆传播，Kalman 常速度模型。

- `ConstantVelocityHeadingPredictor`：用观测窗口端点的**大圆距离/时间**估地速，
  并沿窗口首末两点确定的大圆**继续等速外推**（向量旋转实现，非平面近似）；
  高度按末点恒定。
- `KalmanConstantVelocityPredictor`：在局部 ENU 内做常速度 Kalman 滤波
  （状态 `[e, n, ve, vn]`，位置观测），再按目标时刻线性外推；高度按末点恒定。

两者的默认参数是工程取值（可覆盖），随 `predictor_version` 记录；输入不满足
`validate_request` 约束时抛出 `ValueError`，不产生任何兜底猜测。
"""

from __future__ import annotations

from datetime import timedelta
from math import asin, atan2, cos, degrees, radians, sin, sqrt

from scat.prediction.alignment import (
    EARTH_RADIUS_M,
    PredictedPoint,
    haversine_metres,
)
from scat.prediction.geo import geodetic_to_local_m, local_to_geodetic_m
from scat.prediction.interfaces import (
    PredictionRequest,
    PredictionResult,
    validate_request,
)

__all__ = [
    "ConstantVelocityHeadingPredictor",
    "KalmanConstantVelocityPredictor",
]


def _to_unit_vector(lat_deg: float, lon_deg: float) -> tuple[float, float, float]:
    lat, lon = radians(lat_deg), radians(lon_deg)
    return cos(lat) * cos(lon), cos(lat) * sin(lon), sin(lat)


def _from_unit_vector(vector: tuple[float, float, float]) -> tuple[float, float]:
    x, y, z = vector
    lat = degrees(asin(max(-1.0, min(1.0, z))))
    lon = degrees(atan2(y, x))
    return lat, ((lon + 540.0) % 360.0) - 180.0


def _extend_great_circle(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    distance_m: float,
) -> tuple[float, float]:
    """沿 (P1 → P2) 确定的大圆，自 P2 继续推进 distance（米）后的坐标。"""
    a = _to_unit_vector(lat1, lon1)
    b = _to_unit_vector(lat2, lon2)
    nx = a[1] * b[2] - a[2] * b[1]
    ny = a[2] * b[0] - a[0] * b[2]
    nz = a[0] * b[1] - a[1] * b[0]
    norm = sqrt(nx * nx + ny * ny + nz * nz)
    if norm < 1e-12:
        raise ValueError("窗口两点重合，无法确定大圆方向")
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    theta = distance_m / EARTH_RADIUS_M
    cos_theta, sin_theta = cos(theta), sin(theta)
    cx = b[0] * cos_theta + (ny * b[2] - nz * b[1]) * sin_theta
    cy = b[1] * cos_theta + (nz * b[0] - nx * b[2]) * sin_theta
    cz = b[2] * cos_theta + (nx * b[1] - ny * b[0]) * sin_theta
    return _from_unit_vector((cx, cy, cz))


class ConstantVelocityHeadingPredictor:
    """匀速/恒航向基线（M6-04）。"""

    predictor_id = "constant-velocity-heading"
    predictor_version = "1"
    applicability_note = (
        "适用于近似直线飞行段（巡航/中低空）的短期外推；转弯与机动段误差显著偏大，"
        "长时域（>10 分钟）建议只作量级参考。"
    )

    def __init__(self, *, window_s: float = 300.0) -> None:
        """`window_s`：估计速度/航向所用的最近观测窗口（秒）。"""
        if window_s <= 0:
            raise ValueError("window_s 必须为正数")
        self.window_s = float(window_s)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        validate_request(request)
        observed = request.observed
        last = observed[-1]
        window_start = last.time - timedelta(seconds=self.window_s)
        window = [point for point in observed if point.time >= window_start]
        if len(window) < 2:
            # 窗口内不足两点时回退到整段首末（确定性回退，不是猜测）。
            window = [observed[0], last]
        first, last_window = window[0], window[-1]
        span_s = (last_window.time - first.time).total_seconds()
        if span_s <= 0:
            raise ValueError("观测窗口内时间跨度为零，无法估计速度")

        distance_m = haversine_metres(first.lat, first.lon, last_window.lat, last_window.lon)
        speed_mps = distance_m / span_s

        fixes: list[PredictedPoint] = []
        for target in request.target_times:
            ahead_s = (target - last.time).total_seconds()
            if distance_m == 0.0:
                lat, lon = last.lat, last.lon  # 原地静止：位置不变
            else:
                lat, lon = _extend_great_circle(
                    first.lat,
                    first.lon,
                    last_window.lat,
                    last_window.lon,
                    speed_mps * max(ahead_s, 0.0),
                )
            fixes.append(PredictedPoint(time=target, lat=lat, lon=lon, altitude_m=last.altitude_m))
        return PredictionResult(
            predictor_id=self.predictor_id,
            predictor_version=self.predictor_version,
            fixes=tuple(fixes),
        )


def _mat_mul(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
    return [[sum(a * b for a, b in zip(row, column)) for column in zip(*right)] for row in left]


def _transpose(matrix: list[list[float]]) -> list[list[float]]:
    return [list(column) for column in zip(*matrix)]


def _identity(size: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]


def _inverse2(matrix: list[list[float]]) -> list[list[float]]:
    (a, b), (c, d) = matrix
    determinant = a * d - b * c
    if determinant == 0.0:
        raise ValueError("奇异矩阵，无法求逆")
    return [[d / determinant, -b / determinant], [-c / determinant, a / determinant]]


class KalmanConstantVelocityPredictor:
    """常速度 Kalman 基线（M6-05，局部 ENU 实现）。"""

    predictor_id = "kalman-cv"
    predictor_version = "1"
    applicability_note = (
        "基于常速度假设的滤波外推：巡航段短期（1–5 分钟）精度最好（实测 MAE 数百米）；"
        "机动/下降段随观测截止点误差增大，长时域（>15 分钟）误差与飞行阶段强相关。"
    )

    def __init__(
        self,
        *,
        process_noise_mps2: float = 2.0,
        measurement_noise_m: float = 30.0,
    ) -> None:
        """`process_noise_mps2`：加速度过程噪声 σ；`measurement_noise_m`：位置观测噪声 σ。"""
        if process_noise_mps2 <= 0 or measurement_noise_m <= 0:
            raise ValueError("噪声参数必须为正数")
        self.process_noise_mps2 = float(process_noise_mps2)
        self.measurement_noise_m = float(measurement_noise_m)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        validate_request(request)
        observed = request.observed
        origin = observed[0]
        origin_alt = origin.altitude_m or 0.0

        local: list[tuple[float, float, float]] = [
            geodetic_to_local_m(
                point.lat,
                point.lon,
                point.altitude_m or 0.0,
                origin_lat=origin.lat,
                origin_lon=origin.lon,
                origin_alt_m=origin_alt,
            )
            for point in observed
        ]

        state = [local[0][0], local[0][1], 0.0, 0.0]
        covariance = [
            [1.0e4, 0.0, 0.0, 0.0],
            [0.0, 1.0e4, 0.0, 0.0],
            [0.0, 0.0, 2.5e3, 0.0],
            [0.0, 0.0, 0.0, 2.5e3],
        ]
        measurement_matrix = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        transposed = _transpose(measurement_matrix)
        measurement_noise = [
            [self.measurement_noise_m**2, 0.0],
            [0.0, self.measurement_noise_m**2],
        ]

        for index in range(1, len(observed)):
            dt = (observed[index].time - observed[index - 1].time).total_seconds()
            if dt <= 0:
                raise ValueError("观测轨迹存在非正时间间隔")
            # 预测步：F x, F P Fᵀ + Q（连续白噪声加速度模型）。
            transition = [
                [1.0, 0.0, dt, 0.0],
                [0.0, 1.0, 0.0, dt],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
            state = [sum(row[j] * state[j] for j in range(4)) for row in transition]
            sigma2 = self.process_noise_mps2**2
            process_noise = [
                [sigma2 * dt**3 / 3.0, 0.0, sigma2 * dt**2 / 2.0, 0.0],
                [0.0, sigma2 * dt**3 / 3.0, 0.0, sigma2 * dt**2 / 2.0],
                [sigma2 * dt**2 / 2.0, 0.0, sigma2 * dt, 0.0],
                [0.0, sigma2 * dt**2 / 2.0, 0.0, sigma2 * dt],
            ]
            covariance = _mat_mul(_mat_mul(transition, covariance), _transpose(transition))
            covariance = [
                [covariance[i][j] + process_noise[i][j] for j in range(4)] for i in range(4)
            ]
            # 更新步：位置观测。
            measurement = [local[index][0], local[index][1]]
            innovation = [measurement[0] - state[0], measurement[1] - state[1]]
            innovation_covariance = _mat_mul(_mat_mul(measurement_matrix, covariance), transposed)
            innovation_covariance = [
                [innovation_covariance[i][j] + measurement_noise[i][j] for j in range(2)]
                for i in range(2)
            ]
            gain = _mat_mul(_mat_mul(covariance, transposed), _inverse2(innovation_covariance))
            state = [state[i] + sum(gain[i][j] * innovation[j] for j in range(2)) for i in range(4)]
            identity = _identity(4)
            residual_gain = [
                [
                    identity[i][j]
                    - gain[i][0] * measurement_matrix[0][j]
                    - gain[i][1] * measurement_matrix[1][j]
                    for j in range(4)
                ]
                for i in range(4)
            ]
            covariance = _mat_mul(residual_gain, covariance)

        last = observed[-1]
        last_up = local[-1][2]
        fixes: list[PredictedPoint] = []
        for target in request.target_times:
            ahead_s = (target - last.time).total_seconds()
            east = state[0] + state[2] * max(ahead_s, 0.0)
            north = state[1] + state[3] * max(ahead_s, 0.0)
            lat, lon, _alt = local_to_geodetic_m(
                east,
                north,
                last_up,
                origin_lat=origin.lat,
                origin_lon=origin.lon,
                origin_alt_m=origin_alt,
            )
            fixes.append(PredictedPoint(time=target, lat=lat, lon=lon, altitude_m=last.altitude_m))
        return PredictionResult(
            predictor_id=self.predictor_id,
            predictor_version=self.predictor_version,
            fixes=tuple(fixes),
        )
