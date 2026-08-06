"""selection.py のテスト（design.md 5.1 / 5.2、10.1 の selection の6行）。

このファイルが固定するのは**順序**である。requirements.md 6節が
「距離を先に、標高を後に」と定めており、逆順にすると
「坂がないだけの外れた距離」が選ばれる（design.md 5.1）。
順序は結果の値からは見えない——**標高が最小の候補が選ばれなかったこと**を
見て初めて固定できるので、そのためのテストを中心に置く。

design.md 5.1 の5手順に対応する節に分けてある。

1. 接近ゲート（REJECT なら1本も出さない）
2. 異常値除外（415km を在庫にも妥協パスにも出さない）
3. 除外後0件 → NO_CANDIDATE
4. 在庫と並び順（獲得標高 昇順 → |距離誤差| 昇順 → seed 昇順）
5. 在庫0件 → 誤差最小で COMPROMISED、在庫は空のまま
"""

import ast
import dataclasses
from pathlib import Path

import pytest

from runloop.models import (
    ApproachVerdict,
    Candidate,
    GenerationOutcome,
    LatLon,
    SelectionOutcome,
)
from runloop.selection import select

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def make_candidate(
    *,
    seed: int = 1,
    loop_m: float,
    ascent_m: float = 50.0,
    approach_m: float = 0.0,
    target_m: int = 5_000,
) -> Candidate:
    """テスト用の候補を組む。各テストは着目する値だけを渡す。

    合計距離・距離誤差は `Candidate` の計算プロパティなので、ここでは渡さない
    （design.md 2.2。判定値と表示値を1か所から出す）。
    """
    return Candidate(
        seed=seed,
        loop_m=loop_m,
        approach_m=approach_m,
        ascent_m=ascent_m,
        descent_m=ascent_m,
        target_m=target_m,
        geometry=(LatLon(lat=31.5966, lon=130.5571),),
    )


def make_outcome(
    *candidates: Candidate,
    verdict: ApproachVerdict | None = ApproachVerdict.OK,
    approach_m: float | None = 0.7,
) -> GenerationOutcome:
    """生成の結果を組む。既定は「起点が道路の上で、全候補が測れた」状態。

    `failures` / `calls_consumed` / `aborted_early` / `cache_diverged` は
    選択の判断材料ではない（design.md 5.1 の入力は候補・接近距離・verdict のみ）。
    """
    return GenerationOutcome(
        candidates=candidates,
        approach_m=approach_m,
        verdict=verdict,
        failures={},
        calls_consumed=15,
        aborted_early=False,
        cache_diverged=False,
    )


# --- 手順1: 接近ゲート（AC-01-3、design.md 5.1 / 5.2） ----------------------


def test_reject_shows_no_course_at_all() -> None:
    """AC-01-3「300m 超 → 結果を表示せず拒否する」。

    候補が在庫の条件を満たしていても1本も返さない。**表示するかどうかの判断は
    選択側にある**（design.md 5.2）ので、生成が候補を持ってきていても結論は変わらない。
    """
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=5_000.0, ascent_m=30.0),
            make_candidate(seed=2, loop_m=4_900.0, ascent_m=40.0),
            verdict=ApproachVerdict.REJECT,
            approach_m=420.0,
        )
    )

    assert result.outcome is SelectionOutcome.ORIGIN_REJECTED
    assert result.chosen is None
    assert result.stock == ()


def test_reject_is_decided_before_exclusion() -> None:
    """ゲートが手順2より先にあること（design.md 5.1 の順序）。

    起点が悪く、かつ候補がすべて異常値だったとき、伝えるべきは
    「起点が道路から離れている」（次にすべき操作がある）であって
    「候補が0件」ではない。除外を先に置くと NO_CANDIDATE に落ちる。
    """
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=416_451.3),
            verdict=ApproachVerdict.REJECT,
            approach_m=420.0,
        )
    )

    assert result.outcome is SelectionOutcome.ORIGIN_REJECTED
    assert result.degenerate_count == 0


def test_warn_is_not_rejected() -> None:
    """拒否するのは REJECT だけ（AC-01-3 の表。50〜300m は表示する）。"""
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=5_000.0),
            verdict=ApproachVerdict.WARN,
            approach_m=120.0,
        )
    )

    assert result.outcome is SelectionOutcome.IN_TOLERANCE
    assert result.chosen is not None


def test_unmeasured_approach_is_not_a_rejection() -> None:
    """接近距離が1本も測れなかった実行は NO_CANDIDATE（AC-06-3）。

    `verdict is None` は「観測できていない」であって「起点が遠い」ではない
    （design.md 4.4 / models.GenerationOutcome）。拒否として扱うと、
    画面が「最寄りの道路上をクリックしてください」と誤った次の行動を指す。
    """
    result = select(make_outcome(verdict=None, approach_m=None))

    assert result.outcome is SelectionOutcome.NO_CANDIDATE
    assert result.chosen is None

    # 対照。候補が0件でもゲートが先にあるので、REJECT なら結論は変わる
    rejected = select(make_outcome(verdict=ApproachVerdict.REJECT, approach_m=420.0))

    assert rejected.outcome is SelectionOutcome.ORIGIN_REJECTED


