"""messages.py のテスト（design.md 9.1 / 9.2、10.1 の messages の4行）。

このファイルが固定するのは**文言の内容そのもの**である。AC-01-3 / AC-01-4 /
AC-02-3〜5 は「何と表示するか」が受け入基準であり（design.md 9.2）、
画面を自動テストしない方針（CLAUDE.md）では、ここで固定しないと誰も検査しない。

節の分け方は design.md 9.1 の対応表と AC の並びに合わせてある。

1. 表示値（AC-02-1 / AC-02-2 / AC-03-3）——**丸めを行うのはこのモジュールだけ**
2. 調整の案内（AC-02-3 / AC-02-4）——符号で切り替わる
3. 接近距離の注意（AC-02-5）——50m 以下では出さない
4. 起点の拒否（AC-01-3）——距離を言う変種と、言わない変種
5. 結論の文言（AC-01-4 / AC-06-3 / AC-05-3）
6. 失敗の翻訳（AC-06-1 / AC-06-2 / AC-06-4）
7. 横断的な規律（次の行動を含む・座標やステータスを含まない・9.1 を網羅する）
"""

import inspect
import re

import pytest

from runloop import messages
from runloop.config import API_KEY_ENV_NAME
from runloop.models import Candidate, Checkpoint, LatLon, TurnDirection
from runloop.ports import (
    ApiKeyMissing,
    ApiKeyRejected,
    MalformedRoute,
    ProviderUnavailable,
    RateLimited,
    RouteNotFound,
    RouteProviderError,
)

# 次の行動を名指ししていることの目印（design.md 9.2「メッセージは必ず次の行動を含める」）。
# 依頼の形は「〜てください」で、動詞は文言ごとに違う（クリックして／待って／走って）。
# 「してください」に限ると、同じ依頼の形が動詞のせいで漏れる
ACTION_MARKER = "ください"

# 値を整形するだけの関数。**これらは「次の行動」を持たない**（文言ではなく表示値）
VALUE_FORMATTERS = frozenset(
    {"total_distance", "distance_error", "ascent", "checkpoint_line"}
)


def make_candidate(
    *,
    loop_m: float,
    approach_m: float = 0.0,
    ascent_m: float = 67.2,
    target_m: int = 5_000,
    seed: int = 1,
) -> Candidate:
    """テスト用の候補を組む。合計距離と距離誤差は計算プロパティ（design.md 2.2）。"""
    return Candidate(
        seed=seed,
        loop_m=loop_m,
        approach_m=approach_m,
        ascent_m=ascent_m,
        descent_m=ascent_m,
        target_m=target_m,
        geometry=(LatLon(lat=31.5966, lon=130.5571),),
    )


def make_checkpoint(
    *,
    order: int = 1,
    distance_from_origin_m: float = 1_234.0,
    direction: TurnDirection = TurnDirection.TURN_LEFT,
    name: str | None = None,
) -> Checkpoint:
    """テスト用のチェックポイントを組む。座標は文言に現れないので固定でよい。"""
    return Checkpoint(
        order=order,
        distance_from_origin_m=distance_from_origin_m,
        direction=direction,
        name=name,
        position=LatLon(lat=31.5966, lon=130.5571),
    )


def all_messages() -> dict[str, str | None]:
    """公開関数を1回ずつ呼び、名前 → 文言の対応を作る。

    横断的な規律（7節）の検査に使う。**新しい文言を足したらここに現れる**ので、
    規律の適用漏れが静かに起きない。
    """
    candidate = make_candidate(loop_m=4_760.0, approach_m=120.0)
    return {
        "total_distance": messages.total_distance(candidate),
        "distance_error": messages.distance_error(candidate),
        "ascent": messages.ascent(candidate),
        "checkpoint_line": messages.checkpoint_line(make_checkpoint()),
        "adjustment_advice": messages.adjustment_advice(candidate),
        "approach_notice": messages.approach_notice(120.0),
        "origin_rejected": messages.origin_rejected(420.3),
        "origin_no_road": messages.origin_no_road(),
        "origin_missing": messages.origin_missing(),
        "compromised": messages.compromised(),
        "no_candidate": messages.no_candidate(),
        "stock_exhausted": messages.stock_exhausted(),
        "provider_failure": messages.provider_failure(RateLimited("429 Too Many Requests")),
        "failure_summary": messages.failure_summary({"ProviderUnavailable": 15}),
        "unexpected_error": messages.unexpected_error(),
    }


