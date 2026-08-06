"""ors/mapper.py のテスト（design.md 1.2 / 7.1 / 10.2、AC-04-4）。

このファイルが固定するのは5つ。

1. **実データ**（T05 の fixture）から `ProviderRoute` が組めること。
   距離・ascent／descent・`snapped_start`・steps・3次元座標（design.md 10.2）
2. `name` の `"-"` が `None` に正規化され、**警告もログも出ない**こと（AC-04-4）
3. maneuver 種別が `Maneuver` に変換されること。
   **どれが方向転換かはここで決めない**（ホワイトリストは T11）
4. 想定外の JSON が `MalformedRoute` になること。素の `KeyError` を上げないこと
5. ORS 固有の知識（キー名・`"-"`・type 番号）が `ors/` の外に漏れていないこと

値の場合分けは fixture の**変種**で作る（design.md 10.3）。新しく API を叩けば
無料枠を消費し、実データの形は1件で足りるため。
"""

import ast
import copy
import json
import logging
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runloop.models import LatLon, Maneuver, ProviderRoute, RawStep
from runloop.ors import mapper
from runloop.ports import MalformedRoute

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "ors_round_trip_5km_points3.json"

# T05 で取得した実レスポンスの実測値（design.md 10.3 / FINDINGS スパイク6）
LOOP_M = 4_479.4
ASCENT_M = 58.7
COORDINATE_COUNT = 258
STEP_COUNT = 71
# ルート始点（= スナップ先）。GeoJSON は [経度, 緯度, 標高] の順で持つ
FIRST_LAT = 31.596359
FIRST_LON = 130.556926


@pytest.fixture(scope="module")
def payload() -> dict[str, Any]:
    """実レスポンス（points=3・instructions: true・elevation: true）。"""
    with FIXTURE.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = json.load(f)
    return loaded


@pytest.fixture
def variant(payload: dict[str, Any]) -> dict[str, Any]:
    """変種を作るための複製。module スコープの fixture を壊さないため。"""
    return copy.deepcopy(payload)


def feature(doc: dict[str, Any]) -> dict[str, Any]:
    """先頭の Feature を取り出す（変種を組むときの補助）。"""
    result: dict[str, Any] = doc["features"][0]
    return result


def steps_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """先頭 segment の steps を取り出す（変種を組むときの補助）。"""
    result: list[dict[str, Any]] = feature(doc)["properties"]["segments"][0]["steps"]
    return result


# --- 実データからの変換（design.md 10.2「200 の変換」） ------------------------


def test_maps_fixture_to_provider_route(payload: dict[str, Any]) -> None:
    """実レスポンスから `ProviderRoute` が組めること。

    値は T05 の実測（ループ 4479.4m、ascent／descent 58.7m、258 座標、71 step）。
    """
    route = mapper.to_provider_route(payload, seed=1)

    assert isinstance(route, ProviderRoute)
    assert route.loop_m == LOOP_M
    assert route.ascent_m == ASCENT_M
    assert route.descent_m == ASCENT_M
    assert len(route.geometry) == COORDINATE_COUNT
    assert len(route.steps) == STEP_COUNT


def test_seed_and_ratelimit_come_from_the_caller(payload: dict[str, Any]) -> None:
    """`seed` と残数を呼び出し側（T07 の client）から受け取ること。

    seed をレスポンスの `metadata` から読まないのは、**要求した seed が
    こちらの台帳の値**だからである。metadata の echo を信じる形にすると、
    「応答が要求を反映している」という未検証の仮定が1つ増える。
    残数は HTTP ヘッダにあり、本文しか見ないこの関数からは読めない。
    """
    route = mapper.to_provider_route(payload, seed=42, ratelimit_remaining=1_987)

    assert route.seed == 42
    assert route.ratelimit_remaining == 1_987
    assert mapper.to_provider_route(payload, seed=1).ratelimit_remaining is None


def test_coordinates_are_read_as_lon_lat_elevation(payload: dict[str, Any]) -> None:
    """GeoJSON の `[経度, 緯度, 標高]` を取り違えないこと（design.md 10.2）。

    緯度経度を逆に読んでも型は通り、地図には「アフリカ沖」が出るだけで
    例外にならない。**桁で判別できる値**（緯度 31 / 経度 130）で固定する。
    標高は `LatLon` が持たないので捨てる。
    """
    route = mapper.to_provider_route(payload, seed=1)
    first = route.geometry[0]

    assert first == LatLon(lat=FIRST_LAT, lon=FIRST_LON)
    assert route.geometry[-1] == LatLon(lat=FIRST_LAT, lon=FIRST_LON)
    assert all(30.0 < point.lat < 32.0 for point in route.geometry)
    assert all(130.0 < point.lon < 131.0 for point in route.geometry)


