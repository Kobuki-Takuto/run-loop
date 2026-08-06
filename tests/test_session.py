"""session.py のテスト（design.md 6.1 / 6.2 / 6.3、10.1 の session の3行）。

このファイルが固定するのは**遷移**である。US-08 の引き直しは Streamlit の
再実行の上で動くが、遷移そのものは純データ操作なので Streamlit なしで書ける
（design.md 1.2 / 6.1）。

design.md 6.2 の遷移表に対応する節に分けてある。

1. 実行（探す）— 在庫を作り `cursor = 0`
2. 引き直し — `cursor + 1` が在庫内なら進める。API は呼ばない（AC-08-1 / AC-08-2）
3. 引き直し（在庫の末尾）— カーソルを動かさず、尽きたことを伝える（AC-08-3）
4. 妥協パス — 在庫は空で、引き直しは即「候補がない」（AC-08-4）
5. 起点または目標距離が変わった — 在庫を破棄する（design.md 6.2）
6. 状態が1つのオブジェクトに収まっていること（design.md 6.1）
"""

import ast
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runloop.models import (
    ApproachVerdict,
    Candidate,
    GenerationOutcome,
    LatLon,
    RouteQuery,
    SelectionOutcome,
    SelectionResult,
)
from runloop.selection import select
from runloop.session import RerollResult, RunSession, is_stale, reroll, start

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ORIGIN = LatLon(lat=31.5966, lon=130.5571)
QUERY = RouteQuery(origin=ORIGIN, target_m=5_000)
# 生成時刻はログとデバッグ用（design.md 6.1）。テストからは固定値を渡す
GENERATED_AT = datetime(2026, 8, 6, 7, 30, tzinfo=UTC)


def make_candidate(
    *,
    seed: int,
    loop_m: float,
    ascent_m: float = 50.0,
    approach_m: float = 0.7,
    target_m: int = 5_000,
) -> Candidate:
    """テスト用の候補を組む。各テストは着目する値だけを渡す。

    合計距離・距離誤差は `Candidate` の計算プロパティなので渡さない（design.md 2.2）。
    """
    return Candidate(
        seed=seed,
        loop_m=loop_m,
        approach_m=approach_m,
        ascent_m=ascent_m,
        descent_m=ascent_m,
        target_m=target_m,
        geometry=(ORIGIN,),
    )


def make_generation(
    *candidates: Candidate,
    approach_m: float | None = 0.7,
    verdict: ApproachVerdict | None = ApproachVerdict.OK,
) -> GenerationOutcome:
    """生成の結果を組む。既定は「起点が道路の上で、全候補が測れた」状態。"""
    return GenerationOutcome(
        candidates=candidates,
        approach_m=approach_m,
        verdict=verdict,
        failures={},
        calls_consumed=15,
        aborted_early=False,
        cache_diverged=False,
    )


def make_session(
    *,
    stock: tuple[Candidate, ...],
    chosen: Candidate | None = None,
    outcome: SelectionOutcome = SelectionOutcome.IN_TOLERANCE,
    query: RouteQuery = QUERY,
    approach_m: float | None = 0.7,
) -> RunSession:
    """在庫を直接指定してセッションを作る。

    `select()` を通さず `SelectionResult` を直接組むのは、**在庫の並びを
    セッション側が並べ替えないこと**を見るテストがあるためである。選択の順序は
    T10 の責務で、ここが固定するのは「受け取った並びのまま出す」ことだけ。
    """
    selection = SelectionResult(
        chosen=chosen if chosen is not None else (stock[0] if stock else None),
        stock=stock,
        outcome=outcome,
        degenerate_count=0,
    )
    return start(
        query,
        make_generation(*stock, approach_m=approach_m),
        selection,
        generated_at=GENERATED_AT,
    )


# --- 1. 実行（探す）（design.md 6.2、AC-01-1） -------------------------------