# --- 1. 表示値（AC-02-1 / AC-02-2 / AC-03-3） --------------------------------


def test_total_distance_is_kilometres_with_two_decimals() -> None:
    """AC-02-1「合計距離がキロメートル単位（小数第2位まで）で表示される」。"""
    text = messages.total_distance(make_candidate(loop_m=5_124.6))

    assert "5.12" in text
    assert "km" in text


def test_total_distance_rounds_instead_of_truncating() -> None:
    """小数第3位以降を切り捨てないこと。

    切り捨てる実装（`int(m / 10) / 100` など）でも 5.12 のテストは通る。
    繰り上がる値を別に置いて区別する。
    """
    assert "5.13" in messages.total_distance(make_candidate(loop_m=5_127.0))


def test_total_distance_uses_total_not_loop() -> None:
    """表示するのは**合計距離**（ループ + 接近 × 2）であること（AC-01-2 / AC-02-1）。

    ループ距離を表示する実装は、接近距離が 0 の候補では区別できない。
    接近距離を持つ候補を置いて固定する。
    """
    # ループ 4760.0 + 接近 120.0 × 2 = 5000.0
    text = messages.total_distance(make_candidate(loop_m=4_760.0, approach_m=120.0))

    assert "5.00" in text
    assert "4.76" not in text


def test_distance_error_is_signed_in_metres() -> None:
    """AC-02-2「距離誤差がメートル単位で、符号付きで表示される」。"""
    excess = messages.distance_error(make_candidate(loop_m=5_124.6))
    shortfall = messages.distance_error(make_candidate(loop_m=4_919.4))

    assert "+125" in excess
    assert "-81" in shortfall


def test_distance_error_is_not_in_kilometres() -> None:
    """メートル単位であること（キロメートルに直さない）。

    誤差は走行中に調整する量なので、0.12 km ではなく 125 m で伝える。
    """
    text = messages.distance_error(make_candidate(loop_m=5_124.6))

    assert "m" in text
    assert "km" not in text
    assert "0.12" not in text


def test_ascent_is_marked_as_loop_only() -> None:
    """AC-03-3「獲得標高……ループ区間のみの値である旨の注記が付く」。

    接近区間の標高はレスポンスに存在せず加算できない（design.md 4.5）。
    注記なしで表示すると、接近距離が大きい起点で実態と合わない値になる。
    """
    text = messages.ascent(make_candidate(loop_m=5_000.0, ascent_m=67.2))

    assert "67" in text
    assert "周回" in text
    assert "含み" in text


# --- 1b. チェックポイントの行（AC-04-2 / AC-04-4） ---------------------------


def test_checkpoint_line_uses_the_standard_form() -> None:
    """AC-04-4「標準形は『起点から 1.2km 地点を左折』」。

    **キロメートル・小数第1位。** requirements.md が文面そのものを標準形として
    定めている数少ない例で、単位も桁もそこに書かれている。
    """
    text = messages.checkpoint_line(
        make_checkpoint(distance_from_origin_m=1_234.0, direction=TurnDirection.TURN_LEFT)
    )

    assert "1.2" in text
    assert "km" in text
    assert "左折" in text
    # メートルのまま出していないこと（T16 の初版はこの形だった）
    assert "1234" not in text