# --- 手順2: 異常値除外（AC-01-5、design.md 5.1） ----------------------------


def test_degenerate_candidate_is_not_in_stock() -> None:
    """AC-01-5「合計距離が目標の3倍を超える候補は異常値として除外する」。

    実測値 416,451.3m（FINDINGS スパイク2）。標高は 0m にしてある——
    **獲得標高で並べれば先頭に来る**ので、除外せずに並べると選ばれてしまう。
    """
    degenerate = make_candidate(seed=1, loop_m=416_451.3, ascent_m=0.0)
    normal = make_candidate(seed=2, loop_m=5_000.0, ascent_m=60.0)

    result = select(make_outcome(degenerate, normal))

    assert result.chosen is normal
    assert result.stock == (normal,)
    assert result.degenerate_count == 1


def test_degenerate_candidate_is_not_shown_in_the_compromise_path() -> None:
    """AC-01-5「除外した候補は AC-01-4 の妥協パスでも表示しない」。

    妥協パスは「誤差最小」で選ぶ。415km を除外し忘れても誤差は最大なので
    普通は選ばれないが、**在庫0件で残りが1本もなければ選ばれてしまう。**
    ここでは誤差 500m の候補を1本だけ置き、そちらが出ることを見る。
    """
    degenerate = make_candidate(seed=1, loop_m=414_672.8, ascent_m=0.0)
    off_target = make_candidate(seed=2, loop_m=5_500.0, ascent_m=90.0)

    result = select(make_outcome(degenerate, off_target))

    assert result.outcome is SelectionOutcome.COMPROMISED
    assert result.chosen is off_target
    assert result.stock == ()
    assert result.degenerate_count == 1


def test_degenerate_count_counts_every_excluded_candidate() -> None:
    """除外した件数が残ること（design.md 5.1 手順2）。実測は 2/23 件。"""
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=416_451.3),
            make_candidate(seed=2, loop_m=414_672.8),
            make_candidate(seed=3, loop_m=5_000.0),
        )
    )

    assert result.degenerate_count == 2


def test_exactly_three_times_target_is_not_excluded() -> None:
    """境界値: 合計距離ちょうど3倍は異常値にしない（design.md 5.1 の表）。

    AC-01-5 は「3倍を**超える**」。判定の定義元は `Candidate.is_degenerate` で、
    selection がここで別の不等号を書くと定義が2か所に分かれる。
    """
    exactly_triple = make_candidate(seed=1, loop_m=15_000.0, target_m=5_000)

    result = select(make_outcome(exactly_triple))

    assert result.degenerate_count == 0
    assert result.outcome is SelectionOutcome.COMPROMISED
    assert result.chosen is exactly_triple


# --- 手順3: 除外後0件（AC-06-3、design.md 5.1） -----------------------------


def test_no_candidates_at_all_is_no_candidate() -> None:
    """AC-06-3「候補が1件も取得できなかった場合、その旨のメッセージ」。"""
    result = select(make_outcome())

    assert result.outcome is SelectionOutcome.NO_CANDIDATE
    assert result.chosen is None
    assert result.stock == ()


def test_all_degenerate_is_no_candidate() -> None:
    """AC-06-3「異常値除外の結果として0件になった場合も、この扱いとする」。

    妥協パス（COMPROMISED）に落とさない。出せる候補が無いのだから
    「条件を満たすコースがなかった」ではなく「候補が無い」である。
    """
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=416_451.3),
            make_candidate(seed=2, loop_m=414_672.8),
        )
    )

    assert result.outcome is SelectionOutcome.NO_CANDIDATE
    assert result.chosen is None
    assert result.stock == ()
    assert result.degenerate_count == 2


# --- 手順4: 在庫と並び順（AC-01-2 / AC-03-2 / AC-08-1、design.md 5.1） -------


def test_lowest_ascent_within_tolerance_is_chosen() -> None:
    """AC-03-2「±300m を満たす候補が2件以上あるとき、獲得標高が最小の候補」。"""
    flat = make_candidate(seed=1, loop_m=5_100.0, ascent_m=20.0)
    hilly = make_candidate(seed=2, loop_m=5_000.0, ascent_m=80.0)

    result = select(make_outcome(hilly, flat))

    assert result.outcome is SelectionOutcome.IN_TOLERANCE
    assert result.chosen is flat