def test_start_shows_the_chosen_course_first() -> None:
    """実行直後は選択された1本が出ている（`cursor = 0`。design.md 6.2）。

    `chosen` は在庫の先頭と同一オブジェクトである（T10）ので、
    初回表示は在庫の先頭と一致する。
    """
    flat = make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0)
    hilly = make_candidate(seed=2, loop_m=5_100.0, ascent_m=90.0)

    session = make_session(stock=(flat, hilly))

    assert session.cursor == 0
    assert session.current is flat


def test_start_keeps_the_order_selection_decided() -> None:
    """AC-08-1「選択規準は初回表示と同一であり、ランダムではない」。

    並び順を確定させるのは `selection.py` だけで、セッション側では並べ替えない
    （design.md 5.1 / 6.1「在庫は生成後は不変」）。ここで並べ替えると
    順序の定義元が2か所に分かれる。
    """
    stock = (
        make_candidate(seed=7, loop_m=5_000.0, ascent_m=10.0),
        make_candidate(seed=3, loop_m=4_800.0, ascent_m=20.0),
        make_candidate(seed=9, loop_m=5_200.0, ascent_m=30.0),
    )

    session = make_session(stock=stock)

    assert session.stock == stock
    assert [c.seed for c in session.stock] == [7, 3, 9]


def test_the_first_course_is_the_one_selection_chose() -> None:
    """本物の `select()` と繋いだときも初回表示が一致すること（AC-08-1）。

    在庫を直接組むテストだけだと、`start()` が `SelectionResult` のどの値を
    使うかを取り違えていても気づけない（`chosen` ではなく `candidates` の
    先頭を見る、など）。
    """
    outcome = make_generation(
        make_candidate(seed=1, loop_m=5_100.0, ascent_m=90.0),
        make_candidate(seed=2, loop_m=4_950.0, ascent_m=10.0),
        make_candidate(seed=3, loop_m=5_050.0, ascent_m=50.0),
    )
    result = select(outcome)

    session = start(QUERY, outcome, result, generated_at=GENERATED_AT)

    assert session.current is result.chosen
    assert session.stock == result.stock
    assert session.outcome is SelectionOutcome.IN_TOLERANCE


# --- 2. 引き直し（AC-08-1 / AC-08-2、design.md 6.2） -------------------------


def test_reroll_moves_to_the_next_stock_entry() -> None:
    """AC-08-1「引き直しを操作すると、次に良い候補が表示される」。"""
    first = make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0)
    second = make_candidate(seed=2, loop_m=5_100.0, ascent_m=20.0)

    result = reroll(make_session(stock=(first, second)))

    assert result.advanced is True
    assert result.session.cursor == 1
    assert result.session.current is second


def test_reroll_walks_the_whole_stock_in_order() -> None:
    """在庫を先頭から順に1本ずつ出し切ること（AC-08-1）。

    1回だけの検査では「2本目は出るが3本目で止まる」実装が通る。
    また、上限のない実装（カーソルが在庫を越えて進む）はここで時間切れではなく
    `for-else` で落ちる。
    """
    stock = (
        make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0),
        make_candidate(seed=2, loop_m=5_050.0, ascent_m=20.0),
        make_candidate(seed=3, loop_m=5_100.0, ascent_m=30.0),
    )
    session = make_session(stock=stock)

    seen = [session.current]
    for _ in range(len(stock) + 2):
        result = reroll(session)
        if not result.advanced:
            break
        session = result.session
        seen.append(session.current)
    else:
        pytest.fail("引き直しが止まらない（カーソルが在庫の末尾を越えて進んでいる）")

    assert seen == list(stock)


def test_reroll_hands_back_the_candidates_it_already_had() -> None:
    """AC-08-2「引き直しでは外部 API を呼び出さない」。

    出てくるのは在庫に入っている**その候補そのもの**（同一オブジェクト）である。
    作り直した同値の候補ではないことまで見る。
    """
    first = make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0)
    second = make_candidate(seed=2, loop_m=5_100.0, ascent_m=20.0)
    session = make_session(stock=(first, second))

    result = reroll(session)

    assert result.session.current is second
    assert result.session.stock is session.stock