def test_checkpoint_line_rounds_instead_of_truncating() -> None:
    """小数第2位以降を切り捨てないこと（`total_distance` と同じ規律）。

    **ちょうど半分（1250m = 1.25km）を使わない。** Python の書式指定は
    偶数丸めなので 1.2 になるが、半分をどちらに倒すかは AC-04-4 が
    定めていない。要件にない挙動をテストで固定すると、実装の都合を
    仕様に格上げすることになる。ここで見たいのは**切り捨てていないこと**
    だけなので、半分から外れた値で見る。
    """
    assert "1.3" in messages.checkpoint_line(make_checkpoint(distance_from_origin_m=1_260.0))


def test_checkpoint_line_states_the_order() -> None:
    """何番目のチェックポイントかが分かること（最大5件が並ぶ。AC-04-1）。"""
    assert "3" in messages.checkpoint_line(make_checkpoint(order=3))


def test_checkpoint_line_appends_the_name_when_present() -> None:
    """AC-04-4「地点名は取得できた場合のみ併記する」。"""
    text = messages.checkpoint_line(make_checkpoint(name="国道10号"))

    assert "国道10号" in text


def test_checkpoint_line_omits_the_name_when_absent() -> None:
    """名前がないことは異常として扱わない（AC-04-4）。`None` を文字列にしない。

    実測では 71/71 = 100% が名前なしで、**こちらが通常の経路**である
    （design.md 7.1）。
    """
    text = messages.checkpoint_line(make_checkpoint(name=None))

    assert "None" not in text
    # 名前を入れる括弧だけが残らないこと（「左折（）」にしない）
    assert "（）" not in text


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (TurnDirection.TURN_LEFT, "左折"),
        (TurnDirection.TURN_RIGHT, "右折"),
        (TurnDirection.SHARP_LEFT, "鋭角左折"),
        (TurnDirection.SHARP_RIGHT, "鋭角右折"),
        (TurnDirection.SLIGHT_LEFT, "緩い左折"),
        (TurnDirection.SLIGHT_RIGHT, "緩い右折"),
    ],
)
def test_checkpoint_line_translates_every_direction(
    direction: TurnDirection, expected: str
) -> None:
    """6種の方向転換それぞれに表記があること（AC-04-2「方向転換の向き」）。"""
    assert expected in messages.checkpoint_line(make_checkpoint(direction=direction))


def test_every_turn_direction_has_a_distinct_label() -> None:
    """6種が**別々の**表記になること。

    1件ずつ「含まれる」を見るだけだと、全部を「左折」にしても
    `TURN_LEFT` の検査は通る（他は落ちるが、部分文字列の包含関係で
    「鋭角左折」が「左折」を含むような取りこぼしが起こりうる）。
    件数で締める。
    """
    labels = {
        messages.checkpoint_line(make_checkpoint(direction=direction))
        for direction in TurnDirection
    }

    assert len(labels) == len(TurnDirection)


# --- 2. 調整の案内（AC-02-3 / AC-02-4） --------------------------------------


def test_shortfall_advises_adjusting_near_the_origin() -> None:
    """AC-02-3「合計距離が目標より短い場合、不足分を起点付近で調整する案内」。"""
    text = messages.adjustment_advice(make_candidate(loop_m=4_800.0))

    assert "起点付近" in text
    assert "手前" not in text


def test_excess_advises_cutting_short() -> None:
    """AC-02-4「合計距離が目標より長い場合、超過分を手前で切り上げる案内」。"""
    text = messages.adjustment_advice(make_candidate(loop_m=5_200.0))

    assert "手前" in text
    assert "起点付近" not in text


def test_adjustment_advice_states_the_amount() -> None:
    """調整する量が入っていること。「短い」だけでは走りながら調整できない。"""
    assert "200" in messages.adjustment_advice(make_candidate(loop_m=4_800.0))
    assert "200" in messages.adjustment_advice(make_candidate(loop_m=5_200.0))