def test_snapped_start_is_the_first_coordinate(payload: dict[str, Any]) -> None:
    """`snapped_start` がルート始点（= スナップ先）であること。

    接近距離はこの点と起点の差として `generation.py` が算出する（design.md 2.2）。
    mapper は**起点を知らない**ので、ここで接近距離を持たせることはできない。
    """
    route = mapper.to_provider_route(payload, seed=1)

    assert route.snapped_start == route.geometry[0]
    assert route.snapped_start == LatLon(lat=FIRST_LAT, lon=FIRST_LON)


# --- steps（design.md 7.1、AC-04-4） ------------------------------------------


def test_step_carries_distance_maneuver_position_and_name(payload: dict[str, Any]) -> None:
    """各 step が T11 に必要な4つを持つこと（design.md 7.1）。

    座標は `way_points[0]` を geometry から引く（7.1 の手順4）。ここで解決するのは、
    `way_points` が ORS 固有のキーであり、添字の解決を `checkpoints.py` に
    持ち出すとその知識が `ors/` の外に出るため。
    """
    route = mapper.to_provider_route(payload, seed=1)
    second = route.steps[1]

    assert isinstance(second, RawStep)
    assert second.distance_m == 6.4
    assert second.maneuver is Maneuver.TURN_LEFT
    # way_points[0] == 4 → geometry[4]（[130.55741, 31.596052, 9.0]）
    assert second.position == LatLon(lat=31.596052, lon=130.55741)
    assert second.name is None


def test_step_distance_is_not_the_cumulative_distance(payload: dict[str, Any]) -> None:
    """`RawStep` が持つのは**その step 単体の距離**であること（design.md 7.1 / 2.3）。

    累積は `checkpoints.py` が積む（7.1 の手順2）。ここで累積に変えると、
    接近距離のオフセットを足す場所が1か所に定まらなくなる（2.3）。
    """
    route = mapper.to_provider_route(payload, seed=1)

    assert route.steps[0].distance_m == 58.4
    assert route.steps[1].distance_m == 6.4
    assert sum(step.distance_m for step in route.steps) == pytest.approx(LOOP_M, abs=1.0)


def test_dash_name_becomes_none(variant: dict[str, Any]) -> None:
    """`"-"` を `None` に正規化し、実名はそのまま残すこと（AC-04-4）。

    `"-"` は ORS の「名前なし」であり、そのまま出すと道の名前として読めてしまう。
    """
    steps_of(variant)[1]["name"] = "国道225号"
    steps_of(variant)[2]["name"] = None

    route = mapper.to_provider_route(variant, seed=1)

    assert route.steps[0].name is None  # 元は "-"
    assert route.steps[1].name == "国道225号"
    assert route.steps[2].name is None


def test_all_names_are_none_in_the_real_response(payload: dict[str, Any]) -> None:
    """実データでは 71/71 が `"-"` だったこと（T05 の実測。design.md 7.1）。

    スパイク1（points=5）の 95% ではなく **100%** である。「名前は取得できた
    場合のみ併記」（AC-04-4）は例外処理ではなく**通常の経路**で、この1本では
    名前が1つも出ない。T11 / T12 が「名前あり」を前提にしていないことの根拠。
    """
    route = mapper.to_provider_route(payload, seed=1)

    assert [step.name for step in route.steps] == [None] * STEP_COUNT