def test_reroll_does_not_change_the_previous_session() -> None:
    """遷移は差し替えであって書き換えではない（design.md 6.1）。

    Streamlit は操作ごとに再実行されるので、古いセッションを参照している
    経路が残る。書き換える実装だと、そこから見える状態が静かに変わる。
    """
    first = make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0)
    second = make_candidate(seed=2, loop_m=5_100.0, ascent_m=20.0)
    session = make_session(stock=(first, second))

    reroll(session)

    assert session.cursor == 0
    assert session.current is first


def test_reroll_carries_the_run_facts_forward() -> None:
    """引き直しで変わるのはカーソルだけ（design.md 6.2 の遷移表）。

    接近距離は AC-02-5「接近距離が 50m を超える場合、その値を画面に表示し、
    ずれの向きも併せて伝える」の材料である。引き直しで落ちると**2本目から
    警告が黙って消える**——コースを替えても起点は同じなのに。
    実行の事実（条件・接近距離・結論・生成時刻）はカーソルと一緒に動かない。
    """
    stock = (
        make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0),
        make_candidate(seed=2, loop_m=5_100.0, ascent_m=20.0),
    )
    session = make_session(stock=stock, approach_m=120.0)

    moved = reroll(session).session

    assert moved.approach_m == 120.0
    assert moved.query == QUERY
    assert moved.outcome is SelectionOutcome.IN_TOLERANCE
    assert moved.generated_at == GENERATED_AT


def test_session_touches_no_provider() -> None:
    """AC-08-2 を構造で担保する。`session.py` が外部に触れないこと。

    引き直しで API を呼ばないことは、呼ぶ手段を持たないことで固定する
    （`ports` / `ors` / `generation` / `requests` を import しない。design.md 1.3）。
    """
    tree = ast.parse((PROJECT_ROOT / "runloop" / "session.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    external = {name for name in imported if name.split(".")[0] != "runloop"}
    internal = {name for name in imported if name.split(".")[0] == "runloop"}

    assert internal <= {"runloop.models"}, f"models 以外に依存している: {sorted(internal)}"
    assert external <= {"dataclasses", "datetime", "typing"}, (
        f"標準ライブラリ以外が入っている: {sorted(external)}"
    )


def test_there_is_no_way_back() -> None:
    """「前の候補に戻る」を実装しないこと（design.md 6.2「カーソルは前進のみ」）。

    要件にない操作を足すと、AC-08-3 の「尽きた」判定が
    「どちら向きに尽きたか」に分かれる。公開する操作を3つに固定する。
    """
    tree = ast.parse((PROJECT_ROOT / "runloop" / "session.py").read_text(encoding="utf-8"))

    module_level = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef) and not node.name.startswith("_")
    }
    assert module_level == {"RunSession", "RerollResult", "start", "reroll", "is_stale"}, (
        f"公開している名前が想定と違う: {sorted(module_level)}"
    )

    session_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RunSession"
    )
    methods = {
        node.name
        for node in session_class.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    assert methods <= {"current", "stock", "outcome"}, (
        f"RunSession に読み出し以外の操作がある: {sorted(methods)}"
    )


# --- 3. 在庫の末尾（AC-08-3、design.md 6.2 / 6.3） ---------------------------


def test_reroll_at_the_end_keeps_the_cursor() -> None:
    """AC-08-3 の前半「末尾ではカーソルを動かさない」（design.md 6.2）。"""
    stock = (
        make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0),
        make_candidate(seed=2, loop_m=5_100.0, ascent_m=20.0),
    )
    at_end = reroll(make_session(stock=stock)).session

    result = reroll(at_end)

    assert result.session is at_end
    assert result.session.cursor == 1


