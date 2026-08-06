"""ORS の GeoJSON を `ProviderRoute` に読み替える（design.md 1.2 / 7.1、AC-04-4）。

**ORS のレスポンス形式に依存する知識は、このモジュールの中だけに置く。**
キー名（`features` / `segments` / `way_points` ...）、`"-"` が「名前なし」を
表すこと、maneuver の種別が 0〜13 の整数であることは、いずれも ORS の都合である。
上位層（`generation.py` / `checkpoints.py`）はこれらを一切知らない。
別のサービスを足すときに写す範囲がこのファイルに収まる（ADR-0001）。

**決めないこと。** どの種別が方向転換かは決めない（AC-04-4 のホワイトリストは
`checkpoints.py` の責務。要件由来の規則を、プロバイダの都合を閉じる層に
置かないため）。距離の判定（±300m）も接近距離の算出も行わない（design.md 3.1）。

**読めない形はすべて `MalformedRoute` にする。** 素の `KeyError` を上げると、
上位が「プロバイダ由来の失敗」を1か所で捕まえる経路（AC-06-4）が破れる。
例外メッセージには**どのキーで躓いたかだけ**を書き、値は入れない
（座標は自宅である。design.md 8.7）。
"""

from collections.abc import Mapping, Sequence
from typing import Final, NoReturn

from runloop.models import LatLon, Maneuver, ProviderRoute, RawStep
from runloop.ports import MalformedRoute

# maneuver の種別（design.md 7.1 の表）。**番号を知るのはこの対応表だけ。**
# 13 は「不明」ではなく右側維持（`instruction` が "Keep right"。12 の対）。
# 2026-08-06 に実測で確認し、design.md 7.1 と FINDINGS 訂正6 で訂正した。
# 表にない番号（ラウンドアバウト系 7 / 8、U ターン 9 ほか。いずれも未観測）は
# `UNKNOWN` にする。**知らない番号が来ることは異常ではない**（AC-04-4）。
_MANEUVER_BY_TYPE: Final[Mapping[int, Maneuver]] = {
    0: Maneuver.TURN_LEFT,
    1: Maneuver.TURN_RIGHT,
    2: Maneuver.SHARP_LEFT,
    3: Maneuver.SHARP_RIGHT,
    4: Maneuver.SLIGHT_LEFT,
    5: Maneuver.SLIGHT_RIGHT,
    6: Maneuver.STRAIGHT,
    10: Maneuver.ARRIVE,
    11: Maneuver.DEPART,
    12: Maneuver.KEEP_LEFT,
    13: Maneuver.KEEP_RIGHT,
}

# ORS が「名前のない道」を表す値。実測では 71/71 = 100% がこれ（design.md 7.1）
_NO_NAME: Final = "-"

# GeoJSON の座標は `[経度, 緯度, 標高]` の順。標高は `LatLon` が持たないので捨てる
_LON: Final = 0
_LAT: Final = 1


def to_provider_route(
    payload: object,
    *,
    seed: int,
    ratelimit_remaining: int | None = None,
) -> ProviderRoute:
    """directions の応答（GeoJSON）を1本の `ProviderRoute` にする。

    `seed` と `ratelimit_remaining` を引数で受けるのは、**どちらも応答の本文に
    無い情報**だからである。seed は呼び出し側が要求した値（応答の `metadata` の
    echo を読むと「応答が要求を反映している」という未検証の仮定が増える）、
    残数は HTTP ヘッダにあり、本文しか見ないこの関数からは読めない。

    接近距離は算出しない。起点はアプリ側の概念で、プロバイダの成果ではない
    （`generation.py` が `geo.haversine(起点, snapped_start)` で出す。design.md 2.2）。
    """
    document = _mapping(payload, "応答")
    features = _sequence(_entry(document, "features", "応答"), "features")
    if not features:
        _fail("features が空")

    feature = _mapping(features[0], "features[0]")
    properties = _mapping(_entry(feature, "properties", "features[0]"), "properties")
    summary = _mapping(_entry(properties, "summary", "properties"), "summary")
    geometry = _read_geometry(_mapping(_entry(feature, "geometry", "features[0]"), "geometry"))

    return ProviderRoute(
        seed=seed,
        loop_m=_number(_entry(summary, "distance", "summary"), "summary.distance"),
        ascent_m=_number(_entry(properties, "ascent", "properties"), "ascent"),
        descent_m=_number(_entry(properties, "descent", "properties"), "descent"),
        # ルート始点 = スナップ先。接近距離の算出元（design.md 2.2）
        snapped_start=geometry[0],
        geometry=geometry,
        steps=_read_steps(properties, geometry),
        ratelimit_remaining=ratelimit_remaining,
    )


def _read_geometry(geometry: Mapping[str, object]) -> tuple[LatLon, ...]:
    """座標列を読む。空なら変換できない（`snapped_start` が取れない）。"""
    coordinates = _sequence(_entry(geometry, "coordinates", "geometry"), "geometry.coordinates")
    if not coordinates:
        _fail("geometry.coordinates が空")
    return tuple(_read_point(item) for item in coordinates)


