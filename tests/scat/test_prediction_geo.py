"""M6 geo 工具测试：ENU 往返精度与量纲。"""

from __future__ import annotations

import pytest

from scat.prediction.geo import geodetic_to_local_m, local_to_geodetic_m

CASES = [
    (59.0, 18.0, 300.0, 59.05, 18.02, 1000.0),
    (-33.9, 151.2, 150.0, -33.85, 151.25, 1200.0),
    (10.0, 179.95, 200.0, 10.05, -179.95, 1100.0),  # 跨 ±180°
]


@pytest.mark.parametrize(
    ("origin_lat", "origin_lon", "origin_alt", "lat", "lon", "alt"),
    CASES,
)
def test_enu_round_trip_is_metric_accurate(
    origin_lat: float,
    origin_lon: float,
    origin_alt: float,
    lat: float,
    lon: float,
    alt: float,
) -> None:
    east, north, up = geodetic_to_local_m(
        lat,
        lon,
        alt,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_alt_m=origin_alt,
    )
    back_lat, back_lon, back_alt = local_to_geodetic_m(
        east,
        north,
        up,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_alt_m=origin_alt,
    )
    assert back_lat == pytest.approx(lat, abs=1e-9)
    assert back_lon == pytest.approx(lon, abs=1e-9)
    assert back_alt == pytest.approx(alt, abs=1e-6)


def test_east_offset_is_metric_scale() -> None:
    # 59°N 上向东 0.01° 约 573 m（东向分量主导、北向近似为零）。
    east, north, _ = geodetic_to_local_m(
        59.0,
        18.01,
        0.0,
        origin_lat=59.0,
        origin_lon=18.0,
        origin_alt_m=0.0,
    )
    assert 560.0 <= east <= 585.0
    assert abs(north) <= 2.0