def test_exhaustion_is_told_not_swallowed() -> None:
    """AC-08-3「尽きたことを黙って同じコースを再表示しない」。

    カーソルが動かないだけの実装だと、画面には同じコースが出て何も起きなかった
    ように見える。**進めたかどうかを呼び出し側が受け取る**ことを固定する。
    成功側と対にして見るのは、常に `False` を返す実装を落とすため。
    """
    stock = (
        make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0),
        make_candidate(seed=2, loop_m=5_100.0, ascent_m=20.0),
    )
    session = make_session(stock=stock)

    first = reroll(session)
    second = reroll(first.session)

    assert first.advanced is True
    assert second.advanced is False
    assert second.session.current is first.session.current


def test_a_stock_of_one_is_exhausted_on_the_first_reroll() -> None:
    """在庫1本で引き直しが1回もできない場合（design.md 6.3）。

    悲観側の推計では**5回に1回**この状態になる。例外処理ではなく通常の経路。
    """
    only = make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0)

    result = reroll(make_session(stock=(only,)))

    assert result.advanced is False
    assert result.session.current is only


# --- 4. 妥協パスと候補なし（AC-08-4 / AC-01-4 / AC-06-3、design.md 6.2） -----


def test_compromised_session_shows_the_course_with_an_empty_stock() -> None:
    """AC-01-4 の1本は表示し、在庫は空のまま（AC-08-4、design.md 6.2）。

    在庫は定義上 ±300m を通過した候補だけなので、誤差最小の1本は在庫に入らない。
    それでも画面には出す（条件未達である旨と併せて。AC-01-4）。
    """
    compromise = make_candidate(seed=1, loop_m=5_400.0)

    session = make_session(
        stock=(),
        chosen=compromise,
        outcome=SelectionOutcome.COMPROMISED,
    )

    assert session.stock == ()
    assert session.current is compromise
    assert session.outcome is SelectionOutcome.COMPROMISED


def test_compromised_reroll_does_not_fill_from_anywhere() -> None:
    """AC-08-4「在庫が尽きたときに ±300m 未満の候補で埋めることはしない」。

    妥協パスでは引き直しが即「候補がない」（AC-08-3）になる。
    生成された候補の残りから補充する経路を作らない。
    """
    compromise = make_candidate(seed=1, loop_m=5_400.0)
    session = make_session(
        stock=(),
        chosen=compromise,
        outcome=SelectionOutcome.COMPROMISED,
    )

    result = reroll(session)

    assert result.advanced is False
    assert result.session.current is compromise
    assert result.session.stock == ()


def test_a_session_without_any_candidate_shows_nothing() -> None:
    """AC-06-3 / AC-01-3 の結論を持つセッション（design.md 5.2）。

    表示する1本がない状態でも引き直しで落ちない。**結論そのものは持ち回る**——
    「候補がない」と「起点が悪い」で次にすべき操作が違う。
    """
    for outcome in (SelectionOutcome.NO_CANDIDATE, SelectionOutcome.ORIGIN_REJECTED):
        session = make_session(stock=(), outcome=outcome, approach_m=None)

        result = reroll(session)

        assert session.current is None
        assert session.outcome is outcome
        assert result.advanced is False


# --- 5. 在庫の鍵（design.md 6.2） -------------------------------------------


def test_the_same_query_keeps_the_stock() -> None:
    """条件が変わっていなければ在庫は使える（design.md 6.2）。

    毎回破棄すると、引き直しのたびに API を呼ぶことになり AC-08-2 が壊れる。
    """
    session = make_session(stock=(make_candidate(seed=1, loop_m=5_000.0),))

    assert is_stale(session, RouteQuery(origin=ORIGIN, target_m=5_000)) is False
    # 対照。これがないと「常に False を返す（在庫を一度も破棄しない）」実装で通る
    assert is_stale(session, RouteQuery(origin=ORIGIN, target_m=3_000)) is True