def test_adjustment_advice_on_target_asks_for_no_adjustment() -> None:
    """誤差 0 のとき、不足とも超過とも言わないこと。

    AC-02-3 / AC-02-4 はどちらも「目標より短い／長い場合」の基準で、
    ちょうどの場合を定めていない。どちらかの分岐に倒すと、
    調整の要らない状況で調整を促すことになる。
    """
    text = messages.adjustment_advice(make_candidate(loop_m=5_000.0))

    assert "起点付近" not in text
    assert "手前" not in text
    # 「何も言わない」ではない。調整が要らないことを伝える（無いのと区別する）
    assert ACTION_MARKER in text


# --- 3. 接近距離の注意（AC-02-5） -------------------------------------------


def test_approach_notice_is_absent_at_fifty_metres() -> None:
    """AC-02-5「50m 以下では表示しない」。境界のちょうど 50.0m は出さない。

    区分の定義元は `models.classify_approach`（ちょうど 50.0 は OK。design.md 5.1）。
    ここで不等号を書き直すと境界の等号が2か所に分かれる。
    """
    assert messages.approach_notice(50.0) is None


def test_approach_notice_appears_above_fifty_metres() -> None:
    """50m を**超える**場合は表示すること（AC-02-5）。"""
    assert messages.approach_notice(50.1) is not None


def test_approach_notice_states_the_value_and_the_direction() -> None:
    """AC-02-5「その値を画面に表示し、ずれの向きも併せて伝える」。

    向きとは「表示は過小評価であり、実走は表示より長くなる」こと（4.4）。
    向きを伝えないと、「足りない」と思って余分に走る判断をしてしまう。
    """
    text = messages.approach_notice(120.0)

    assert text is not None
    assert "120" in text  # 接近距離そのもの
    assert "240" in text  # 合計距離に含めた往復ぶん
    assert "長くなり" in text


def test_approach_notice_warns_that_precision_drops() -> None:
    """50〜300m の帯では ±300m が保証の対象外であることを伝える（AC-01-3 / 9.2）。"""
    text = messages.approach_notice(120.0)

    assert text is not None
    assert "精度" in text


# --- 4. 起点の拒否（AC-01-3、design.md 4.6.1） ------------------------------


def test_origin_rejected_names_the_next_action() -> None:
    """AC-01-3「300m 超のメッセージは……『最寄りの道路上をクリックしてください』」。

    起点が悪いことを述べるだけでなく、**次にすべき操作を名指しする**のが基準。
    """
    text = messages.origin_rejected(420.3)

    assert "最寄りの道路上" in text
    assert ACTION_MARKER in text


def test_origin_rejected_states_the_measured_distance() -> None:
    """距離を伴うこと（「起点が道路から 420m 離れています」）。

    小数は出さない。420.3m と 420m の差はユーザーの操作を変えない。
    """
    text = messages.origin_rejected(420.3)

    assert "420" in text
    assert "420.3" not in text


def test_origin_no_road_says_no_distance() -> None:
    """350m 以内に道路がない変種の文言に、距離が**含まれない**こと（design.md 4.6.1）。

    `snap` が `null` を返したときは測れていない。半径の 350 も、それらしい距離も
    書かない（観測していない数を画面に出さない。T07 / T08 の「0 で埋めない」と同じ）。
    """
    text = messages.origin_no_road()

    assert "見つかりません" in text
    assert re.search(r"\d", text) is None, f"距離を言わない変種に数字がある: {text}"


def test_origin_no_road_names_the_next_action() -> None:
    """距離を言えなくても、次にすべき操作は同じであること（AC-01-3）。"""
    text = messages.origin_no_road()

    assert "最寄りの道路上" in text
    assert ACTION_MARKER in text


def test_origin_rejected_and_no_road_are_different_messages() -> None:
    """2つの変種が別の文言であること（design.md 9.1 の別の行）。

    同じ文言を返す実装は、片方のテストしか無ければ通ってしまう。
    """
    assert messages.origin_rejected(420.3) != messages.origin_no_road()