def test_dash_name_is_not_reported(
    payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """`"-"` で警告もログも出さないこと（AC-04-4「異常として扱わない」）。

    実データは 100% が `"-"` なので、1件ごとに記録すると 71 行のノイズになり、
    本当に見たいログ（残数・失敗の内訳）が埋もれる。
    """
    with caplog.at_level(logging.DEBUG), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mapper.to_provider_route(payload, seed=1)

    assert caplog.records == []
    assert list(caught) == []


# --- maneuver 種別（design.md 7.1、AC-04-4） ----------------------------------

# 実データの内訳（T05 の実測。FINDINGS スパイク6 の表と同じ）
OBSERVED_MANEUVERS = {
    Maneuver.TURN_LEFT: 27,
    Maneuver.TURN_RIGHT: 26,
    Maneuver.SHARP_LEFT: 3,
    Maneuver.SHARP_RIGHT: 1,
    Maneuver.SLIGHT_LEFT: 1,
    Maneuver.SLIGHT_RIGHT: 1,
    Maneuver.STRAIGHT: 2,
    Maneuver.KEEP_LEFT: 3,
    Maneuver.KEEP_RIGHT: 5,
    Maneuver.DEPART: 1,
    Maneuver.ARRIVE: 1,
}


def test_observed_maneuver_types_are_converted(payload: dict[str, Any]) -> None:
    """実データの 11 種類がすべて型に変換されること（design.md 7.1 の表）。

    合計が 71 であることも見る。取りこぼしが `UNKNOWN` に落ちていれば
    内訳が合わなくなる。
    """
    route = mapper.to_provider_route(payload, seed=1)

    counts = {maneuver: 0 for maneuver in OBSERVED_MANEUVERS}
    for step in route.steps:
        assert step.maneuver is not Maneuver.UNKNOWN
        counts[step.maneuver] += 1

    assert counts == OBSERVED_MANEUVERS
    assert sum(counts.values()) == STEP_COUNT


def test_type_13_is_keep_right(payload: dict[str, Any]) -> None:
    """type 13 が「右側維持」として変換されること（T05 で5件実測）。

    design.md 7.1 と FINDINGS は 13 を「不明」としているが、**同じ step の
    `instruction` が `"Keep right"` である**（type 12「Keep left」の対）。
    意味が読めるので `UNKNOWN` に潰さない。
    **ホワイトリスト（AC-04-4）の判断は変わらない。** 12 と同じく方向転換ではなく、
    どちらを方向転換とするかを決めるのは T11 である。
    """
    route = mapper.to_provider_route(payload, seed=1)
    keep_right = [step for step in route.steps if step.maneuver is Maneuver.KEEP_RIGHT]

    assert len(keep_right) == 5
    assert route.steps[38].maneuver is Maneuver.KEEP_RIGHT


@pytest.mark.parametrize("unknown_type", [7, 8, 9, 14, 99, -1])
def test_unobserved_maneuver_type_becomes_unknown_without_failing(
    variant: dict[str, Any], unknown_type: int
) -> None:
    """未観測の種別（ラウンドアバウト系ほか）で例外にしないこと（AC-04-4）。

    知らない番号が来ることは異常ではない。`UNKNOWN` として通し、
    **方向転換として扱わない側に倒すのは T11 のホワイトリスト**である。
    ここで例外にすると、1件の未知の step でコース全体が失われる。
    """
    steps_of(variant)[1]["type"] = unknown_type

    route = mapper.to_provider_route(variant, seed=1)

    assert route.steps[1].maneuver is Maneuver.UNKNOWN


def test_mapper_does_not_decide_which_maneuvers_are_turns() -> None:
    """「方向転換かどうか」を T06 で決めていないこと（AC-04-4 は T11 の責務）。

    ここに `is_turn` を置くと、ホワイトリストが `ors/`（ORS の都合を閉じる層）に
    入り、プロバイダを替えたときに要件由来の規則まで写すことになる。
    """
    assert not hasattr(Maneuver, "is_turn")
    assert not hasattr(mapper, "is_turn")
    assert not hasattr(mapper, "TURN_MANEUVERS")


# --- 壊れた JSON（design.md 10.2「壊れた JSON」） -----------------------------


def test_empty_steps_is_not_malformed(variant: dict[str, Any]) -> None:
    """steps が0件でも例外にしないこと（design.md 7.3「0件のときは何も出さない」）。

    周回の形によっては方向転換が出ないことがある。異常ではない。
    """
    feature(variant)["properties"]["segments"][0]["steps"] = []

    route = mapper.to_provider_route(variant, seed=1)

    assert route.steps == ()
    assert route.loop_m == LOOP_M


def test_multiple_segments_are_concatenated_in_order(variant: dict[str, Any]) -> None:
    """segment が複数でも steps を順に連結すること。

    起点1つの周回では 1 segment だが、連結しておけば「想定外だが読める」応答で
    step を落とさない。
    """
    segments = feature(variant)["properties"]["segments"]
    tail = copy.deepcopy(segments[0])
    tail["steps"] = tail["steps"][:2]
    segments.append(tail)

    route = mapper.to_provider_route(variant, seed=1)

    assert len(route.steps) == STEP_COUNT + 2
    assert route.steps[STEP_COUNT].distance_m == 58.4


def break_no_features(doc: dict[str, Any]) -> None:
    doc["features"] = []


def break_missing_geometry(doc: dict[str, Any]) -> None:
    del feature(doc)["geometry"]


def break_empty_coordinates(doc: dict[str, Any]) -> None:
    feature(doc)["geometry"]["coordinates"] = []


def break_flat_coordinate(doc: dict[str, Any]) -> None:
    feature(doc)["geometry"]["coordinates"][0] = [130.556926]


def break_non_numeric_coordinate(doc: dict[str, Any]) -> None:
    feature(doc)["geometry"]["coordinates"][0] = ["130.556926", "31.596359"]


def break_missing_summary(doc: dict[str, Any]) -> None:
    del feature(doc)["properties"]["summary"]


def break_missing_distance(doc: dict[str, Any]) -> None:
    del feature(doc)["properties"]["summary"]["distance"]


def break_non_numeric_distance(doc: dict[str, Any]) -> None:
    feature(doc)["properties"]["summary"]["distance"] = "4479.4"


def break_missing_ascent(doc: dict[str, Any]) -> None:
    del feature(doc)["properties"]["ascent"]


def break_missing_descent(doc: dict[str, Any]) -> None:
    del feature(doc)["properties"]["descent"]


def break_missing_segments(doc: dict[str, Any]) -> None:
    del feature(doc)["properties"]["segments"]


def break_missing_steps(doc: dict[str, Any]) -> None:
    del feature(doc)["properties"]["segments"][0]["steps"]


def break_step_missing_type(doc: dict[str, Any]) -> None:
    del steps_of(doc)[1]["type"]


def break_step_non_integer_type(doc: dict[str, Any]) -> None:
    steps_of(doc)[1]["type"] = "0"


def break_step_missing_distance(doc: dict[str, Any]) -> None:
    del steps_of(doc)[1]["distance"]


def break_step_missing_way_points(doc: dict[str, Any]) -> None:
    del steps_of(doc)[1]["way_points"]


def break_step_way_point_out_of_range(doc: dict[str, Any]) -> None:
    steps_of(doc)[1]["way_points"] = [COORDINATE_COUNT, COORDINATE_COUNT]


def break_step_negative_way_point(doc: dict[str, Any]) -> None:
    """負の添字は Python では末尾から数えられてしまう。**静かに別の座標になる。**"""
    steps_of(doc)[1]["way_points"] = [-1, 2]


def break_step_non_string_name(doc: dict[str, Any]) -> None:
    steps_of(doc)[1]["name"] = 225


BREAKAGES: list[Callable[[dict[str, Any]], None]] = [
    break_no_features,
    break_missing_geometry,
    break_empty_coordinates,
    break_flat_coordinate,
    break_non_numeric_coordinate,
    break_missing_summary,
    break_missing_distance,
    break_non_numeric_distance,
    break_missing_ascent,
    break_missing_descent,
    break_missing_segments,
    break_missing_steps,
    break_step_missing_type,
    break_step_non_integer_type,
    break_step_missing_distance,
    break_step_missing_way_points,
    break_step_way_point_out_of_range,
    break_step_negative_way_point,
    break_step_non_string_name,
]


@pytest.mark.parametrize("breakage", BREAKAGES, ids=lambda f: f.__name__)
def test_broken_response_raises_malformed_route(
    variant: dict[str, Any], breakage: Callable[[dict[str, Any]], None]
) -> None:
    """想定外の形が `MalformedRoute` になること（design.md 3.2 / 10.2）。

    **素の `KeyError` / `TypeError` / `IndexError` を上げない。** 上位（T08 / T18b）が
    捕まえる語彙は `RouteProviderError` の6種だけであり、Python の組み込み例外が
    混ざると「プロバイダ由来の失敗」を1か所で捕まえる経路（AC-06-4）が破れる。
    """
    breakage(variant)

    with pytest.raises(MalformedRoute):
        mapper.to_provider_route(variant, seed=1)


@pytest.mark.parametrize(
    "payload_value",
    [None, "", [], 42, {}, {"type": "FeatureCollection"}],
    ids=["none", "empty_string", "list", "int", "empty_dict", "no_features_key"],
)
def test_non_route_payload_raises_malformed_route(payload_value: object) -> None:
    """そもそもルートでない応答も `MalformedRoute` にすること。

    ORS はエラー時に `{"error": {...}}` を返すことがある（`code: 2009` など）。
    ステータスの翻訳は T07 の責務だが、本文だけが渡ってもここで落ちない。
    """
    with pytest.raises(MalformedRoute):
        mapper.to_provider_route(payload_value, seed=1)


def message_of(doc: dict[str, Any]) -> str:
    """変換に失敗したときのメッセージを取り出す（診断の質を見るための補助）。"""
    with pytest.raises(MalformedRoute) as excinfo:
        mapper.to_provider_route(doc, seed=1)
    return str(excinfo.value)


def test_message_distinguishes_a_missing_key_from_a_wrong_type(
    payload: dict[str, Any],
) -> None:
    """キーの欠落と型違いで、違うことを言うこと。

    200 が返っているのに読めないとき、**手がかりはこのメッセージだけ**である
    （応答の本文を丸ごとログに出すと座標が漏れる。design.md 8.7）。
    「ascent が数値ではない」と報告されたのに実は `ascent` が無かった、では
    次に何を見ればよいか分からない。

    キーの有無を確かめずに読むと、欠落が下流の型検査で「型違い」として
    現れる。**結果はどちらも `MalformedRoute` なので、この区別はメッセージ
    でしか壊れたことに気づけない。**
    """
    missing = copy.deepcopy(payload)
    del feature(missing)["properties"]["ascent"]
    wrong_type = copy.deepcopy(payload)
    feature(wrong_type)["properties"]["ascent"] = "58.7"

    missing_message = message_of(missing)
    wrong_type_message = message_of(wrong_type)

    assert "ascent" in missing_message
    assert "ascent" in wrong_type_message
    assert missing_message != wrong_type_message


def test_message_names_the_container_not_its_first_entry(variant: dict[str, Any]) -> None:
    """配列の場所に文字列が来たとき、壊れているのは**その配列**だと言うこと。

    Python では文字列も「文字の並び」なので、配列として素通しすると
    `features[0]` が先頭の1文字になり、「features[0] が辞書ではない」という
    **見当違いの報告**になる。壊れているのは `features` そのものである。
    """
    variant["features"] = "FeatureCollection"

    message = message_of(variant)

    assert "features" in message
    assert "配列" in message
    assert "features[0]" not in message


def test_malformed_route_message_has_no_coordinates(variant: dict[str, Any]) -> None:
    """例外メッセージに座標を入れないこと（design.md 8.7 プライバシー）。

    起点の座標は自宅である。例外はログにも画面にも流れうるので、
    メッセージは「どこが壊れていたか」だけにする。
    """
    break_missing_summary(variant)

    with pytest.raises(MalformedRoute) as excinfo:
        mapper.to_provider_route(variant, seed=1)

    message = str(excinfo.value)
    assert "31.59" not in message
    assert "130.55" not in message
    assert "summary" in message


# --- 層の規律（design.md 1.2「ORS 固有の知識を ors/ の中だけに閉じる」） ------

# ORS のレスポンス形式に固有の語彙。`ors/` の外に文字列として現れてはいけない
ORS_RESPONSE_VOCABULARY = frozenset(
    {
        "features",
        "segments",
        "steps",
        "way_points",
        "summary",
        "ascent",
        "descent",
        "coordinates",
        "instruction",
        "-",
    }
)


def module_string_constants(path: Path) -> set[str]:
    """モジュール中の文字列定数を集める（docstring は除く）。

    ソースの文字列検索では docstring の説明文に反応する（`models.py` の
    `ProviderRoute` の docstring には「steps」という語がある）。構文木で
    docstring を外せば、説明文と実際のキー参照を区別できる（T04 の申し送り）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_ors_response_vocabulary_stays_inside_ors_package() -> None:
    """ORS のキー名と `"-"` が `runloop/ors/` の外に無いこと（design.md 1.2）。

    レスポンス形式の知識が漏れると、プロバイダを足すときに写す範囲が
    追えなくなる（非機能要件・保守性、ADR-0001）。
    """
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "runloop").rglob("*.py")):
        if path.parent.name == "ors":
            continue
        leaked = module_string_constants(path) & ORS_RESPONSE_VOCABULARY
        offenders += [f"{path.name}: {value!r}" for value in sorted(leaked)]

    assert offenders == [], f"ORS 固有の語彙が ors/ の外にある: {', '.join(offenders)}"


def test_maneuver_does_not_carry_ors_type_numbers() -> None:
    """`Maneuver` が ORS の type 番号を値に持たないこと（design.md 1.2）。

    番号と種別の対応は ORS 固有の知識で、`ors/mapper.py` の対応表にだけ置く。
    `Maneuver.TURN_LEFT = 0` にすると、番号がドメインの型に焼き付き、
    別サービス（番号体系が違う）を足すときに `models.py` を触ることになる。
    """
    values = [member.value for member in Maneuver]

    assert all(isinstance(value, str) for value in values)
    assert not any(value.lstrip("-").isdigit() for value in values)
    assert len(set(values)) == len(values)
