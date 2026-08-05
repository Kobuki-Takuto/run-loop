"""models.py のテスト（design.md 2.1 / 2.2 / 5.1、10.1 の1行目）。

このファイルが固定するのは3つ。

1. 合計距離が「ループ距離 + 接近距離 × 2」であり、判定値と表示値が
   同一の計算経路から出ること（design.md 2.2）
2. design.md 5.1 の境界値4件（誤差 300.0 は在庫／ちょうど3倍は異常値でない／
   接近 50.0 は OK／接近 300.0 は WARN）
3. 丸めを一切していないこと。表示の丸めは messages.py の責務（T12）
"""

import dataclasses

import pytest

from runloop.models import (
    APPROACH_OK_M,
    APPROACH_REJECT_M,
    DEGENERATE_FACTOR,
    TOLERANCE_M,
    ApproachVerdict,
    Candidate,
    LatLon,
    RouteQuery,
    classify_approach,
)


def make_candidate(
    *,
    loop_m: float,
    approach_m: float,
    target_m: int = 5_000,
    ascent_m: float = 50.0,
    seed: int = 1,
) -> Candidate:
    """テスト用の候補を組む。各テストは着目する値だけを渡す。"""
    return Candidate(
        seed=seed,
        loop_m=loop_m,
        approach_m=approach_m,
        ascent_m=ascent_m,
        descent_m=ascent_m,
        target_m=target_m,
        geometry=(LatLon(lat=31.5966, lon=130.5571),),
    )


# --- 要件由来の定数（T04 の config.py がここを参照する） ---------------------


def test_constants_match_requirements() -> None:
    """定数が要件の数値そのものであること。

    models.py に置くのは、design.md 1.3 が「models.py は何にも依存しない」と
    定めており、Candidate のプロパティから config.py を import できないため。
    """
    assert TOLERANCE_M == 300.0
    assert DEGENERATE_FACTOR == 3
    assert APPROACH_OK_M == 50.0
    assert APPROACH_REJECT_M == 300.0


# --- LatLon と RouteQuery ---------------------------------------------------


def test_latlon_is_frozen() -> None:
    """座標は生成後に書き換えない（design.md 2.1）。"""
    point = LatLon(lat=31.5966, lon=130.5571)

    with pytest.raises(dataclasses.FrozenInstanceError):
        point.lat = 0.0  # type: ignore[misc]


def test_latlon_is_not_rounded() -> None:
    """座標を丸めない。AC-05-1「自宅の玄関を正確に起点にしたい」。"""
    point = LatLon(lat=31.59661234567, lon=130.55712345678)

    assert point.lat == 31.59661234567
    assert point.lon == 130.55712345678


def test_route_query_defaults() -> None:
    """既定値は points=3 / avoid_steps=True（design.md 2.1、AC-03-1）。"""
    query = RouteQuery(origin=LatLon(lat=31.5966, lon=130.5571), target_m=5_000)

    assert query.points == 3
    assert query.avoid_steps is True


def test_route_query_is_frozen() -> None:
    """実行条件も不変。在庫の鍵に使うため（design.md 6.2）。"""
    query = RouteQuery(origin=LatLon(lat=31.5966, lon=130.5571), target_m=5_000)

    with pytest.raises(dataclasses.FrozenInstanceError):
        query.target_m = 3_000  # type: ignore[misc]


# --- Candidate の計算プロパティ（design.md 2.2） -----------------------------


def test_total_m_adds_approach_twice() -> None:
    """合計距離 = ループ距離 + 接近距離 × 2（AC-01-2）。往復するので2回足す。"""
    candidate = make_candidate(loop_m=4_800.0, approach_m=100.0)

    assert candidate.total_m == 5_000.0


def test_total_m_is_computed_not_stored() -> None:
    """合計距離がフィールドではなくプロパティであること（design.md 2.2）。

    フィールドだと生成時に一度計算した値が固定され、
    判定と表示が別経路を通る余地が残る。
    """
    field_names = {field.name for field in dataclasses.fields(Candidate)}

    assert "total_m" not in field_names
    assert "error_m" not in field_names


def test_error_m_is_negative_when_short() -> None:
    """合計距離が目標より短いとき、誤差は負（AC-02-2 の符号。AC-02-3 の分岐）。"""
    candidate = make_candidate(loop_m=4_600.0, approach_m=100.0)

    assert candidate.total_m == 4_800.0
    assert candidate.error_m == -200.0
    assert candidate.abs_error_m == 200.0


def test_error_m_is_positive_when_long() -> None:
    """合計距離が目標より長いとき、誤差は正（AC-02-2 の符号。AC-02-4 の分岐）。"""
    candidate = make_candidate(loop_m=5_000.0, approach_m=200.0)

    assert candidate.total_m == 5_400.0
    assert candidate.error_m == 400.0
    assert candidate.abs_error_m == 400.0


def test_target_m_is_baked_into_candidate() -> None:
    """目標距離を候補が持つこと（design.md 2.2）。

    外から渡す形にすると、呼び出し側が別の目標距離を渡す余地が残る。
    """
    candidate = make_candidate(loop_m=3_000.0, approach_m=0.0, target_m=3_000)

    assert candidate.target_m == 3_000
    assert candidate.error_m == 0.0