def test_origin_missing_asks_for_a_map_click() -> None:
    """AC-05-3「起点を指定せずに実行しようとした場合、指定を促すメッセージ」。"""
    text = messages.origin_missing()

    assert "地図" in text
    assert "起点" in text
    assert ACTION_MARKER in text


# --- 5. 結論の文言（AC-01-4 / AC-06-3） -------------------------------------


def test_compromised_says_the_condition_was_not_met() -> None:
    """AC-01-4「条件を満たすコースがなかった旨のメッセージ」（design.md 9.1）。

    許容誤差そのものを文言に出す。「条件」とだけ言われても、
    何が満たせなかったのかが分からない。
    """
    text = messages.compromised()

    assert "300" in text
    assert "見つかりませんでした" in text


def test_no_candidate_says_nothing_was_obtained() -> None:
    """AC-06-3「候補が1件も取得できなかった場合、その旨のメッセージ」。"""
    text = messages.no_candidate()

    assert "候補" in text
    assert "得られませんでした" in text


def test_stock_exhausted_says_there_is_nothing_left() -> None:
    """AC-08-3「在庫を出し切った場合、それ以上の候補がない旨を表示する」。

    **黙って同じコースを再表示しない**ことが基準なので、尽きたことを
    言葉にする必要がある。
    """
    text = messages.stock_exhausted()

    assert "候補" in text
    assert ACTION_MARKER in text


def test_stock_exhausted_offers_searching_again() -> None:
    """次の行動が「もう一度探す」であること（design.md 6.3）。

    在庫方式では引き直しに API を使わない（AC-08-2）が、尽きたあとに
    新しいコースを得る手段は再実行しかない。**それを名指しする。**
    """
    assert "探す" in messages.stock_exhausted()


def test_stock_exhausted_is_not_the_no_candidate_message() -> None:
    """全滅（AC-06-3）と在庫切れ（AC-08-3）が別の文言であること。

    前者は1本も出せていない、後者は出したうえで次が無い。
    次にすべきことは似ているが、置かれている状況が違う。
    """
    assert messages.stock_exhausted() != messages.no_candidate()


def test_compromised_and_no_candidate_are_different_messages() -> None:
    """妥協パスと全滅が別の文言であること（design.md 5.2 の2つの結論）。

    前者はコースが1本出ており、後者は1本も出ていない。次にすべきことが違う。
    """
    assert messages.compromised() != messages.no_candidate()


# --- 6. 失敗の翻訳（AC-06-1 / AC-06-2 / AC-06-4） ---------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ApiKeyMissing("キーがない"), API_KEY_ENV_NAME),
        (ApiKeyRejected("401"), API_KEY_ENV_NAME),
        (RateLimited("429"), "混み合"),
        (ProviderUnavailable("503"), "接続できませんでした"),
        (RouteNotFound("404"), "取得できませんでした"),
        (MalformedRoute("壊れた JSON"), "取得できませんでした"),
    ],
)
def test_provider_failure_translates_each_domain_error(
    error: RouteProviderError, expected: str
) -> None:
    """6つのドメイン例外がそれぞれの文言になること（design.md 9.1 / AC-06-1 / AC-06-2）。

    翻訳の表を型ごとに固定する。1件でも取りこぼすと、画面には
    「予期しないエラー」だけが出て、ユーザーは対処のしようがない。
    """
    assert expected in messages.provider_failure(error)


def test_missing_and_rejected_keys_share_one_message() -> None:
    """キー未設定とキー無効が同じ文言であること（design.md 9.1「同上」）。

    ユーザーがすべきことは両方とも「キーの設定を確かめる」であり、
    どちらだったかは行動を変えない（原因の切り分けはログの仕事。9.2）。
    """
    missing = messages.provider_failure(ApiKeyMissing("x"))
    rejected = messages.provider_failure(ApiKeyRejected("y"))

    assert API_KEY_ENV_NAME in missing  # 両方が空でも一致するので、中身も見る
    assert missing == rejected


