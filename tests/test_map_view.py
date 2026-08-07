"""ui/map_view.py のテスト（design.md 1.2 / 10.4、requirements.md AC-01-1 / AC-04 / AC-05-1〜3）。

地図の見た目そのものは手動確認とする方針（design.md 10.4「地図描画」）。
ここで自動固定するのは3つだけ。

1. 生成された HTML に想定の座標が含まれること（起点マーカー・ループの
   ポリライン・チェックポイントのマーカー。WORKFLOW.md フェーズ4 のテスト方針）
2. **接近区間の線を描かないこと**（design.md 7.2「接近区間の道は描けない」）。
   起点とループの始点をつなぐ線を足すと、実在しない道を描くことになる。
   ポリラインがちょうど1本（ループのみ）であることで固定する
3. チェックポイントの表示内容（方向転換の向き・距離・地点名の有無）が
   AC-04-2 / AC-04-4 の要求を満たすこと

タップしやすい部品サイズ（非機能要件）は自動テストの対象外で、手動確認する
（design.md 10.4）。
"""

import json
import re

import folium
import pytest

from runloop import messages
from runloop.models import Candidate, Checkpoint, LatLon, TurnDirection
from ui import map_view


def html_of(folium_map: folium.Map) -> str:
    """folium.Map から埋め込み JS を含む完全な HTML を取り出す。"""
    return folium_map.get_root().render()


def marker_coordinates(html: str) -> list[float]:
    """HTML から `L.marker(...)` の座標（`[lat, lon]`）を取り出す。

    `"31.6" in html` のような部分一致だけでは、緯度経度を入れ替えても
    両方の数字がどこかに存在するので検出できない。配列の並びごと比較する。
    """
    match = re.search(r"L\.marker\(\s*(\[[^\]]*\])", html)
    assert match is not None, f"L.marker の座標が見つからない: {html}"
    result: list[float] = json.loads(match.group(1))
    return result


def circle_marker_coordinates(html: str) -> list[list[float]]:
    """HTML から `L.circleMarker(...)` の座標をすべて取り出す（出現順）。"""
    return [json.loads(m.group(1)) for m in re.finditer(r"L\.circleMarker\(\s*(\[[^\]]*\])", html)]


def polyline_coordinates(html: str) -> list[list[float]]:
    """HTML から `L.polyline(...)` の座標配列を取り出す。

    `html.count("L.polyline")` だけでは、既存の1本に点を追加する形の
    改変（起点をループの先頭に足すなど）を見逃す。座標配列そのものを
    比較することで、ポリラインの中身まで固定する。
    """
    match = re.search(r"L\.polyline\(\s*(\[\[.*?\]\])", html, re.S)
    assert match is not None, f"L.polyline の座標配列が見つからない: {html}"
    result: list[list[float]] = json.loads(match.group(1))
    return result


def make_candidate(*, geometry: tuple[LatLon, ...]) -> Candidate:
    """テスト用の候補を組む。地図描画には geometry しか使わない。"""
    return Candidate(
        seed=1,
        loop_m=5_000.0,
        approach_m=80.0,
        ascent_m=50.0,
        descent_m=50.0,
        target_m=5_000,
        geometry=geometry,
    )


def make_checkpoint(
    *,
    order: int = 1,
    lat: float = 31.601,
    lon: float = 130.558,
    direction: TurnDirection = TurnDirection.TURN_LEFT,
    name: str | None = None,
    distance_from_origin_m: float = 1_000.0,
) -> Checkpoint:
    """テスト用のチェックポイントを組む。"""
    return Checkpoint(
        order=order,
        distance_from_origin_m=distance_from_origin_m,
        direction=direction,
        name=name,
        position=LatLon(lat=lat, lon=lon),
    )


# --- 1. 起点未指定（AC-05-1 の前提。クリックできる地図は出す） --------------


def test_build_map_without_origin_returns_a_map() -> None:
    """起点が無くても地図オブジェクトを返すこと（クリックして起点を指定する前提）。"""
    result = map_view.build_map()

    assert isinstance(result, folium.Map)


def test_build_map_without_origin_has_no_marker() -> None:
    """起点が無ければマーカーを置かないこと。"""
    html = html_of(map_view.build_map())

    assert "L.marker" not in html


# --- 2. 起点マーカー（AC-05-2） ----------------------------------------------


def test_build_map_places_a_marker_at_the_origin() -> None:
    """AC-05-2「設定された起点が地図上にマーカーで表示される」。"""
    origin = LatLon(lat=31.601234, lon=130.558765)

    html = html_of(map_view.build_map(origin=origin))

    assert "L.marker" in html
    assert marker_coordinates(html) == [31.601234, 130.558765]


def test_build_map_without_candidate_has_no_polyline() -> None:
    """候補がまだ無ければループのポリラインを描かないこと（起点確定だけの状態）。"""
    html = html_of(map_view.build_map(origin=LatLon(lat=31.6, lon=130.55)))

    assert "L.polyline" not in html


# --- 3. ループのポリライン（AC-01-1 の描画部分、design.md 7.2） -------------


def test_build_map_draws_the_loop_polyline() -> None:
    """候補があればループの座標列がポリラインとして描かれること。"""
    geometry = (
        LatLon(lat=31.60101, lon=130.55801),
        LatLon(lat=31.60202, lon=130.55902),
        LatLon(lat=31.60303, lon=130.56003),
    )
    candidate = make_candidate(geometry=geometry)

    html = html_of(map_view.build_map(origin=LatLon(lat=31.6, lon=130.55), candidate=candidate))

    for point in geometry:
        assert str(point.lat) in html
        assert str(point.lon) in html


