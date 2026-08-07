"""checkpoints.py のテスト（design.md 7.1 / 7.2 / 7.3、requirements.md AC-04-1〜4）。

このファイルが固定するのは4つ。

1. 抽出（AC-04-4）——ホワイトリスト（type 0〜5 相当の6種）に無い maneuver は
   `Turn` にならないこと。`"-"` の正規化は T06（`ors/mapper.py`）が既に済ませて
   いるので、ここでは `RawStep.name` をそのまま引き継ぐだけでよい
2. 起点からの距離のオフセット（AC-04-2）——`approach_m + cumulative_loop_m` を
   `Checkpoint` 生成時にだけ足すこと（`Turn` は生の累積距離のまま）
3. 間引き（AC-04-3）——6件以上のとき、ループを6等分した目標距離への
   最近傍で5件を選び、同じ Turn を2回選ばないこと
4. 件数（AC-04-1）——5件以下ならすべて出し、0件でも例外にしない

実データ（T05 の fixture、points=3）を1件使い、方向転換 59 件（design.md 7.3）
という実測値に対する回帰も固定する。
"""

import inspect
import json
from pathlib import Path

import pytest

from runloop import checkpoints
from runloop.models import Candidate, Checkpoint, LatLon, Maneuver, RawStep, Turn, TurnDirection
from runloop.ors import mapper

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "ors_round_trip_5km_points3.json"

# design.md 7.3（2026-08-06、スパイク6）。71 step のうち方向転換は 59 件
REAL_FIXTURE_STEP_COUNT = 71
REAL_FIXTURE_TURN_COUNT = 59


def make_step(
    *,
    maneuver: Maneuver = Maneuver.TURN_LEFT,
    distance_m: float = 100.0,
    lat: float = 31.6,
    lon: float = 130.55,
    name: str | None = None,
) -> RawStep:
    """テスト用の RawStep を組む。"""
    return RawStep(
        distance_m=distance_m,
        maneuver=maneuver,
        position=LatLon(lat=lat, lon=lon),
        name=name,
    )


def make_turn(
    *,
    direction: TurnDirection = TurnDirection.TURN_LEFT,
    cumulative_loop_m: float,
    name: str | None = None,
) -> Turn:
    """テスト用の Turn を組む。座標は着目しないので固定値でよい。"""
    return Turn(
        direction=direction,
        cumulative_loop_m=cumulative_loop_m,
        position=LatLon(lat=31.6, lon=130.55),
        name=name,
    )


# --- 1. 抽出（AC-04-4、design.md 7.1） --------------------------------------


def test_extract_turns_keeps_the_six_whitelisted_maneuvers() -> None:
    """type 0〜5 相当の6種はすべて Turn になること。"""
    steps = tuple(
        make_step(maneuver=m)
        for m in (
            Maneuver.TURN_LEFT,
            Maneuver.TURN_RIGHT,
            Maneuver.SHARP_LEFT,
            Maneuver.SHARP_RIGHT,
            Maneuver.SLIGHT_LEFT,
            Maneuver.SLIGHT_RIGHT,
        )
    )

    turns = checkpoints.extract_turns(steps)

    assert len(turns) == 6


@pytest.mark.parametrize(
    "maneuver",
    [
        Maneuver.STRAIGHT,
        Maneuver.KEEP_LEFT,
        Maneuver.KEEP_RIGHT,  # 実測 type 13。「右側維持」であって不明ではない（design.md 7.1）
        Maneuver.DEPART,
        Maneuver.ARRIVE,
        Maneuver.UNKNOWN,  # 未観測の種別が来た場合の代表
    ],
)
def test_extract_turns_drops_non_turn_maneuvers(maneuver: Maneuver) -> None:
    """AC-04-4「知らない種別は方向転換として扱わない側に倒す」。

    `KEEP_LEFT` / `KEEP_RIGHT` は分岐でどちらの車線に留まるかの案内であり、
    進行方向は変わらない（design.md 7.1）。方向転換として数えると、
    曲がらない地点を「曲がる」と案内することになる。
    """
    steps = (make_step(maneuver=maneuver),)

    assert checkpoints.extract_turns(steps) == ()


