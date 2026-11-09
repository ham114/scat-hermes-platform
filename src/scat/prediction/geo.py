"""局部 ENU 坐标转换（M6 预测器的几何工具）。

与 `scat.preprocessing.phases` 的 ENU 约定一致（WGS84 → ECEF → 站心 ENU），
但以**米**为单位、支持任意原点，并提供 ENU → 大地坐标的逆变换。

对外只用两个稳定函数；ECEF 细节保持私有：

- `geodetic_to_local_m(lat, lon, alt_m, *, origin_lat, origin_lon, origin_alt_m)`；
- `local_to_geodetic_m(east_m, north_m, up_m, *, origin_lat, origin_lon, origin_alt_m)`。
"""

from __future__ import annotations

from math import atan2, cos, degrees, radians, sin, sqrt

from scat.preprocessing.phases import WGS84_A, WGS84_E2

__all__ = ["geodetic_to_local_m", "local_to_geodetic_m"]


def _geodetic_to_ecef_m(lat_deg: float, lon_deg: float, alt_m: float) -> tuple[float, float, float]:
    lat = radians(lat_deg)
    lon = radians(lon_deg)
    sin_lat, cos_lat = sin(lat), cos(lat)
    sin_lon, cos_lon = sin(lon), cos(lon)
    prime_vertical = WGS84_A / sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (prime_vertical + alt_m) * cos_lat * cos_lon
    y = (prime_vertical + alt_m) * cos_lat * sin_lon
    z = (prime_vertical * (1.0 - WGS84_E2) + alt_m) * sin_lat
    return x, y, z


def _ecef_to_geodetic_m(x: float, y: float, z: float) -> tuple[float, float, float]:
    """ECEF（米）→ 大地坐标（度/度/米）；迭代法，数步内收敛。"""
    lon = atan2(y, x)
    p = sqrt(x * x + y * y)
    if p < 1e-9:  # 极点附近退化保护（航迹不会出现，仅保证数值稳定）
        return (90.0 if z >= 0 else -90.0), degrees(lon), abs(z) - WGS84_A
    lat = atan2(z, p * (1.0 - WGS84_E2))
    for _ in range(8):
        sin_lat = sin(lat)
        prime_vertical = WGS84_A / sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        alt = p / cos(lat) - prime_vertical
        new_lat = atan2(
            z,
            p * (1.0 - WGS84_E2 * prime_vertical / (prime_vertical + alt)),
        )
        if abs(new_lat - lat) < 1e-13:
            lat = new_lat
            break
        lat = new_lat
    sin_lat = sin(lat)
    prime_vertical = WGS84_A / sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    alt = p / cos(lat) - prime_vertical
    return degrees(lat), degrees(lon), alt


def geodetic_to_local_m(
    lat: float,
    lon: float,
    alt_m: float,
    *,
    origin_lat: float,
    origin_lon: float,
    origin_alt_m: float,
) -> tuple[float, float, float]:
    """大地坐标 → 以 (origin_lat, origin_lon, origin_alt_m) 为原点的 ENU（米）。"""
    x0, y0, z0 = _geodetic_to_ecef_m(origin_lat, origin_lon, origin_alt_m)
    x, y, z = _geodetic_to_ecef_m(lat, lon, alt_m)
    dx, dy, dz = x - x0, y - y0, z - z0
    lat0, lon0 = radians(origin_lat), radians(origin_lon)
    sin_lat0, cos_lat0 = sin(lat0), cos(lat0)
    sin_lon0, cos_lon0 = sin(lon0), cos(lon0)
    east = -sin_lon0 * dx + cos_lon0 * dy
    north = -sin_lat0 * cos_lon0 * dx - sin_lat0 * sin_lon0 * dy + cos_lat0 * dz
    up = cos_lat0 * cos_lon0 * dx + cos_lat0 * sin_lon0 * dy + sin_lat0 * dz
    return east, north, up


def local_to_geodetic_m(
    east_m: float,
    north_m: float,
    up_m: float,
    *,
    origin_lat: float,
    origin_lon: float,
    origin_alt_m: float,
) -> tuple[float, float, float]:
    """ENU（米，原点同 `geodetic_to_local_m`）→ 大地坐标（度/度/米）。"""
    x0, y0, z0 = _geodetic_to_ecef_m(origin_lat, origin_lon, origin_alt_m)
    lat0, lon0 = radians(origin_lat), radians(origin_lon)
    sin_lat0, cos_lat0 = sin(lat0), cos(lat0)
    sin_lon0, cos_lon0 = sin(lon0), cos(lon0)
    dx = -sin_lon0 * east_m - sin_lat0 * cos_lon0 * north_m + cos_lat0 * cos_lon0 * up_m
    dy = cos_lon0 * east_m - sin_lat0 * sin_lon0 * north_m + cos_lat0 * sin_lon0 * up_m
    dz = cos_lat0 * north_m + sin_lat0 * up_m
    return _ecef_to_geodetic_m(x0 + dx, y0 + dy, z0 + dz)