def _read_point(item: object) -> LatLon:
    """`[経度, 緯度, 標高]` を `LatLon` にする。**順序を取り違えない。**

    緯度と経度を逆に読んでも型は通り、地図に別の場所が出るだけで例外にならない。
    ここが ORS 固有の順序を扱う唯一の場所である。
    """
    values = _sequence(item, "座標")
    if len(values) < 2:
        _fail("座標が経度・緯度の2値に満たない")
    return LatLon(lat=_number(values[_LAT], "緯度"), lon=_number(values[_LON], "経度"))


def _read_steps(
    properties: Mapping[str, object],
    geometry: tuple[LatLon, ...],
) -> tuple[RawStep, ...]:
    """案内の列を読む。segment が複数なら順に連結する。

    起点1つの周回では segment は1つだが、連結しておけば「想定外だが読める」
    応答で案内を落とさない。**0件は異常ではない**（design.md 7.3）が、
    `steps` のキー自体が無いのは変換できない形として扱う。
    """
    segments = _sequence(_entry(properties, "segments", "properties"), "segments")
    steps: list[RawStep] = []
    for segment in segments:
        entries = _sequence(_entry(_mapping(segment, "segments[]"), "steps", "segments[]"), "steps")
        steps += [_read_step(_mapping(entry, "steps[]"), geometry) for entry in entries]
    return tuple(steps)


def _read_step(step: Mapping[str, object], geometry: tuple[LatLon, ...]) -> RawStep:
    """案内1件を `RawStep` にする。

    座標は `way_points[0]`（案内が始まる点）を geometry から引く（design.md 7.1）。
    **添字の範囲を必ず確かめる。** 負の添字は Python では末尾から数えられ、
    静かに別の地点を指す。
    """
    way_points = _sequence(_entry(step, "way_points", "steps[]"), "steps[].way_points")
    if not way_points:
        _fail("steps[].way_points が空")
    index = _integer(way_points[0], "steps[].way_points[0]")
    if not 0 <= index < len(geometry):
        _fail("steps[].way_points[0] が geometry の範囲外")

    return RawStep(
        # その step 単体の距離。累積にするのは checkpoints.py（design.md 7.1 / 2.3）
        distance_m=_number(_entry(step, "distance", "steps[]"), "steps[].distance"),
        maneuver=_read_maneuver(_integer(_entry(step, "type", "steps[]"), "steps[].type")),
        position=geometry[index],
        name=_read_name(step),
    )


def _read_maneuver(ors_type: int) -> Maneuver:
    """種別の番号を型にする。対応表に無ければ `UNKNOWN`。

    `.get(番号, UNKNOWN)` と書かないのは、`runloop/` 全体で
    `.get(キー, 既定値)` を禁じているため（design.md 3.3、T04）。
    設定の取りこぼしを静かに埋める形と見分けがつかなくなる。
    """
    if ors_type in _MANEUVER_BY_TYPE:
        return _MANEUVER_BY_TYPE[ors_type]
    return Maneuver.UNKNOWN


def _read_name(step: Mapping[str, object]) -> str | None:
    """道の名前を読む。`"-"` は「名前なし」なので `None` にする（AC-04-4）。

    **警告もログも出さない。** 実測では 71/71 = 100% が `"-"` で、名前が無いことは
    異常ではなく通常である（design.md 7.1）。1件ずつ記録すると、本当に見たい
    ログ（残数・失敗の内訳）が埋もれる。
    """
    if "name" not in step:
        return None
    value: object = step["name"]
    if value is None or value == _NO_NAME:
        return None
    if not isinstance(value, str):
        _fail("steps[].name が文字列ではない")
    return value


# --- 読み取りの土台（想定外の形はここで `MalformedRoute` になる） -------------


def _fail(where: str) -> NoReturn:
    """変換できない形を報告する。**値をメッセージに入れない**（design.md 8.7）。"""
    raise MalformedRoute(f"ORS の応答が想定の形ではない: {where}")


def _mapping(value: object, where: str) -> Mapping[str, object]:
    """辞書として読む。"""
    if not isinstance(value, Mapping):
        _fail(f"{where} が辞書ではない")
    return value


def _sequence(value: object, where: str) -> Sequence[object]:
    """配列として読む。文字列は「文字の並び」として通ってしまうので弾く。"""
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        _fail(f"{where} が配列ではない")
    return value


def _entry(mapping: Mapping[str, object], key: str, where: str) -> object:
    """キーを取り出す。**既定値で埋めない**（design.md 3.3、T04）。

    欠けていることは「変換できない応答が来た」という事実であり、
    それらしい値を置いて先に進むと、間違いに気づく手がかりが消える。
    """
    if key not in mapping:
        _fail(f"{where} に {key} がない")
    value: object = mapping[key]
    return value


def _number(value: object, where: str) -> float:
    """数値として読む。`bool` は `int` の派生なので除く。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(f"{where} が数値ではない")
    return float(value)


def _integer(value: object, where: str) -> int:
    """整数として読む（種別の番号と geometry の添字）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{where} が整数ではない")
    return value