def test_extract_turns_handles_empty_steps() -> None:
    """0件でも例外にせず空を返す。"""
    assert checkpoints.extract_turns(()) == ()


def test_extract_turns_computes_cumulative_distance_as_the_steps_start() -> None:
    """`cumulative_loop_m` はその step の**開始点**（それ以前の step の距離の合計）。

    2件目の Turn の累積は1件目の distance_m と一致し、自分自身の distance_m を
    含まないこと（design.md 7.1「それまでの step の distance の合計」）。
    """
    steps = (
        make_step(maneuver=Maneuver.STRAIGHT, distance_m=200.0),
        make_step(maneuver=Maneuver.TURN_LEFT, distance_m=300.0),
        make_step(maneuver=Maneuver.TURN_RIGHT, distance_m=50.0),
    )

    turns = checkpoints.extract_turns(steps)

    assert [t.cumulative_loop_m for t in turns] == [200.0, 500.0]


def test_extract_turns_accumulates_distance_from_non_turn_steps_too() -> None:
    """方向転換ではない step の距離も、後続の Turn の累積距離には効くこと。

    ホワイトリストで除外するのは Turn への**変換**だけであり、
    距離の積み上げから除外してはならない。
    """
    steps = (
        make_step(maneuver=Maneuver.DEPART, distance_m=50.0),
        make_step(maneuver=Maneuver.STRAIGHT, distance_m=150.0),
        make_step(maneuver=Maneuver.TURN_LEFT, distance_m=80.0),
    )

    turns = checkpoints.extract_turns(steps)

    assert turns[0].cumulative_loop_m == 200.0


def test_extract_turns_preserves_position_and_name() -> None:
    """座標と地点名を Turn にそのまま引き継ぐこと（地図マーカー用。design.md 7.1）。"""
    step = make_step(maneuver=Maneuver.TURN_LEFT, lat=31.601, lon=130.558, name="国道10号")

    turns = checkpoints.extract_turns((step,))

    assert turns[0].position == LatLon(lat=31.601, lon=130.558)
    assert turns[0].name == "国道10号"


def test_extract_turns_keeps_missing_name_as_none() -> None:
    """名前がないことは異常として扱わない（AC-04-4）。`None` のまま、文字列にしない。"""
    step = make_step(maneuver=Maneuver.TURN_LEFT, name=None)

    turns = checkpoints.extract_turns((step,))

    assert turns[0].name is None


def test_extract_turns_has_no_approach_parameter() -> None:
    """接近距離のオフセットを足す場所は `Checkpoint` 生成時の1か所に限定する（design.md 7.2）。

    `extract_turns` が `approach_m` を受け取れる形だと、二重加算・加算漏れの
    余地が生まれる。型（引数）でその場所を1か所に固定する。
    """
    assert "approach_m" not in inspect.signature(checkpoints.extract_turns).parameters


# --- 2. 実データ（T05 fixture）による回帰確認（design.md 7.3） --------------


@pytest.fixture(scope="module")
def real_steps() -> tuple[RawStep, ...]:
    """T05 の実データ（points=3・instructions: true）から steps を取り出す。"""
    with FIXTURE.open(encoding="utf-8") as f:
        payload = json.load(f)
    route = mapper.to_provider_route(payload, seed=1)
    return route.steps


def test_real_fixture_has_the_expected_step_count(real_steps: tuple[RawStep, ...]) -> None:
    """fixture 自体の前提（71 step）が変わっていないこと。"""
    assert len(real_steps) == REAL_FIXTURE_STEP_COUNT


def test_extract_turns_matches_the_real_fixture_count(real_steps: tuple[RawStep, ...]) -> None:
    """実データ（71 step）から方向転換 59 件（design.md 7.3、2026-08-06 スパイク6）。

    type 13（Keep right）が5件含まれるが、方向転換には数えない。
    """
    assert len(checkpoints.extract_turns(real_steps)) == REAL_FIXTURE_TURN_COUNT


# --- 3. 起点からの距離のオフセット（AC-04-2、design.md 7.2） -----------------


def test_select_checkpoints_adds_the_approach_offset_once() -> None:
    """`distance_from_origin_m = approach_m + cumulative_loop_m`（design.md 7.2）。"""
    turns = (make_turn(cumulative_loop_m=1_200.0),)

    result = checkpoints.select_checkpoints(turns, approach_m=80.0, loop_m=5_000.0)

    assert result[0].distance_from_origin_m == 1_280.0


