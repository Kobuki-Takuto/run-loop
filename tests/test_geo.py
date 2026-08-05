"""geo.haversine のテスト（design.md 10.1「既知の2点で期待値」）。

期待値は実装と別の式から出す。球面上の子午線・赤道に沿った距離は
``R × 弧度`` で解析的に求まるので、haversine の式を使わずに期待値を書ける
（実装をそのまま写した循環したテストにならない）。
球近似の半径はスパイクと同じ 6,371,000m（spike/ors_feasibility.py:99）。
"""

import math

import pytest

from runloop.geo import EARTH_RADIUS_M, haversine
from runloop.models import LatLon

# 赤道に沿った経度1度。球面では R × 弧度 に一致する
EQUATOR_ONE_DEGREE_M = 6_371_000.0 * math.radians(1.0)
# 子午線に沿った緯度 0.001 度。緯度に依らず R × 弧度
MERIDIAN_MILLI_DEGREE_M = 6_371_000.0 * math.radians(0.001)


def test_earth_radius_matches_spike() -> None:
    """半径が変わると接近距離も変わり、AC-01-3 の 50m / 300m 判定がずれる。"""
    assert EARTH_RADIUS_M == 6_371_000.0


def test_same_point_is_zero() -> None:
    """同一の点は 0m。接近距離 0 が OK 側に入ることの前提（design.md 5.1）。"""
    point = LatLon(lat=31.5966, lon=130.5571)

    assert haversine(point, point) == 0.0


def test_one_degree_along_equator() -> None:
    """赤道に沿った経度1度が解析値 R × 弧度 と一致する。"""
    actual = haversine(LatLon(lat=0.0, lon=0.0), LatLon(lat=0.0, lon=1.0))

    assert actual == pytest.approx(EQUATOR_ONE_DEGREE_M, abs=1e-6)


def test_milli_degree_along_meridian() -> None:
    """子午線に沿った緯度 0.001 度が解析値と一致する（接近距離と同じ 100m 前後）。"""
    actual = haversine(LatLon(lat=31.5966, lon=130.5571), LatLon(lat=31.5976, lon=130.5571))

    assert actual == pytest.approx(MERIDIAN_MILLI_DEGREE_M, abs=1e-6)


def test_lat_and_lon_are_not_swapped() -> None:
    """緯度 31.6 度では、同じ 0.001 度でも経度方向が cos(緯度) 倍だけ短い。

    緯度と経度を取り違えた実装ではこの2つが同じ値になり、テストが落ちる。
    """
    origin = LatLon(lat=31.5966, lon=130.5571)
    east = haversine(origin, LatLon(lat=31.5966, lon=130.5581))
    north = haversine(origin, LatLon(lat=31.5976, lon=130.5571))

    assert east == pytest.approx(94.711, abs=0.01)
    assert north == pytest.approx(111.195, abs=0.01)
    assert east < north


def test_is_symmetric() -> None:
    """引数の順序を入れ替えても同じ距離になる。"""
    a = LatLon(lat=31.5966, lon=130.5571)
    b = LatLon(lat=31.6001, lon=130.5600)

    assert haversine(a, b) == pytest.approx(haversine(b, a), abs=1e-9)


def test_distant_pair_has_right_magnitude() -> None:
    """東京駅と大阪駅。球面余弦定理で求めた 403,057.53m と一致する。

    許容 1m は式の違いによる浮動小数の差を吸収するためで、
    半径の誤りや cos(緯度) の抜けはこの幅では通らない。
    """
    tokyo = LatLon(lat=35.681236, lon=139.767125)
    osaka = LatLon(lat=34.702485, lon=135.495951)

    assert haversine(tokyo, osaka) == pytest.approx(403_057.53, abs=1.0)