def test_api_key_message_names_where_to_set_it() -> None:
    """AC-06-2「設定が必要である旨」＋**設定場所**（design.md 9.1）。"""
    text = messages.provider_failure(ApiKeyMissing("x"))

    assert API_KEY_ENV_NAME in text
    assert ".env" in text
    assert "Secrets" in text


def test_rate_limited_message_asks_to_wait() -> None:
    """429 が残ったときは待って再実行する案内（design.md 9.1）。"""
    text = messages.provider_failure(RateLimited("429"))

    assert "待" in text
    assert ACTION_MARKER in text


def test_provider_failure_does_not_leak_status_or_key() -> None:
    """例外メッセージをそのまま画面に出さないこと（design.md 9.2「ログと画面を分ける」）。

    HTTP ステータスは行動を変えない。キーは漏らしてはならない
    （非機能要件・セキュリティ）。`str(exc)` を画面に流す実装をここで止める。
    """
    error = RateLimited(
        "429 Too Many Requests: POST https://api.openrouteservice.org/v2/directions "
        "(Authorization: 5b3ce35978511100)",
        ratelimit_remaining=0,
    )

    text = messages.provider_failure(error)

    assert "混み合" in text  # 文言そのものは出ている（空文字でこのテストを通さない）
    assert "429" not in text
    assert "5b3ce35978511100" not in text
    assert "Authorization" not in text
    assert "openrouteservice.org" not in text


def test_failure_summary_translates_the_dominant_failure() -> None:
    """全滅の原因を型名から文言にする（AC-06-1、2026-08-07 に追加）。

    `GenerationOutcome.failures` は**例外の型の名前 → 件数**（生成側は
    例外を上げずに数える。design.md 4.4）。全滅したとき、その原因を
    伝えないと AC-06-3 の「起点を道路の近くに」だけが出て、
    **起点は悪くないのに起点を疑わせる**（実機で実際にそうなった）。
    """
    text = messages.failure_summary({"ProviderUnavailable": 15})

    assert text is not None
    assert "接続できませんでした" in text


def test_failure_summary_picks_the_most_common_kind() -> None:
    """複数の種類が混ざったら**最も多いもの**を採る。

    15本のうち 429 が 12 件・404 が 3 件なら、伝えるべきは「混み合っている」。
    件数を見ずに先頭を採ると、少数派の原因を案内することになる。
    """
    text = messages.failure_summary({"RouteNotFound": 3, "RateLimited": 12})

    assert text is not None
    assert "混み合" in text


def test_failure_summary_is_none_when_nothing_failed() -> None:
    """失敗が無ければ `None`（何も出さない）。

    **「原因が分からない」と「失敗していない」を区別する。** 呼び出し側は
    `None` のとき AC-06-3 の文言に落とす。
    """
    assert messages.failure_summary({}) is None


def test_failure_summary_is_none_for_an_unknown_kind() -> None:
    """知らない型名なら `None`。**それらしい文言を当てない。**

    表に無い名前に既定の文言を当てると、実際とは違う原因を案内しうる。
    分からないときは呼び出し側の一般的な文言（AC-06-3）に委ねる。
    """
    assert messages.failure_summary({"SomethingUnexpected": 9}) is None


def test_domain_errors_are_still_six() -> None:
    """ドメイン例外がちょうど6件であること（T03 の完了条件）。

    ここで数えるのは、`provider_failure` が**型ごとの表**で翻訳するためである。
    7件目が増えたとき、この表に足さなければ黙って既定の文言に落ちる。
    件数を固定しておけば、増えた時点で翻訳の判断を求められる。
    """
    assert len(RouteProviderError.__subclasses__()) == 6