def test_select_checkpoints_with_zero_approach_keeps_the_raw_cumulative() -> None:
    """接近距離 0 の起点では、生の累積距離と一致すること（オフセットが0のときの対照）。"""
    turns = (make_turn(cumulative_loop_m=700.0),)

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=5_000.0)

    assert result[0].distance_from_origin_m == 700.0


def test_select_checkpoints_preserves_direction_and_name() -> None:
    """向きと地点名が `Checkpoint` にそのまま引き継がれること（AC-04-2 / AC-04-4）。"""
    turns = (
        make_turn(direction=TurnDirection.SHARP_RIGHT, cumulative_loop_m=300.0, name="桜島通り"),
    )

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=5_000.0)

    assert result[0].direction is TurnDirection.SHARP_RIGHT
    assert result[0].name == "桜島通り"


def test_select_checkpoints_keeps_missing_name_as_none() -> None:
    """名前がない Turn から作った Checkpoint も `None` のまま（文字列にしない）。"""
    turns = (make_turn(cumulative_loop_m=300.0, name=None),)

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=5_000.0)

    assert result[0].name is None


# --- 4. 件数（AC-04-1） ------------------------------------------------------


def test_select_checkpoints_returns_nothing_for_zero_turns() -> None:
    """0件でも例外にせず空を返す（周回の形によっては起こりうる。design.md 7.3）。"""
    assert checkpoints.select_checkpoints((), approach_m=0.0, loop_m=5_000.0) == ()


def test_select_checkpoints_returns_all_when_five_or_fewer() -> None:
    """AC-04-1「最大5件」。5件以下ならすべて出す（間引かない）。"""
    turns = tuple(make_turn(cumulative_loop_m=float(i * 900)) for i in range(1, 6))  # 5件

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=5_000.0)

    assert len(result) == 5


def test_select_checkpoints_never_exceeds_five() -> None:
    """AC-04-3「6か所以上のとき5件に間引かれる」。ちょうど6件でも5件になる。"""
    turns = tuple(make_turn(cumulative_loop_m=float(i * 800)) for i in range(1, 7))  # 6件

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=5_000.0)

    assert len(result) == 5


def test_select_checkpoints_with_many_turns_still_returns_five() -> None:
    """実測相当（59件・76件）の本数でも5件に収まること。"""
    turns = tuple(make_turn(cumulative_loop_m=float(i)) for i in range(76))

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=5_000.0)

    assert len(result) == 5


# --- 5. 間引き（AC-04-3、design.md 7.3） ------------------------------------


def test_thinning_picks_the_nearest_turn_to_each_equal_target() -> None:
    """ループを6等分した目標距離それぞれに最も近い Turn を選ぶこと。

    目標（loop_m=6000, approach_m=0）: 1000, 2000, 3000, 4000, 5000。
    ぴったりの位置に Turn を置き、選ばれる5件がそれと一致することを固定する。
    目標から離れた位置に埋め草（decoy）を置き、間引きが働いていることも確認する。
    """
    exact_targets = [1_000.0, 2_000.0, 3_000.0, 4_000.0, 5_000.0]
    decoys = [500.0, 1_500.0, 2_500.0, 3_500.0, 4_500.0, 5_500.0]
    turns = tuple(make_turn(cumulative_loop_m=m) for m in exact_targets + decoys)

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=6_000.0)

    assert sorted(cp.distance_from_origin_m for cp in result) == exact_targets


def test_thinning_targets_account_for_a_nonzero_approach_offset() -> None:
    """間引きの目標距離も `approach_m + loop_m * i / 6` であること（design.md 7.3）。

    上のテストは `approach_m=0.0` なので、目標距離の式からオフセットを
    落とす実装ミスがあっても区別できない。接近距離 500m の起点を置き、
    起点からの距離が 1500/2500/3500/4500/5500 になる Turn が選ばれることを固定する。
    """
    exact_targets = [1_500.0, 2_500.0, 3_500.0, 4_500.0, 5_500.0]
    # cumulative_loop_m は起点からの距離ではなくルート始点からの距離なので、
    # 500.0（approach_m）を引いた位置に置く
    exact_cumulatives = [1_000.0, 2_000.0, 3_000.0, 4_000.0, 5_000.0]
    decoy_cumulatives = [0.0, 1_500.0, 2_500.0, 3_500.0, 4_500.0, 5_500.0]
    turns = tuple(make_turn(cumulative_loop_m=m) for m in exact_cumulatives + decoy_cumulatives)

    result = checkpoints.select_checkpoints(turns, approach_m=500.0, loop_m=6_000.0)

    assert sorted(cp.distance_from_origin_m for cp in result) == exact_targets