def test_candidate_is_frozen() -> None:
    """候補は生成後に書き換えない。在庫を並べ替えても壊れない（design.md 2.1）。"""
    candidate = make_candidate(loop_m=4_800.0, approach_m=100.0)

    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.loop_m = 0.0  # type: ignore[misc]


# --- 境界値1: 誤差ちょうど 300.0m は在庫に入れる（design.md 5.1） -------------


def test_error_exactly_300_is_within_tolerance() -> None:
    """AC-01-2 は「±300m 以内」。以内は境界を含む（design.md 5.1）。"""
    candidate = make_candidate(loop_m=5_300.0, approach_m=0.0)

    assert candidate.abs_error_m == 300.0
    assert candidate.is_within_tolerance is True


def test_error_exactly_minus_300_is_within_tolerance() -> None:
    """負側の境界も同じ扱い。絶対値で判定する。"""
    candidate = make_candidate(loop_m=4_700.0, approach_m=0.0)

    assert candidate.error_m == -300.0
    assert candidate.is_within_tolerance is True


def test_error_just_over_300_is_out_of_tolerance() -> None:
    """境界を1cm 超えたら在庫に入らない。"""
    candidate = make_candidate(loop_m=5_300.01, approach_m=0.0)

    assert candidate.is_within_tolerance is False


def test_tolerance_does_not_round() -> None:
    """判定前に丸めていないこと（design.md 2.2）。

    誤差 300.4m は round すると 300 になり、丸める実装だと在庫に入ってしまう。
    表示の丸め（AC-02-1 の小数第2位）は messages.py の責務。
    """
    candidate = make_candidate(loop_m=5_300.4, approach_m=0.0)

    assert candidate.abs_error_m == pytest.approx(300.4)
    assert candidate.is_within_tolerance is False


def test_total_m_keeps_full_precision() -> None:
    """合計距離を丸めない。接近距離 0.7m（実測値）が消えないこと。"""
    candidate = make_candidate(loop_m=6_177.4, approach_m=0.7)

    assert candidate.total_m == pytest.approx(6_178.8, abs=1e-9)
    assert candidate.total_m != 6_178.0


# --- 境界値2: 合計距離ちょうど3倍は異常値にしない（design.md 5.1） -----------


def test_exactly_three_times_target_is_not_degenerate() -> None:
    """AC-01-5 は「3倍を**超える**」。ちょうど3倍は超えていない（design.md 5.1）。"""
    candidate = make_candidate(loop_m=15_000.0, approach_m=0.0, target_m=5_000)

    assert candidate.total_m == 15_000.0
    assert candidate.is_degenerate is False


def test_just_over_three_times_target_is_degenerate() -> None:
    """3倍をわずかに超えたら異常値（AC-01-5）。"""
    candidate = make_candidate(loop_m=15_000.01, approach_m=0.0, target_m=5_000)

    assert candidate.is_degenerate is True


def test_measured_degenerate_route_is_degenerate() -> None:
    """実測の異常値 416,451.3m が 5km 目標で異常値になること。

    FINDINGS スパイク2 で観測した値。AC-01-5 が守るのはこの型の候補。
    """
    candidate = make_candidate(loop_m=416_451.3, approach_m=0.7, target_m=5_000)

    assert candidate.is_degenerate is True
    assert candidate.is_within_tolerance is False


def test_normal_candidate_is_not_degenerate() -> None:
    """在庫に入る候補が異常値扱いされないこと。"""
    candidate = make_candidate(loop_m=4_800.0, approach_m=100.0)

    assert candidate.is_degenerate is False
    assert candidate.is_within_tolerance is True


# --- 境界値3・4: 接近距離の分類（design.md 5.1 / 4.1） ----------------------


def test_approach_zero_is_ok() -> None:
    """道路上の起点は OK。"""
    assert classify_approach(0.0) is ApproachVerdict.OK


def test_approach_exactly_50_is_ok() -> None:
    """AC-01-3 の表は「〜50m」で境界を下段に含む。WARN ではなく OK（design.md 5.1）。"""
    assert classify_approach(50.0) is ApproachVerdict.OK


def test_approach_just_over_50_is_warn() -> None:
    """50m を超えたら WARN。AC-02-5 の「接近距離を表示する」条件。"""
    assert classify_approach(50.01) is ApproachVerdict.WARN


def test_approach_exactly_300_is_warn() -> None:
    """AC-01-3 で拒否するのは「300m 超」。ちょうど 300.0 は WARN（design.md 5.1）。"""
    assert classify_approach(300.0) is ApproachVerdict.WARN


def test_approach_just_over_300_is_reject() -> None:
    """300m を超えたら REJECT。ここで打ち切る（design.md 4.1 の接近ゲート）。"""
    assert classify_approach(300.01) is ApproachVerdict.REJECT


def test_approach_measured_value_is_ok() -> None:
    """実測の 0.7m（自宅座標。FINDINGS スパイク3）が OK であること。"""
    assert classify_approach(0.7) is ApproachVerdict.OK


def test_approach_verdict_does_not_round() -> None:
    """分類前に丸めていないこと。300.4m は round すると 300 になる。"""
    assert classify_approach(300.4) is ApproachVerdict.REJECT