def test_changing_the_target_distance_discards_the_stock() -> None:
    """AC-08-4 を守るための破棄（design.md 6.2）。

    目標 5km の在庫は 3km の ±300m を満たさない。条件が変わった在庫から出すと
    「引き直しでも ±300m」が壊れる。
    """
    session = make_session(stock=(make_candidate(seed=1, loop_m=5_000.0),))

    assert is_stale(session, RouteQuery(origin=ORIGIN, target_m=3_000)) is True


def test_changing_the_origin_discards_the_stock() -> None:
    """起点が変われば在庫は別物（design.md 6.2）。

    目標距離だけを比べる実装だと、地図を別の場所にクリックしても前の起点の
    コースが出続ける。**目標距離を同じにして**その取り違えを落とす。
    """
    session = make_session(stock=(make_candidate(seed=1, loop_m=5_000.0),))
    elsewhere = RouteQuery(origin=LatLon(lat=31.6000, lon=130.5600), target_m=5_000)

    assert is_stale(session, elsewhere) is True


def test_changing_the_route_conditions_discards_the_stock() -> None:
    """在庫の鍵は `RouteQuery` 全体である（design.md 6.2）。

    `avoid_steps` を外した在庫は AC-03-1（階段を含まない）の前提が違う。
    起点と目標距離だけを比べる実装だと、条件を変えても古い在庫が残る。
    """
    session = make_session(stock=(make_candidate(seed=1, loop_m=5_000.0),))
    without_avoid = RouteQuery(origin=ORIGIN, target_m=5_000, avoid_steps=False)

    assert is_stale(session, without_avoid) is True


def test_a_reused_stock_stays_stale_after_rerolling() -> None:
    """引き直しで進めても鍵は変わらない（design.md 6.2）。

    カーソルだけを差し替える実装で `query` を落とすと、2本目以降で
    在庫の有効性が判定できなくなる。
    """
    stock = (
        make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0),
        make_candidate(seed=2, loop_m=5_100.0, ascent_m=20.0),
    )
    advanced = reroll(make_session(stock=stock)).session

    assert advanced.query == QUERY
    assert is_stale(advanced, RouteQuery(origin=ORIGIN, target_m=3_000)) is True


# --- 6. 純データであること（design.md 6.1） ---------------------------------


def test_design_6_1_state_is_readable_from_one_object() -> None:
    """design.md 6.1 の6項目が1つのオブジェクトから読めること。

    状態を複数のキーに分けると片方だけ更新される事故が起きる。Streamlit 層は
    `st.session_state["run"]` にこれを1つ置くだけにする。
    """
    stock = (make_candidate(seed=1, loop_m=5_000.0, ascent_m=10.0),)

    session = make_session(stock=stock, approach_m=12.5)

    assert session.query == QUERY
    assert session.stock == stock
    assert session.cursor == 0
    assert session.approach_m == 12.5
    assert session.outcome is SelectionOutcome.IN_TOLERANCE
    assert session.generated_at == GENERATED_AT


def test_run_session_is_frozen() -> None:
    """セッションは差し替えるもので、書き換えるものではない（design.md 6.1）。"""
    session = make_session(stock=(make_candidate(seed=1, loop_m=5_000.0),))

    with pytest.raises(dataclasses.FrozenInstanceError):
        session.cursor = 1  # type: ignore[misc]


def test_reroll_result_is_frozen() -> None:
    """引き直しの結果も不変（design.md 2.1）。持ち回っても壊れない。"""
    result = reroll(make_session(stock=(make_candidate(seed=1, loop_m=5_000.0),)))

    assert isinstance(result, RerollResult)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.advanced = True  # type: ignore[misc]


def test_stock_is_a_tuple() -> None:
    """在庫は生成後は不変（design.md 6.1）。list だと呼び出し側から並べ替えられる。"""
    stock = (make_candidate(seed=1, loop_m=5_000.0),)
    session = make_session(stock=stock)

    assert isinstance(session.stock, tuple)
    # 中身も見る。空を返す実装では「tuple である」だけが通ってしまう
    assert session.stock == stock