def test_flattest_candidate_outside_tolerance_is_not_chosen() -> None:
    """**このファイルの中心。** 距離を先に、標高を後に（requirements.md 6節）。

    坂が最も少ない候補（標高 5m）は距離が 900m 外れている。順序を逆にした実装
    ——標高で選んでから距離を見る——では、この候補が選ばれる。
    CLAUDE.md が「実装で最も外しやすい」と名指ししている点。
    """
    flat_but_off = make_candidate(seed=1, loop_m=5_900.0, ascent_m=5.0)
    hilly_but_close = make_candidate(seed=2, loop_m=5_050.0, ascent_m=95.0)

    result = select(make_outcome(flat_but_off, hilly_but_close))

    assert result.outcome is SelectionOutcome.IN_TOLERANCE
    assert result.chosen is hilly_but_close
    assert flat_but_off not in result.stock


def test_stock_holds_every_in_tolerance_candidate_sorted_by_ascent() -> None:
    """在庫は ±300m を満たす候補すべてで、獲得標高の昇順（design.md 2.4）。

    在庫を1本だけにすると引き直し（AC-08-1）が成立しない。
    並び順を選択側で確定させ、引き直し側では並べ替えない。
    """
    high = make_candidate(seed=1, loop_m=5_000.0, ascent_m=90.0)
    low = make_candidate(seed=2, loop_m=5_100.0, ascent_m=10.0)
    mid = make_candidate(seed=3, loop_m=4_900.0, ascent_m=50.0)
    far = make_candidate(seed=4, loop_m=6_000.0, ascent_m=1.0)

    result = select(make_outcome(high, low, mid, far))

    assert result.stock == (low, mid, high)


def test_chosen_is_the_head_of_the_stock() -> None:
    """初回表示と引き直しが同じ列を使うこと（AC-08-1「選択規準は初回表示と同一」）。

    別々に選ぶと、引き直し1回目で初回と同じコースが出る経路ができる。
    """
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=5_000.0, ascent_m=70.0),
            make_candidate(seed=2, loop_m=5_200.0, ascent_m=30.0),
        )
    )

    assert result.chosen is result.stock[0]


def test_error_exactly_300_is_in_stock() -> None:
    """境界値: 誤差ちょうど 300.0m は在庫に入れる（design.md 5.1 の表）。

    「±300m 以内」は境界を含む。判定の定義元は
    `Candidate.is_within_tolerance` で、selection は不等号を書き直さない。
    """
    on_boundary = make_candidate(seed=1, loop_m=5_300.0)

    result = select(make_outcome(on_boundary))

    assert result.outcome is SelectionOutcome.IN_TOLERANCE
    assert result.stock == (on_boundary,)


def test_approach_distance_counts_toward_the_tolerance() -> None:
    """在庫の判定に**合計距離**（ループ + 接近 × 2）を使うこと（AC-01-2）。

    ループ距離だけで判定すると、接近 200m の候補が在庫に入る。
    """
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=5_000.0, approach_m=200.0),
            verdict=ApproachVerdict.WARN,
            approach_m=200.0,
        )
    )

    assert result.outcome is SelectionOutcome.COMPROMISED
    assert result.stock == ()


def test_order_breaks_ascent_ties_by_absolute_error() -> None:
    """AC-08-1「ランダムではない」。標高が同値なら |距離誤差| 昇順（design.md 5.1）。

    第2キーに距離誤差を置くのは、距離が中核価値だから。
    """
    far = make_candidate(seed=1, loop_m=5_250.0, ascent_m=40.0)
    near = make_candidate(seed=2, loop_m=5_050.0, ascent_m=40.0)

    result = select(make_outcome(far, near))

    assert result.stock == (near, far)


def test_order_breaks_remaining_ties_by_seed() -> None:
    """標高も誤差も同値なら seed 昇順。順序が完全に決まること（AC-08-1）。

    誤差は符号違いで絶対値が等しい2本を置く。第2キーまでで決まらない場合が
    実際にありうることを示す形にしてある。
    """
    later = make_candidate(seed=9, loop_m=5_100.0, ascent_m=40.0)
    earlier = make_candidate(seed=4, loop_m=4_900.0, ascent_m=40.0)

    result = select(make_outcome(later, earlier))

    assert result.stock == (earlier, later)


def test_order_does_not_depend_on_the_input_order() -> None:
    """入力の並びが結果に影響しないこと（AC-08-1）。

    生成は並列なので、候補の到着順は実行ごとに変わる。到着順が残る実装
    （安定ソートの第2・第3キーを省いたもの）では、同じ起点・同じ距離でも
    引き直しの結果が実行ごとに変わる。
    """
    candidates = [
        make_candidate(seed=3, loop_m=5_100.0, ascent_m=40.0),
        make_candidate(seed=1, loop_m=4_900.0, ascent_m=40.0),
        make_candidate(seed=2, loop_m=5_000.0, ascent_m=40.0),
    ]

    forward = select(make_outcome(*candidates))
    backward = select(make_outcome(*reversed(candidates)))

    # 期待する並びも書く。両者が「同じ」だけでは、どちらも空でも通ってしまう
    assert [c.seed for c in forward.stock] == [2, 1, 3]
    assert [c.seed for c in backward.stock] == [2, 1, 3]