def test_build_map_draws_exactly_one_polyline() -> None:
    """接近区間の線を描かないこと（design.md 7.2）。

    起点とループの始点をつなぐ線を足すと、実在しない接近区間の道を
    描くことになる。起点をループの始点から離して置き、ポリラインが
    ちょうど1本（ループそのもの）であることを固定する。
    """
    geometry = (LatLon(lat=31.60101, lon=130.55801), LatLon(lat=31.60202, lon=130.55902))
    candidate = make_candidate(geometry=geometry)
    origin = LatLon(lat=31.590000, lon=130.540000)  # ループの始点とは離れた起点

    html = html_of(map_view.build_map(origin=origin, candidate=candidate))

    assert html.count("L.polyline") == 1
    assert polyline_coordinates(html) == [[point.lat, point.lon] for point in geometry]


# --- 4. チェックポイントのマーカー（AC-04-1〜2・AC-04-4） -------------------


def test_build_map_places_a_marker_for_each_checkpoint() -> None:
    """チェックポイントの座標がマーカーとして描かれること。"""
    checkpoints = (
        make_checkpoint(order=1, lat=31.601, lon=130.558),
        make_checkpoint(order=2, lat=31.602, lon=130.559),
    )

    html = html_of(
        map_view.build_map(origin=LatLon(lat=31.6, lon=130.55), checkpoints=checkpoints)
    )

    assert circle_marker_coordinates(html) == [[31.601, 130.558], [31.602, 130.559]]


def test_build_map_without_checkpoints_has_no_checkpoint_marker() -> None:
    """チェックポイントを渡さなければ何も描かないこと（0件の周回もありうる。design.md 7.3）。"""
    html = html_of(map_view.build_map(origin=LatLon(lat=31.6, lon=130.55)))

    assert "L.circleMarker" not in html


def test_build_map_checkpoint_tooltip_states_the_direction_and_distance() -> None:
    """AC-04-2「起点からの距離と方向転換の向き」がチェックポイントの表示に含まれること。"""
    checkpoint = make_checkpoint(direction=TurnDirection.TURN_LEFT, distance_from_origin_m=1_234.0)

    html = html_of(
        map_view.build_map(origin=LatLon(lat=31.6, lon=130.55), checkpoints=(checkpoint,))
    )

    assert "左折" in html
    # AC-04-4 の標準形はキロメートル・小数第1位（1234m → 1.2km）
    assert "1.2" in html
    assert "1234" not in html


def test_build_map_checkpoint_tooltip_comes_from_messages() -> None:
    """吹き出しが `messages.checkpoint_line` の文言そのものであること（AC-04-2）。

    表記（向きの日本語・単位・桁）を地図側に持つと、AC-04-4 が定める内容が
    2か所に分かれる。**画面に文字列リテラルを書かない**規律（T12）は
    地図の吹き出しにも及ぶ。
    """
    checkpoint = make_checkpoint(
        order=2, distance_from_origin_m=2_345.0, direction=TurnDirection.SLIGHT_RIGHT
    )

    html = html_of(
        map_view.build_map(origin=LatLon(lat=31.6, lon=130.55), checkpoints=(checkpoint,))
    )

    assert messages.checkpoint_line(checkpoint) in html


def test_build_map_checkpoint_tooltip_includes_name_when_present() -> None:
    """AC-04-4「地点名は取得できた場合のみ併記する」。"""
    checkpoint = make_checkpoint(name="国道10号")

    html = html_of(
        map_view.build_map(origin=LatLon(lat=31.6, lon=130.55), checkpoints=(checkpoint,))
    )

    assert "国道10号" in html


def test_build_map_checkpoint_tooltip_omits_name_when_absent() -> None:
    """名前がないことは異常として扱わない（AC-04-4）。`None` を文字列として出さない。"""
    checkpoint = make_checkpoint(name=None)

    html = html_of(
        map_view.build_map(origin=LatLon(lat=31.6, lon=130.55), checkpoints=(checkpoint,))
    )

    assert "None" not in html


@pytest.mark.parametrize(
    ("direction", "expected_label"),
    [
        (TurnDirection.TURN_LEFT, "左折"),
        (TurnDirection.TURN_RIGHT, "右折"),
        (TurnDirection.SHARP_LEFT, "鋭角左折"),
        (TurnDirection.SHARP_RIGHT, "鋭角右折"),
        (TurnDirection.SLIGHT_LEFT, "緩い左折"),
        (TurnDirection.SLIGHT_RIGHT, "緩い右折"),
    ],
)
def test_build_map_translates_every_turn_direction(
    direction: TurnDirection, expected_label: str
) -> None:
    """6種の方向転換すべてに個別の日本語表記があること（AC-04-2）。"""
    checkpoint = make_checkpoint(direction=direction)

    html = html_of(
        map_view.build_map(origin=LatLon(lat=31.6, lon=130.55), checkpoints=(checkpoint,))
    )

    assert expected_label in html


def test_turn_direction_still_has_six_members() -> None:
    """`TurnDirection` がちょうど6件であること（T11 の完了条件）。

    増えたのに地図側の翻訳表を更新し忘れると、新しい向きが無表記で
    表示される。件数を固定して、増えた時点で対応表の更新を求める。
    """
    assert len(TurnDirection) == 6