def test_unexpected_error_keeps_the_app_usable() -> None:
    """AC-06-4「いずれの異常時も、アプリが停止せず再実行できる状態を保つ」。

    ドメイン例外に該当しない失敗のための文言（design.md 9.1 の最終行）。
    """
    text = messages.unexpected_error()

    assert "予期しない" in text
    assert ACTION_MARKER in text


# --- 7. 横断的な規律（design.md 9.2 / T12 の完了条件） ----------------------


def test_every_public_function_is_covered_by_this_file() -> None:
    """公開関数がすべて `all_messages()` に現れること。

    横断的な規律（次の行動・座標を出さない）は、関数を1つ足しただけで
    抜ける。**列挙をテスト側で持たず、モジュールから取る**ことで、
    足したのに検査されない状態を作れないようにする。
    """
    public = {
        name
        for name, obj in inspect.getmembers(messages, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == messages.__name__
    }

    assert public == set(all_messages()), "公開関数と all_messages() の対応が食い違っている"


def test_every_message_names_a_next_action() -> None:
    """すべての文言が次の行動を含むこと（design.md 9.2）。

    US-06 の目的は「自分で対処するか諦めるかを判断したい」であり、
    状態の報告だけでは判断できない。**値を整形するだけの関数は対象外**
    （表示値であって文言ではない）。
    """
    offenders = [
        name
        for name, text in all_messages().items()
        if name not in VALUE_FORMATTERS and text is not None and ACTION_MARKER not in text
    ]

    assert offenders == [], "次の行動を含まない文言がある: " + ", ".join(offenders)


def test_no_message_is_empty() -> None:
    """どの関数も空文字を返さないこと。

    絶対に出してはならないもの（座標・ステータス・数字）を検査するテストは、
    **空文字なら必ず通る。** 出すべきものが出ていることを1か所で押さえておく
    （`approach_notice` の `None` は「出さない」という結論なので対象外）。
    """
    offenders = [name for name, text in all_messages().items() if text == ""]

    assert offenders == [], "空の文言がある: " + ", ".join(offenders)


def test_no_message_leaks_raw_floats() -> None:
    """文言に生の float（座標を含む）が現れないこと（design.md 9.2 / 8.7）。

    座標は小数第4位以上を持つ（31.5966）。表示に必要な丸め（小数第2位、AC-02-1）
    より細かい小数が現れたら、値をそのまま流している。
    """
    offenders = [
        f"{name}: {text}"
        for name, text in all_messages().items()
        if text is not None and re.search(r"\d+\.\d{3,}", text)
    ]

    assert offenders == [], "丸めていない値が文言にある: " + ", ".join(offenders)


def test_design_9_1_table_rows_have_distinct_messages() -> None:
    """design.md 9.1 の対応表の各行が別の文言であること。

    表の行は「次にすべき操作」が違うから分かれている（9.2）。同じ文言を返す
    実装は、行ごとのテストが揃っていても網羅の見かけだけが残る。
    キー未設定とキー無効だけは「同上」なので、代表として1件だけ数える。
    """
    rows = {
        "キーの問題": messages.provider_failure(ApiKeyMissing("x")),
        "起点が未指定": messages.origin_missing(),
        "接近距離 > 300m": messages.origin_rejected(420.3),
        "道路が見つからない": messages.origin_no_road(),
        "接近距離 50〜300m": messages.approach_notice(120.0),
        "全滅": messages.no_candidate(),
        "在庫0件（妥協パス）": messages.compromised(),
        "在庫を出し切った": messages.stock_exhausted(),
        "429 が残った": messages.provider_failure(RateLimited("429")),
        "5xx・接続不能": messages.provider_failure(ProviderUnavailable("503")),
        "予期しない例外": messages.unexpected_error(),
    }

    assert len(set(rows.values())) == len(rows), "9.1 の別々の行が同じ文言になっている"