# --- 手順5: 妥協パス（AC-01-4、design.md 5.1） ------------------------------


def test_compromise_picks_the_smallest_absolute_error() -> None:
    """AC-01-4「±300m 以内の候補が得られなかった場合、距離誤差が最小の候補」。

    標高は最小の候補を外してある。妥協パスの基準は**距離**であって標高ではない。
    """
    closest = make_candidate(seed=1, loop_m=5_400.0, ascent_m=99.0)
    flattest = make_candidate(seed=2, loop_m=6_000.0, ascent_m=1.0)

    result = select(make_outcome(flattest, closest))

    assert result.outcome is SelectionOutcome.COMPROMISED
    assert result.chosen is closest


def test_compromise_ignores_the_sign_of_the_error() -> None:
    """誤差は絶対値で比べる（AC-01-4）。不足も超過も同じ扱い。"""
    short_by_500 = make_candidate(seed=1, loop_m=4_500.0)
    long_by_350 = make_candidate(seed=2, loop_m=5_350.0)

    result = select(make_outcome(short_by_500, long_by_350))

    assert result.chosen is long_by_350


def test_compromise_leaves_the_stock_empty() -> None:
    """AC-08-4「在庫が尽きたときに ±300m 未満の候補で埋めることはしない」。

    在庫は定義上 ±300m を通過した候補だけなので、妥協した1本は在庫に入らない。
    結果として引き直しは即「候補がない」（AC-08-3）になる（design.md 6.2）。
    """
    result = select(
        make_outcome(
            make_candidate(seed=1, loop_m=5_400.0),
            make_candidate(seed=2, loop_m=5_500.0),
        )
    )

    assert result.outcome is SelectionOutcome.COMPROMISED
    assert result.chosen is not None
    assert result.stock == ()


def test_compromise_order_is_deterministic() -> None:
    """妥協パスも決定的であること（AC-08-1）。

    誤差が同値の2本（符号違い）で、標高 → seed の順に決まる。
    `min()` は先に見た方を返すので、キーを持たない実装では到着順で変わる。
    """
    hilly = make_candidate(seed=1, loop_m=5_400.0, ascent_m=80.0)
    flat = make_candidate(seed=2, loop_m=4_600.0, ascent_m=20.0)

    forward = select(make_outcome(hilly, flat))
    backward = select(make_outcome(flat, hilly))

    assert forward.chosen is flat
    assert backward.chosen is flat


# --- 純関数であること（design.md 1.2「外部依存ゼロの純関数」） ---------------


def test_selection_depends_only_on_models() -> None:
    """`selection.py` が models 以外を import していないこと（design.md 1.2 / 1.3）。

    ここは本アプリの中核価値であり、外部依存が入るとテストが遅く不安定になる。
    `ports` / `ors` / `generation` を参照しないことも含めて固定する
    （プロバイダの都合が選択規準に混ざらない）。
    """
    tree = ast.parse(
        (PROJECT_ROOT / "runloop" / "selection.py").read_text(encoding="utf-8"),
    )
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    external = {name for name in imported if name.split(".")[0] not in {"runloop"}}
    internal = {name for name in imported if name.split(".")[0] == "runloop"}

    assert external == set(), f"外部依存が入っている: {sorted(external)}"
    assert internal <= {"runloop.models"}, f"models 以外に依存している: {sorted(internal)}"


def test_selection_does_not_mutate_the_input() -> None:
    """入力の候補列を並べ替えないこと（design.md 2.1 の不変方針）。

    `list.sort()` を入力に対して行うと、呼び出し側が持つ順序が静かに変わる。
    """
    candidates = (
        make_candidate(seed=1, loop_m=5_000.0, ascent_m=90.0),
        make_candidate(seed=2, loop_m=5_100.0, ascent_m=10.0),
    )
    outcome = make_outcome(*candidates)

    select(outcome)

    assert outcome.candidates == candidates


def test_selection_result_is_frozen() -> None:
    """選択の結果も不変（design.md 2.1）。在庫を持ち回っても壊れない。"""
    result = select(make_outcome(make_candidate(seed=1, loop_m=5_000.0)))

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.chosen = None  # type: ignore[misc]


def test_stock_is_a_tuple() -> None:
    """在庫が tuple であること（生成後は不変。design.md 6.1）。"""
    result = select(make_outcome(make_candidate(seed=1, loop_m=5_000.0)))

    assert isinstance(result.stock, tuple)