def test_thinning_does_not_pick_the_same_turn_twice() -> None:
    """すでに選ばれた Turn は次の目標には選ばない（design.md 7.3 手順3）。

    目標は 1000/2000/3000/4000/5000（loop_m=6000）。1000 と 4000 付近に
    Turn を寄せて、複数の目標が同じ1件に引き寄せられる状況を作る。
    素朴な実装（毎回全体から最近傍を選ぶだけで既選出を除かない）だと
    1000 と 4000 系のどれかが2回選ばれ、選ばれる Turn が5件未満になる。
    """
    positions = (1_000.0, 4_000.0, 4_001.0, 4_002.0, 4_003.0, 4_004.0)
    turns = tuple(make_turn(cumulative_loop_m=m) for m in positions)

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=6_000.0)

    assert len(result) == 5
    assert len({cp.distance_from_origin_m for cp in result}) == 5


# --- 6. 並び順（design.md 7.3 手順4） ---------------------------------------


def test_select_checkpoints_orders_by_distance_ascending() -> None:
    """起点からの距離の昇順に並び、`order` は 1 から振られること。"""
    turns = tuple(make_turn(cumulative_loop_m=m) for m in (300.0, 100.0, 200.0))

    result = checkpoints.select_checkpoints(turns, approach_m=0.0, loop_m=5_000.0)

    assert [cp.distance_from_origin_m for cp in result] == [100.0, 200.0, 300.0]
    assert [cp.order for cp in result] == [1, 2, 3]


# --- 7. 型の分離（design.md 2.3。オフセットを足す場所を1か所に限定する） ----


def test_turn_has_no_distance_from_origin_field() -> None:
    """`Turn` は起点からの距離を持たない（design.md 2.3）。

    持たせると、`extract_turns` の側でもオフセットを足せてしまい、
    「足す場所は `Checkpoint` 生成時の1か所だけ」という制約が型で守れなくなる。
    """
    turn = make_turn(cumulative_loop_m=0.0)

    assert not hasattr(turn, "distance_from_origin_m")


def test_checkpoint_has_no_cumulative_loop_field() -> None:
    """`Checkpoint` はルート始点からの生の累積距離を持たない（design.md 2.3）。"""
    checkpoint = Checkpoint(
        order=1,
        distance_from_origin_m=0.0,
        direction=TurnDirection.TURN_LEFT,
        name=None,
        position=LatLon(lat=0.0, lon=0.0),
    )

    assert not hasattr(checkpoint, "cumulative_loop_m")


# --- 8. Candidate.turns（design.md 2.1 の Candidate 表、T02 の申し送り） -----


def test_candidate_can_hold_turns() -> None:
    """`Candidate.turns` に `Turn` の列を持たせられること（design.md 2.1）。"""
    turn = make_turn(cumulative_loop_m=100.0)
    candidate = Candidate(
        seed=1,
        loop_m=5_000.0,
        approach_m=0.0,
        ascent_m=50.0,
        descent_m=50.0,
        target_m=5_000,
        geometry=(LatLon(lat=31.6, lon=130.55),),
        turns=(turn,),
    )

    assert candidate.turns == (turn,)


def test_candidate_turns_defaults_to_empty() -> None:
    """`turns` を渡さなければ空タプル（既存の呼び出し側を壊さない。T02〜T10 との互換）。"""
    candidate = Candidate(
        seed=1,
        loop_m=5_000.0,
        approach_m=0.0,
        ascent_m=50.0,
        descent_m=50.0,
        target_m=5_000,
        geometry=(LatLon(lat=31.6, lon=130.55),),
    )

    assert candidate.turns == ()
