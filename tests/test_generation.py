"""generation.py のテスト（design.md 4.1 / 4.2 / 4.4 / 4.6 / 10.2 / 10.4）。

**経路B（二段投入）が T08、経路A（一括並列）と乖離検出が T09。** 接近ゲートの
規則は両経路で共通で、経路A は「ゲートの答えを既に持っている」だけの違いである
（design.md 4.1）。T09 で足したものは末尾の「一括並列」節にまとめてある。

このファイルが固定するのは7つ（T08）。

1. **打ち切り**（design.md 10.2 が「テストの中心」と位置づけた項目）。
   プローブの応答が 300m 超のとき、2本目以降が**1回も送信されない**（AC-01-3）
2. **プローブも候補にする。** 別枠にすると16回になり、15回の上限（7節）を破る
3. **部分的失敗で続行する**（AC-06-4）。全滅は候補0本で返す（AC-06-3 は T10 が判定）
4. **呼び出し数が 15 を超えない**（非機能要件・API 利用）
5. **接近距離は候補ごとに、その応答の `snapped_start` から算出する**（design.md 2.2）
6. **2段目が並列である**（design.md 10.4「呼び出しの重なりはフェイク側で記録できる」）
7. **シードは実行ごとの乱数で15個、重複なし**（design.md 4.2）

T09 が足すのは4つ。

8. **キャッシュが有効ならプローブを挟まず15本が同時に飛ぶ**（design.md 4.6.2 経路A）
9. **キャッシュが無ければ二段投入に落ちる**（同 経路B）
10. **どちらの経路でも directions は15回**（非機能要件・API 利用）
11. **実測がキャッシュから 10m を超えて離れたら、破棄を呼び出し側に通知する**
    （design.md 8.5.1）。判定と表示に使う値は**各応答の実測のまま**である

**壁時計は測らない**（design.md 10.4）。並列であることは「14本が同時に飛んでいる」
という構造で確かめる。実測値（1呼び出し 1.33〜2.64秒、10本同時の壁時計 2.03秒）は
スパイクに依拠し、テストで測り直さない。

ほとんどのテストは**フェイクのプロバイダ**を使う。`generation.py` が知るのは
`ports.RouteProvider` だけで、HTTP は `ors/client.py` の内側にあるためである。
ただし「2本目以降が**送信**されない」は送信の有無そのものが主張なので、
実物の `OrsClient` を通し `responses` で数える（design.md 10.2）。
"""

import dataclasses
import json
import logging
import math
import random
import threading
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Final

import pytest
import responses

from runloop.config import (
    CACHE_DRIFT_TOLERANCE_M,
    CANDIDATE_COUNT,
    ORS_BASE_URL,
    Settings,
)
from runloop.generation import generate
from runloop.geo import EARTH_RADIUS_M, haversine
from runloop.models import (
    APPROACH_OK_M,
    APPROACH_REJECT_M,
    ApproachVerdict,
    Candidate,
    GenerationOutcome,
    LatLon,
    ProviderRoute,
    RouteQuery,
    SnapResult,
)
from runloop.ors.client import OrsClient
from runloop.ports import (
    MalformedRoute,
    ProviderUnavailable,
    RouteNotFound,
    RouteProvider,
    RouteProviderError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "ors_round_trip_5km_points3.json"

# 起点。緯度と経度が桁で見分けられる値にする（取り違えを検出するため）
ORIGIN: Final = LatLon(lat=31.5966, lon=130.5571)
TARGET_M: Final = 5_000
QUERY: Final = RouteQuery(origin=ORIGIN, target_m=TARGET_M)

# フェイクが返す値。候補ごとに違う値にして、取り違えを見つけられるようにする
LOOP_M: Final = 4_900.0
ASCENT_M: Final = 58.7
DESCENT_M: Final = 57.1
REMAINING: Final = 1_999

# 既定の接近距離。OK（50m 以下）の側に置き、ゲートを通す
NEAR_M: Final = 10.0

# ゲートに落ちる接近距離。300m を**超える**こと（ちょうど 300.0 は WARN）
FAR_M: Final = 420.0

# 2段目に投げる本数。プローブ1本を差し引く（design.md 4.1）
SECOND_STAGE_COUNT: Final = CANDIDATE_COUNT - 1

# 起点確定時のプローブで測り、保存してあったスナップ距離（design.md 8.2）。
# ゲートを通る側（50m 以下）に置く。経路A はこの値でゲートを済ませたと見なす
CACHED_M: Final = 20.0

# 実測がキャッシュから離れる幅。**ちょうど 10.0m は乖離ではない**（「10m を超えて」）。
# 座標から算出する都合で厳密な等号は作れないため、境界の両側を 0.5m 外して挟む
NEAR_CACHE_M: Final = CACHED_M + CACHE_DRIFT_TOLERANCE_M - 0.5
FAR_FROM_CACHE_M: Final = CACHED_M + CACHE_DRIFT_TOLERANCE_M + 0.5

# fixture の実応答のスナップ距離（T05 の実測 31.45m）に近いキャッシュ値。
# 実物の client を通すテストで、乖離していない経路Aを作るのに使う
CACHED_FIXTURE_M: Final = 31.0

# 並列の検査で待つ上限。落ちるときだけ待つので長めでよい
BARRIER_TIMEOUT_S: Final = 10.0

DIRECTIONS_URL: Final = f"{ORS_BASE_URL}/v2/directions/foot-walking/geojson"
API_KEY: Final = "test-key-must-never-appear-in-messages"


def north_of(origin: LatLon, meters: float) -> LatLon:
    """起点から真北へ `meters` 離れた点。

    子午線上では距離が `R × 弧度` になるという球面の解析的性質から出す。
    **`haversine` の式をテストに書き写さない**（実装のバグが同じ形でテストに
    入る。T02 の申し送り）。この点を `snapped_start` に据えれば、
    generation が算出する接近距離が `meters` になるはずである。
    """
    return LatLon(lat=origin.lat + math.degrees(meters / EARTH_RADIUS_M), lon=origin.lon)


@dataclasses.dataclass(frozen=True)
class SentCall:
    """フェイクが受け取った1回の呼び出し。"""

    origin: LatLon
    length_m: int
    seed: int
    points: int
    avoid_steps: bool


class FakeProvider:
    """`RouteProvider` のフェイク。**呼び出しの順序と重なりを記録する。**

    実 API も HTTP も使わない（design.md 10.4）。`generation.py` が知るのは
    ポートだけなので、生成の構造（何本・どの順序・同時か・打ち切るか）は
    この層で完全に観測できる。

    `approaches` / `errors` は**到着順の添字**で指定する。プローブは必ず
    添字 0 で、2段目の 1〜14 は並列なので順序が決まらないが、
    「何件が失敗するか」は順序によらず一定である。
    """

    def __init__(
        self,
        *,
        approach_m: float = NEAR_M,
        approaches: Mapping[int, float] | None = None,
        errors: Mapping[int, RouteProviderError] | None = None,
        barrier: threading.Barrier | None = None,
        barrier_from: int = 1,
    ) -> None:
        self._approach_m = approach_m
        self._approaches = dict(approaches) if approaches else {}
        self._errors = dict(errors) if errors else {}
        self._barrier = barrier
        # バリアで待ち合わせる最初の添字。既定の 1 は経路B（プローブは単独で
        # 先行するので待たせない）。経路A は 0 にして**15本全員**を待ち合わせる
        self._barrier_from = barrier_from
        self._lock = threading.Lock()
        self.calls: list[SentCall] = []
        # ("enter" | "exit", 到着順の添字) の並び。順序の主張はここから読む
        self.events: list[tuple[str, int]] = []
        self.snap_calls: list[LatLon] = []
        # 2段目が同時に飛んでいなかった（バリアが時間切れになった）
        self.not_concurrent = False

    def round_trip(
        self,
        origin: LatLon,
        length_m: int,
        seed: int,
        points: int,
        avoid_steps: bool,
    ) -> ProviderRoute:
        """1本ぶんの応答を返す。指定があれば例外を上げる。"""
        with self._lock:
            index = len(self.calls)
            self.calls.append(
                SentCall(
                    origin=origin,
                    length_m=length_m,
                    seed=seed,
                    points=points,
                    avoid_steps=avoid_steps,
                )
            )
            self.events.append(("enter", index))
        try:
            if index >= self._barrier_from:
                self._wait_for_the_others()
            if index in self._errors:
                raise self._errors[index]
            return self._route(origin, index=index, seed=seed)
        finally:
            with self._lock:
                self.events.append(("exit", index))

    def snap(self, point: LatLon, radius_m: int) -> SnapResult | None:
        """起点確定時のプローブ（T17）。**generation は呼ばないはず。**"""
        with self._lock:
            self.snap_calls.append(point)
        return SnapResult(snapped_distance_m=0.0)

    def _wait_for_the_others(self) -> None:
        """待ち合わせ対象の全員がそろうまで待つ。**そろわなければ時間切れで記録する。**

        バリアを置くと「同時に飛んでいる」ことが**待ち合わせの成否**として
        観測できる。壁時計を測らずに並列性を主張できる（design.md 10.4）。
        """
        if self._barrier is None:
            return
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            self.not_concurrent = True

    def _route(self, origin: LatLon, *, index: int, seed: int) -> ProviderRoute:
        """候補1本ぶんの応答。**添字ごとに違う値にする**（取り違えの検出）。"""
        approach_m = self._approaches[index] if index in self._approaches else self._approach_m
        start = north_of(origin, approach_m)
        return ProviderRoute(
            seed=seed,
            loop_m=LOOP_M + index,
            ascent_m=ASCENT_M + index,
            descent_m=DESCENT_M,
            snapped_start=start,
            geometry=(start, north_of(origin, approach_m + 100.0)),
            ratelimit_remaining=REMAINING - index,
        )


def seeds_of(fake: FakeProvider) -> list[int]:
    """フェイクが受け取ったシードを到着順に並べる。"""
    return [call.seed for call in fake.calls]


def not_found(index: int) -> RouteNotFound:
    """404 相当の失敗（実測される正常な範囲。design.md 4.4）。"""
    return RouteNotFound(f"directions が HTTP 404 を返した（{index}）")


# --- Protocol への適合 --------------------------------------------------------


def test_the_fake_satisfies_the_route_provider_port() -> None:
    """フェイクが本物と同じポートであること。

    ここが崩れると、以下のテストが「本体が実際に使う口」を確かめていない
    ことになる。静的な適合は下の注釈で mypy が見る（T03 / T07 と同じ形）。
    """
    provider: RouteProvider = FakeProvider()

    assert isinstance(provider, RouteProvider)


# --- 二段投入の構造（design.md 4.1） -----------------------------------------


def test_the_probe_finishes_before_any_other_call_starts() -> None:
    """1本目（プローブ）が**単独で先行する**こと（design.md 4.1）。

    15本を同時に投げてから判定すると**打ち切りが成立しない。**
    送信済みのリクエストは取り消せず、404 と同様に枠を消費する（4.6.1）。
    「1本目の応答を見てから残りを投げる」ことは順序でしか主張できない。
    """
    fake = FakeProvider()

    generate(fake, QUERY)

    assert fake.events[0] == ("enter", 0)
    assert fake.events[1] == ("exit", 0), "プローブの応答を待たずに2本目を投げている"


def test_the_second_stage_sends_the_remaining_fourteen() -> None:
    """ゲートを通ったら残り14本を投げること（design.md 4.1）。"""
    fake = FakeProvider()

    outcome = generate(fake, QUERY)

    assert len(fake.calls) == CANDIDATE_COUNT
    assert CANDIDATE_COUNT == 15
    assert outcome.aborted_early is False


def test_the_second_stage_calls_are_all_in_flight_together() -> None:
    """2段目の14本が**同時に**飛んでいること（design.md 4.1 / 10.4）。

    14人ぶんのバリアで待ち合わせる。同時に飛んでいなければ全員がそろわず
    時間切れになる。**壁時計は測らない**（実 API なしでは意味のある値が
    出ない。design.md 10.4）。

    ここは性能要件（10秒）だけの話ではない。429 の待機は
    `time.sleep()` なので（T07 の申し送り）、**並列の中で眠るワーカーが出る。**
    14本が同時に飛んでいれば1秒の待機は重なって高々1秒だが、
    ワーカー数が足りないと待機が波に分かれて積み上がる。
    """
    barrier = threading.Barrier(SECOND_STAGE_COUNT, timeout=BARRIER_TIMEOUT_S)
    fake = FakeProvider(barrier=barrier)

    generate(fake, QUERY)

    # 1本も投げていなければバリアは壊れない。**空回りで通らないように本数も見る**
    assert len(fake.calls) == CANDIDATE_COUNT
    assert fake.not_concurrent is False, "2段目が同時に飛んでいない（並列度が足りない）"
    assert SECOND_STAGE_COUNT == 14


def test_generation_never_calls_snap() -> None:
    """`snap()` を呼ばないこと（design.md 4.6.1 / 4.6.3）。

    起点確定時のプローブは `ui/`（T17）が1回だけ投げる。生成側でも投げると
    起点1つにつき2回消費し、**枠の消費が実行のたびに増える。**
    経路Bのゲートは directions の応答（`snapped_start`）で判定する。
    """
    fake = FakeProvider()

    generate(fake, QUERY)

    assert fake.calls != [], "directions を1本も投げていない（検査が空回りする）"
    assert fake.snap_calls == []


# --- 接近ゲート（AC-01-3。design.md 10.2「打ち切り」） -----------------------


def test_a_rejecting_probe_stops_the_second_stage() -> None:
    """**プローブが 300m 超なら2本目以降を投げないこと**（AC-01-3 / design.md 4.1）。

    このテストが T08 の中心である。300m 超の起点で15回消費するバグは、
    画面上は正常に見え（結果を拒否するので）、**無料枠の減りとしてしか
    現れない。** 実測でしか気づけないバグは自動テストで止める価値が高い
    （design.md 10.2）。
    """
    fake = FakeProvider(approaches={0: FAR_M})

    outcome = generate(fake, QUERY)

    assert len(fake.calls) == 1, "打ち切りが効いていない（残り14本を投げている）"
    assert outcome.verdict is ApproachVerdict.REJECT
    assert outcome.aborted_early is True
    assert outcome.calls_consumed == 1


def test_a_rejecting_probe_still_reports_the_measured_distance() -> None:
    """拒否のときも接近距離を返すこと（AC-01-3 の文言に距離が入る）。

    「420m 離れています」型の文言（design.md 9.1）は、この値がないと出せない。
    """
    fake = FakeProvider(approaches={0: FAR_M})

    outcome = generate(fake, QUERY)

    assert outcome.approach_m == pytest.approx(FAR_M, abs=0.1)


@pytest.mark.parametrize(
    ("approach_m", "expected"),
    [
        (0.0, ApproachVerdict.OK),
        (APPROACH_OK_M - 0.5, ApproachVerdict.OK),
        (APPROACH_OK_M + 0.5, ApproachVerdict.WARN),
        (APPROACH_REJECT_M - 0.5, ApproachVerdict.WARN),
    ],
)
def test_the_gate_lets_through_everything_up_to_the_reject_threshold(
    approach_m: float, expected: ApproachVerdict
) -> None:
    """300m の手前までは続行すること（design.md 5.1 の境界値表）。

    ここで確かめるのは**ゲートの配線**である。判定を取り違えると、通すべき
    起点で打ち切る（在庫が1本になる）か、拒否すべき起点に15回使う。

    **ちょうど 50.0m / 300.0m は使わない。** 接近距離は座標から算出されるので、
    度への変換と `haversine` の往復で 50.00000000043126 のような値になり、
    厳密な等号を作れない。境界の等号（50.0 は OK、300.0 は WARN）は
    `models.classify_approach` が定義元で、T02 のテストが**生の float** で
    固定している。ここで作り直すと、丸めの都合で要件の境界が動いてしまう。
    """
    fake = FakeProvider(approaches={0: approach_m})

    outcome = generate(fake, QUERY)

    assert outcome.verdict is expected
    assert len(fake.calls) == CANDIDATE_COUNT
    assert outcome.aborted_early is False


def test_just_over_the_reject_threshold_aborts() -> None:
    """300.0m を**超えた**ら打ち切ること。上のテストと対で境界を挟む。"""
    fake = FakeProvider(approaches={0: APPROACH_REJECT_M + 0.5})

    outcome = generate(fake, QUERY)

    assert outcome.verdict is ApproachVerdict.REJECT
    assert len(fake.calls) == 1


def test_generation_does_not_decide_whether_to_show_the_result() -> None:
    """拒否でも候補を握りつぶさないこと（design.md 5.2）。

    「300m 超なら結果を表示しない」は**選択の結論**（AC-01-3）であって
    生成の都合ではない。生成側が候補を0本にして返すと、「起点が悪い」と
    「ルートが見つからない」（AC-06-3）が画面上で区別できなくなる。
    `ORIGIN_REJECTED` と `NO_CANDIDATE` を分けるのは `selection.py`（T10）。
    """
    fake = FakeProvider(approaches={0: FAR_M})

    outcome = generate(fake, QUERY)

    assert len(outcome.candidates) == 1, "拒否のときもプローブの候補は返す"
    assert outcome.verdict is ApproachVerdict.REJECT


# --- プローブも候補にする（design.md 4.1「呼び出し総数を15回に保つ」） --------


def test_the_probe_response_is_kept_as_a_candidate() -> None:
    """プローブの応答を捨てないこと（design.md 4.1）。

    別枠にすると呼び出しが16回になり、根拠のある数値（7節 API 利用）が崩れる。
    """
    fake = FakeProvider()

    outcome = generate(fake, QUERY)

    assert len(outcome.candidates) == CANDIDATE_COUNT
    assert len(fake.calls) == CANDIDATE_COUNT
    probe_seed = seeds_of(fake)[0]
    assert probe_seed in {candidate.seed for candidate in outcome.candidates}


@pytest.mark.parametrize(
    ("label", "fake", "expected_calls"),
    [
        ("全部成功", FakeProvider(), CANDIDATE_COUNT),
        ("プローブが拒否", FakeProvider(approaches={0: FAR_M}), 1),
        ("2本が 404", FakeProvider(errors={1: not_found(1), 2: not_found(2)}), CANDIDATE_COUNT),
        (
            "全滅",
            FakeProvider(errors={index: not_found(index) for index in range(CANDIDATE_COUNT)}),
            CANDIDATE_COUNT,
        ),
    ],
)
def test_the_consumption_per_run_matches_the_budget(
    label: str, fake: FakeProvider, expected_calls: int
) -> None:
    """経路ごとの消費回数（非機能要件・API 利用。design.md 4.6.3 の表）。

    **上限を破っても画面には何も起きない。** 無料枠の減りとしてしか観測
    できないので、経路ごとに「何回で済むはずか」を等号で固定する。
    `<=` だけにすると、1回も投げない実装でも通ってしまう。

    `calls_consumed` は directions を呼んだ回数である。429 の投げ直し
    （client の内側で最大2回送信。T07 の申し送り）は枠を消費しないので
    ここには現れない。
    """
    outcome = generate(fake, QUERY)

    assert len(fake.calls) == expected_calls
    assert len(fake.calls) <= CANDIDATE_COUNT
    assert outcome.calls_consumed == len(fake.calls)


# --- 部分的失敗と全滅（AC-06-4 / AC-06-3。design.md 4.4） --------------------


def test_two_failures_still_leave_thirteen_candidates() -> None:
    """15本中2本が 404 でも 13 本で続行すること（AC-06-4 / design.md 10.2）。

    1〜2本の 404 は実測される正常な範囲であり（自宅 1/103、暫定座標 2/23）、
    ユーザーの行動を変えない情報である（design.md 4.4）。
    """
    fake = FakeProvider(errors={1: not_found(1), 2: not_found(2)})

    outcome = generate(fake, QUERY)

    assert len(outcome.candidates) == CANDIDATE_COUNT - 2
    assert len(fake.calls) == CANDIDATE_COUNT


def test_failures_are_counted_by_kind() -> None:
    """失敗を**種類別に**数えること（design.md 4.4）。

    種類をまとめると、404 が続いているのか 5xx なのかがログから読めない。
    どちらも「候補が減る」だが、原因も次の行動も違う。
    """
    fake = FakeProvider(
        errors={
            1: not_found(1),
            2: not_found(2),
            3: ProviderUnavailable("directions が HTTP 503 を返した"),
            4: MalformedRoute("ORS の応答が想定の形ではない"),
        }
    )

    outcome = generate(fake, QUERY)

    assert dict(outcome.failures) == {
        "RouteNotFound": 2,
        "ProviderUnavailable": 1,
        "MalformedRoute": 1,
    }


def test_all_failures_produce_no_candidates_without_raising() -> None:
    """全滅しても例外を投げないこと（AC-06-3 の前段。design.md 4.4）。

    ここで例外を上げると、AC-06-3「候補が1件も取得できなかった旨」を
    `selection.py` が判定できず、画面が例外処理の経路に落ちる。
    **候補0本は通常の結果**として返す。
    """
    fake = FakeProvider(errors={index: not_found(index) for index in range(CANDIDATE_COUNT)})

    outcome = generate(fake, QUERY)

    assert outcome.candidates == ()
    assert dict(outcome.failures) == {"RouteNotFound": CANDIDATE_COUNT}


def test_a_run_with_no_successful_probe_reports_no_verdict() -> None:
    """接近距離が1度も測れなければ `None` を返すこと。

    測れなかったことを 0m や REJECT で埋めると、「起点が道路の上にある」
    または「起点が悪い」と読めてしまう。**観測できていないことと区別する**
    （T07 の残数の扱いと同じ判断）。
    """
    fake = FakeProvider(errors={index: not_found(index) for index in range(CANDIDATE_COUNT)})

    outcome = generate(fake, QUERY)

    assert len(fake.calls) == CANDIDATE_COUNT, "全滅の経路に到達していない"
    assert outcome.approach_m is None
    assert outcome.verdict is None


def test_a_failing_probe_falls_through_to_the_next_seed() -> None:
    """プローブが失敗したら**次のシードで測り直す**こと（AC-06-4）。

    404 は 1/103〜2/23 の頻度で実測される。1本目に当たっただけで実行全体を
    諦めると、**運任せで実行が死ぬ経路**が残る。ゲートの答えは接近距離であり、
    どのシードの応答からでも得られる。

    測り直しも15回の枠の内側で行う（別枠にすると上限を破る）。
    """
    fake = FakeProvider(errors={0: not_found(0)})

    outcome = generate(fake, QUERY)

    assert len(fake.calls) == CANDIDATE_COUNT
    assert outcome.verdict is ApproachVerdict.OK
    assert len(outcome.candidates) == CANDIDATE_COUNT - 1
    assert fake.events[1] == ("exit", 0), "失敗したプローブの後も順序が保たれていない"


def test_the_gate_still_applies_after_a_failing_probe() -> None:
    """測り直したプローブが 300m 超なら、そこで打ち切ること。

    プローブが失敗したときに**ゲートを飛ばして15本投げる**実装でも、
    上のテストは通ってしまう（失敗が1件なら本数は同じ14本になる）。
    打ち切りが効いていることを別に固定する。
    """
    fake = FakeProvider(errors={0: not_found(0)}, approaches={1: FAR_M})

    outcome = generate(fake, QUERY)

    assert len(fake.calls) == 2, "測り直したプローブでゲートが効いていない"
    assert outcome.verdict is ApproachVerdict.REJECT
    assert outcome.aborted_early is True


# --- 接近距離の算出（design.md 2.2 / 4.4） ----------------------------------


def test_the_approach_distance_is_measured_from_the_origin() -> None:
    """接近距離を `haversine(起点, snapped_start)` で算出すること（design.md 4.4）。

    プロバイダは接近距離を返さない（起点はアプリ側の概念である。design.md 3.1）。
    ここで算出しないと、合計距離（AC-01-2）が組み立てられない。
    """
    fake = FakeProvider(approach_m=NEAR_M)

    outcome = generate(fake, QUERY)

    assert outcome.approach_m == pytest.approx(NEAR_M, abs=0.01)
    for candidate in outcome.candidates:
        assert candidate.approach_m == pytest.approx(NEAR_M, abs=0.01)


def test_each_candidate_keeps_its_own_measured_approach() -> None:
    """候補ごとに、**その応答の** `snapped_start` から算出すること（design.md 2.2）。

    実測では接近距離は起点だけで決まりシードに依存しないが、それは ORS の
    実測事実であってドメインの不変則ではない。プローブの値を全候補に配ると、
    前提が崩れたときに合計距離（AC-01-2）が静かに間違う。
    """
    others = {index: 25.0 for index in range(1, CANDIDATE_COUNT)}
    fake = FakeProvider(approaches={0: NEAR_M, **others})

    outcome = generate(fake, QUERY)

    by_seed = {candidate.seed: candidate for candidate in outcome.candidates}
    probe_seed = seeds_of(fake)[0]
    assert by_seed[probe_seed].approach_m == pytest.approx(NEAR_M, abs=0.01)
    rest = [by_seed[seed].approach_m for seed in seeds_of(fake)[1:]]
    assert rest == [pytest.approx(25.0, abs=0.01)] * SECOND_STAGE_COUNT


def test_the_approach_matches_the_geo_module() -> None:
    """算出が `geo.haversine` と一致すること。**別の式を持ち込まない。**

    generation が独自に距離を出すと、AC-01-3 の 50m / 300m 判定と
    AC-01-2 の合計距離が別の計算経路を通る。
    """
    fake = FakeProvider(approach_m=137.0)

    outcome = generate(fake, QUERY)

    expected = haversine(ORIGIN, north_of(ORIGIN, 137.0))
    assert outcome.approach_m == pytest.approx(expected)


# --- 候補の組み立て（design.md 2.2、ADR-0003） -------------------------------


def test_candidates_carry_the_target_distance_and_the_route_values() -> None:
    """応答の値と目標距離が候補に入ること（design.md 2.2）。

    `target_m` を候補に焼き付けるのは、`error_m`（AC-02-2）の算出に必要で、
    外から渡す形にすると別の目標距離を渡す余地が残るため。
    """
    fake = FakeProvider()

    outcome = generate(fake, QUERY)
    candidate = outcome.candidates[0]

    assert isinstance(candidate, Candidate)
    assert candidate.target_m == TARGET_M
    assert candidate.loop_m in {LOOP_M + index for index in range(CANDIDATE_COUNT)}
    assert candidate.ascent_m in {ASCENT_M + index for index in range(CANDIDATE_COUNT)}
    assert candidate.descent_m == pytest.approx(DESCENT_M)
    assert len(candidate.geometry) == 2


def test_the_requested_length_is_the_target_distance_without_correction() -> None:
    """要求する長さを補正しないこと（ADR-0003）。

    「ズレを補正すればよい」は4本のスパイク（計 127 呼び出し）で成立しないことを
    確認済みで、単純方式（シードを変えて15本投げる）を採用した。
    接近距離を差し引くなどの補正をここで足すと、その決定を静かに覆すことになる。
    """
    fake = FakeProvider()

    generate(fake, QUERY)

    assert [call.length_m for call in fake.calls] == [TARGET_M] * CANDIDATE_COUNT


def test_the_query_decides_the_origin_points_and_avoid_steps() -> None:
    """起点・頂点数・階段回避を `RouteQuery` から渡すこと。

    `avoid_steps` を落とすと AC-03-1（階段を含まない）が満たせない。
    ここで焼き付けると、`RouteQuery` の既定値が効いているのか区別できない。
    """
    query = RouteQuery(origin=ORIGIN, target_m=TARGET_M, points=4, avoid_steps=False)
    fake = FakeProvider()

    generate(fake, query)

    assert {call.origin for call in fake.calls} == {ORIGIN}
    assert {call.points for call in fake.calls} == {4}
    assert {call.avoid_steps for call in fake.calls} == {False}


def test_generation_does_not_filter_the_candidates() -> None:
    """集めた候補を1本も落とさないこと（design.md 5.1 / 5.2）。

    AC-01-5 の異常値除外と AC-01-2 の ±300m は `selection.py`（T10）の責務で
    ある。生成側で落とすと、除外件数を数えて AC-06-3 の判定に使う経路が作れない
    （`ports.py` が `MalformedRoute` に異常な長さを含めないのと同じ理由）。

    **並び順は主張しない。** `selection.py` が獲得標高の昇順に並べ替えるので
    （AC-03-2）、ここでの順序は契約ではない。
    """
    fake = FakeProvider()

    outcome = generate(fake, QUERY)

    assert len(outcome.candidates) == CANDIDATE_COUNT
    # 到着順にばらつきがある `loop_m` が、そのままの値で残っている
    assert len({candidate.loop_m for candidate in outcome.candidates}) == CANDIDATE_COUNT


# --- シード（design.md 4.2） -------------------------------------------------


def test_seeds_are_distinct_within_a_run() -> None:
    """15個のシードが重複しないこと（design.md 4.2）。

    重複すると同じ経路が2本返り、在庫（US-08）が見かけより少なくなる。
    しかも画面上は「コースが出る」ので正常に見える。
    """
    fake = FakeProvider()

    generate(fake, QUERY)

    seeds = seeds_of(fake)
    assert len(seeds) == CANDIDATE_COUNT
    assert len(set(seeds)) == CANDIDATE_COUNT


def test_seeds_differ_between_runs() -> None:
    """実行ごとに別のシードを選ぶこと（design.md 4.2）。

    固定すると「もう一度探す」（AC-08-3 の次の行動）で同じ15本が返り、
    「別のコースを見たい」（US-08）に応えられない。
    """
    first, second = FakeProvider(), FakeProvider()

    generate(first, QUERY)
    generate(second, QUERY)

    assert set(seeds_of(first)) != set(seeds_of(second))


def test_the_random_source_can_be_supplied() -> None:
    """乱数源を渡せること（テストと再現のため）。

    同じ `Random` を渡せば同じ15個が選ばれる。**本体は渡さない**ので
    既定は実行ごとの乱数のままである（上のテストが固定している）。

    比べるのは**集合**である。到着順は2段目が並列なので実行ごとに入れ替わり、
    並びを比べると「シードの選び方」ではなくスレッドの都合を検査してしまう。
    """
    first, second = FakeProvider(), FakeProvider()

    generate(first, QUERY, rng=random.Random(0))
    generate(second, QUERY, rng=random.Random(0))

    assert len(seeds_of(first)) == CANDIDATE_COUNT
    assert sorted(seeds_of(first)) == sorted(seeds_of(second))


# --- ログ（design.md 4.4 / 8.7 / 9.2） ---------------------------------------


def test_failures_are_logged_by_kind(caplog: pytest.LogCaptureFixture) -> None:
    """失敗の内訳をログに出すこと（design.md 4.4）。**画面には出さない。**

    画面に出さないと決めた以上、ログだけが「404 が増えている」ことに
    気づく手段になる。
    """
    fake = FakeProvider(errors={1: not_found(1), 2: not_found(2)})

    with caplog.at_level(logging.DEBUG):
        generate(fake, QUERY)

    assert "RouteNotFound" in caplog.text


def test_logs_do_not_leak_the_origin(caplog: pytest.LogCaptureFixture) -> None:
    """ログに座標を出さないこと（design.md 8.7、非機能要件）。

    起点は自宅である。失敗の内訳を出すためにログを使うので、
    同じ経路で座標が混ざらないことを固定する。
    """
    fake = FakeProvider(errors={1: not_found(1)}, approaches={0: FAR_M})

    with caplog.at_level(logging.DEBUG):
        generate(fake, QUERY)

    assert len(fake.calls) == 1, "拒否の経路に到達していない（検査が空回りする）"
    assert "31.59" not in caplog.text
    assert "130.55" not in caplog.text


# --- 結果の型（design.md 2.1） -----------------------------------------------


def test_the_outcome_is_frozen() -> None:
    """結果を後から書き換えられないこと（design.md 2.1）。"""
    outcome = generate(FakeProvider(), QUERY)

    assert isinstance(outcome, GenerationOutcome)
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.calls_consumed = 0  # type: ignore[misc]


# --- 一括並列（経路A。design.md 4.6.2 / 4.6.3 / 10.2） -----------------------


def test_a_valid_cache_sends_all_fifteen_at_once() -> None:
    """キャッシュが有効なら**プローブを挟まず15本が同時に飛ぶ**こと（design.md 4.6.2）。

    経路A が節約するのは呼び出し数ではなく**時間**である（約4〜5秒 → 約2秒）。
    「プローブを挟まない」は本数では主張できない——どちらの経路でも15本だからで
    ある。**15人ぶんのバリア**で待ち合わせれば、1本目を待ってから残りを投げる
    実装では全員がそろわず時間切れになる。壁時計は測らない（design.md 10.4）。
    """
    barrier = threading.Barrier(CANDIDATE_COUNT, timeout=BARRIER_TIMEOUT_S)
    fake = FakeProvider(barrier=barrier, barrier_from=0)

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert len(fake.calls) == CANDIDATE_COUNT
    assert fake.not_concurrent is False, "15本が同時に飛んでいない（プローブを挟んでいる）"
    assert outcome.aborted_early is False


def test_the_cached_path_does_not_call_snap() -> None:
    """経路A でも `snap()` を呼ばないこと（design.md 4.6.1）。

    キャッシュは起点確定時のプローブ（T17 が1回だけ投げる）の結果であり、
    実行のたびに測り直すならキャッシュを持つ意味がない。
    """
    fake = FakeProvider()

    generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert fake.calls != [], "directions を1本も投げていない（検査が空回りする）"
    assert fake.snap_calls == []


def test_the_cached_path_consumes_the_same_fifteen_calls() -> None:
    """経路A でも directions が15回であること（非機能要件・API 利用）。

    design.md 4.6.2 の表が両経路とも 15 と定めている。**等号で固定する。**
    `<=` だけにすると、1本も投げない実装でも通ってしまう。
    """
    fake = FakeProvider()

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert len(fake.calls) == CANDIDATE_COUNT
    assert outcome.calls_consumed == CANDIDATE_COUNT
    assert len(outcome.candidates) == CANDIDATE_COUNT


def test_the_cached_path_keeps_the_seeds_distinct() -> None:
    """経路A でもシードが15個・重複なしであること（design.md 4.2）。

    プローブを外す実装変更でシードの配り方が変わりうる。重複すると同じ経路が
    2本返り、在庫（US-08）が見かけより少なくなる。
    """
    fake = FakeProvider()

    generate(fake, QUERY, cached_approach_m=CACHED_M)

    seeds = seeds_of(fake)
    assert len(seeds) == CANDIDATE_COUNT
    assert len(set(seeds)) == CANDIDATE_COUNT


def test_no_cache_falls_back_to_the_two_stage_path() -> None:
    """キャッシュが無ければ二段投入に落ちること（design.md 4.6.2 経路B）。

    **経路B を消さない。** 起点だけ復元されてスナップ距離が無い状態
    （design.md 8.5 条件3〜5、8.6 の部分復旧）は通常の経路であって例外処理では
    ない。`None` を「0m のキャッシュ」と解釈すると、道路から離れた起点で
    ゲートを飛ばして15回消費する。
    """
    fake = FakeProvider()

    outcome = generate(fake, QUERY, cached_approach_m=None)

    assert fake.events[0] == ("enter", 0)
    assert fake.events[1] == ("exit", 0), "キャッシュが無いのにプローブを挟んでいない"
    assert len(fake.calls) == CANDIDATE_COUNT
    assert outcome.verdict is ApproachVerdict.OK


def test_the_two_stage_path_still_aborts_when_the_cache_is_absent() -> None:
    """キャッシュが無いときの打ち切りが残っていること（AC-01-3 / design.md 4.6.3）。

    経路A を足したときに**打ち切りの経路を壊していない**ことを確かめる。
    ここが破れると、キャッシュを持たない起点（初回・失効後）で15回消費する。
    """
    fake = FakeProvider(approaches={0: FAR_M})

    outcome = generate(fake, QUERY, cached_approach_m=None)

    assert len(fake.calls) == 1
    assert outcome.verdict is ApproachVerdict.REJECT
    assert outcome.aborted_early is True


# --- 乖離の検出（design.md 8.5.1） -------------------------------------------


def test_the_cached_path_measures_each_approach_from_its_own_response() -> None:
    """経路A でも接近距離を**各応答から実測する**こと（design.md 2.2 / 8.5.1）。

    **キャッシュは接近ゲートの入力にしか使わない。** キャッシュ値を表示や
    ±300m 判定（AC-01-2）に流用すると、比較対象が消えて乖離の検査自体が
    書けなくなる。候補ごとの実測値が残っているからこそ検査できる。
    """
    fake = FakeProvider(approach_m=NEAR_CACHE_M)

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert outcome.approach_m == pytest.approx(NEAR_CACHE_M, abs=0.01)
    for candidate in outcome.candidates:
        assert candidate.approach_m == pytest.approx(NEAR_CACHE_M, abs=0.01)
        assert candidate.approach_m != pytest.approx(CACHED_M, abs=0.01)


def test_the_verdict_comes_from_the_measurements_not_the_cache() -> None:
    """接近ゲートの結論も**実測から**出すこと（AC-01-3 / design.md 4.6.3）。

    キャッシュが誤っていれば、経路A は不適切な起点に15回使う。**その1回ぶんの
    露出は受け入れる**（design.md 4.6.3）。しかし結果まで受け入れるわけではない。
    キャッシュ値（20m = OK）を verdict に流すと、実際には 420m 離れた起点の
    コースが画面に出て AC-01-3「300m 超なら結果を表示せず拒否する」を破る。
    """
    fake = FakeProvider(approach_m=FAR_M)

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert outcome.verdict is ApproachVerdict.REJECT
    assert outcome.approach_m == pytest.approx(FAR_M, abs=0.1)
    assert len(fake.calls) == CANDIDATE_COUNT, "経路A は送信済みなので打ち切れない"
    assert outcome.aborted_early is False


@pytest.mark.parametrize(
    ("measured_m", "expected"),
    [
        (CACHED_M, False),
        (NEAR_CACHE_M, False),
        (FAR_FROM_CACHE_M, True),
        (FAR_M, True),
    ],
)
def test_a_drift_beyond_the_tolerance_asks_the_caller_to_discard_the_cache(
    measured_m: float, expected: bool
) -> None:
    """実測がキャッシュから **10m を超えて**離れたら通知すること（design.md 8.5.1）。

    「スナップ距離は起点だけで決まる」は ORS の実測事実であってドメインの
    不変則ではない。キャッシュはこの事実を**構造の前提**に格上げする操作なので、
    前提が崩れたことに気づく手段を同時に持たなければならない。持たなければ、
    崩れたとき静かに間違える。

    許容 10m は `/v2/snap` の `snapped_distance` と本アプリの haversine が
    別々の計算経路を通るための幅であって、ずれを許す幅ではない
    （実測の分散はゼロ。99回すべて同一値）。
    """
    fake = FakeProvider(approach_m=measured_m)

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert outcome.cache_diverged is expected


def test_a_drift_of_exactly_the_tolerance_is_not_a_divergence() -> None:
    """**ちょうど 10.0m は乖離としない**（design.md 8.5.1「10m を**超えて**」）。

    上のパラメータ化は境界を ±0.5m で挟むだけで、等号の向きを固定していない。
    座標から算出した実測値でちょうどの差を作れないためだが、**差の方を実測値から
    引いて作れば**厳密な等号になる。`measured - 10.0` の引き算が誤差なく
    ちょうど 10.0 を返すことは前提なので、テスト自身がそれを先に確かめる
    （前提が崩れたら「境界を検査したつもり」で通り続けるより、落ちるほうがよい）。

    等号の向きが逆だと、`/v2/snap` と haversine の計算経路の違いだけで
    キャッシュが破棄され、経路A に入れないまま毎回プローブすることになる。
    """
    measured_m = haversine(ORIGIN, north_of(ORIGIN, CACHED_M))
    cached_m = measured_m - CACHE_DRIFT_TOLERANCE_M
    assert measured_m - cached_m == CACHE_DRIFT_TOLERANCE_M, "ちょうどの差を作れていない"
    fake = FakeProvider(approach_m=CACHED_M)

    outcome = generate(fake, QUERY, cached_approach_m=cached_m)

    assert outcome.cache_diverged is False


def test_one_diverging_candidate_is_enough_to_discard_the_cache() -> None:
    """**各候補**を比べること（design.md 8.5.1「各候補の `approach_m` を」）。

    14本が一致していても1本ずれていれば、「起点だけで決まる」という前提は
    もう成り立っていない。平均や代表値1本で比べると、この1本が埋もれる。
    """
    fake = FakeProvider(approach_m=CACHED_M, approaches={7: FAR_FROM_CACHE_M})

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert len(outcome.candidates) == CANDIDATE_COUNT, "検査対象がそろっていない"
    assert outcome.cache_diverged is True


def test_a_diverging_probe_is_compared_too() -> None:
    """プローブの応答も比較の対象であること。

    経路A ではプローブと2段目の区別が無いが、実装が経路B と部品を共有するとき
    「1本目だけ検査から漏れる」形になりうる。1本目だけずれた場合を固定する。
    """
    fake = FakeProvider(approach_m=CACHED_M, approaches={0: FAR_FROM_CACHE_M})

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert outcome.cache_diverged is True


def test_a_divergence_does_not_throw_away_the_candidates() -> None:
    """乖離を検出しても候補を落とさないこと（AC-06-4 / design.md 4.6.3）。

    損害は**キャッシュが信用できないこと**であって、集めた15本が壊れたわけでは
    ない。接近距離は候補ごとに実測してあるので、合計距離（AC-01-2）は正しい。
    捨てると、その1回の実行で消費した15回が無駄になる。
    """
    fake = FakeProvider(approach_m=FAR_FROM_CACHE_M)

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert len(outcome.candidates) == CANDIDATE_COUNT
    assert outcome.cache_diverged is True


def test_the_two_stage_path_reports_no_divergence() -> None:
    """キャッシュが無い実行では通知しないこと（design.md 8.5.1）。

    比べる相手が無いのだから、破棄を促す理由も無い。ここが常に `True` だと、
    呼び出し側（T17 以降）が保存したばかりのキャッシュを毎回捨てることになり、
    経路A に入れなくなる。
    """
    fake = FakeProvider(approach_m=FAR_FROM_CACHE_M)

    outcome = generate(fake, QUERY, cached_approach_m=None)

    assert outcome.cache_diverged is False


def test_a_cached_run_with_no_response_cannot_conclude_a_divergence() -> None:
    """1本も測れなければ乖離とは言えないこと（AC-06-4）。

    全滅は API 側の事情であってキャッシュの誤りではない。破棄すると、
    通信が不安定な間ずっとキャッシュを失い続ける（毎回プローブし直しになる）。
    **観測できていないことと「ずれていた」を区別する**（`approach_m` が
    `None` になるのと同じ判断）。
    """
    fake = FakeProvider(errors={index: not_found(index) for index in range(CANDIDATE_COUNT)})

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert len(fake.calls) == CANDIDATE_COUNT, "全滅の経路に到達していない"
    assert outcome.candidates == ()
    assert outcome.cache_diverged is False
    assert outcome.approach_m is None
    assert outcome.verdict is None


def test_the_cached_path_survives_partial_failures() -> None:
    """経路A でも部分的失敗で続行すること（AC-06-4）。

    ゲートを飛ばす経路でも失敗の扱いは変わらない。

    **消費は候補の数ではない。** 失敗した2本も枠を消費している。ここを
    `len(candidates)` で数えると、失敗のある実行ほど消費を少なく見積もり、
    15回の上限（非機能要件）の確認が甘くなる。
    """
    fake = FakeProvider(errors={1: not_found(1), 2: not_found(2)})

    outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert len(fake.calls) == CANDIDATE_COUNT
    assert len(outcome.candidates) == CANDIDATE_COUNT - 2
    assert outcome.calls_consumed == CANDIDATE_COUNT
    assert dict(outcome.failures) == {"RouteNotFound": 2}


def test_a_divergence_is_logged_without_coordinates(caplog: pytest.LogCaptureFixture) -> None:
    """乖離をログに出すこと。**座標は出さない**（design.md 8.5.1 / 8.7）。

    画面には出さない（ユーザーに取れる行動が無い。design.md 9.2）ので、
    ログだけが「前提が崩れている」ことに気づく手段になる。要検証 #14
    （TTL 30日の妥当性）は、この記録が何回出るかで判断すると決めてある。
    """
    fake = FakeProvider(approach_m=FAR_FROM_CACHE_M)

    with caplog.at_level(logging.DEBUG):
        outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert outcome.cache_diverged is True
    assert "乖離" in caplog.text
    assert "31.59" not in caplog.text
    assert "130.55" not in caplog.text


def test_a_run_without_divergence_does_not_log_one(caplog: pytest.LogCaptureFixture) -> None:
    """乖離していないときに出さないこと。

    無条件に出す実装でも上のテストは通る。**ログが警報として機能するには、
    出ないときがなければならない。**
    """
    fake = FakeProvider(approach_m=NEAR_CACHE_M)

    with caplog.at_level(logging.DEBUG):
        outcome = generate(fake, QUERY, cached_approach_m=CACHED_M)

    assert outcome.cache_diverged is False
    assert "乖離" not in caplog.text


# --- 実物の client を通した打ち切り（design.md 10.2「2本目以降が送信されない」） ---


@pytest.fixture(scope="module")
def route_payload() -> dict[str, Any]:
    """T05 の実レスポンス（points=3・instructions: true）。"""
    with FIXTURE.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = json.load(f)
    return loaded


def payload_with_start(payload: Mapping[str, Any], start: LatLon) -> dict[str, Any]:
    """ルート始点（= スナップ先）を差し替えた応答を作る。

    fixture の実応答は起点から 31.45m の地点から始まる（T05 の実測）。
    ゲートに落ちる応答は実データに無いので、**先頭の座標だけを書き換えた
    変種**として作る（design.md 10.3「値の場合分けは変種で確認する」）。
    """
    copied = deepcopy(dict(payload))
    coordinates = copied["features"][0]["geometry"]["coordinates"]
    elevation = coordinates[0][2]
    coordinates[0] = [start.lon, start.lat, elevation]
    return copied


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    """HTTP を差し替える。**このファイルのテストは実 API に出られない。**"""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


def test_a_rejecting_probe_sends_only_one_http_request(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """**2本目以降が1回も送信されないこと**（AC-01-3 / design.md 10.2）。

    フェイクで数えるのは「ポートを何回呼んだか」であって「何回送信したか」
    ではない。打ち切りの主張は**送信そのもの**なので、実物の `OrsClient` を
    通して `responses` で数える。ここが破れると、道路から離れた地点を
    クリックした1回の操作で無料枠を15回消費する。
    """
    rejecting = payload_with_start(route_payload, north_of(ORIGIN, FAR_M))
    mocked.add(responses.POST, DIRECTIONS_URL, json=rejecting, status=200)
    for _ in range(CANDIDATE_COUNT):
        mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)
    client = OrsClient(Settings(api_key=API_KEY, base_url=ORS_BASE_URL))

    outcome = generate(client, QUERY)

    assert len(mocked.calls) == 1, "打ち切りが効かず、2本目以降を送信している"
    assert outcome.verdict is ApproachVerdict.REJECT
    assert outcome.aborted_early is True


def test_a_full_run_sends_exactly_fifteen_http_requests(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """ゲートを通ったときの送信が15回であること（非機能要件・API 利用）。

    fixture の実応答は起点から 31.45m（`APPROACH_OK_M` = 50m 以下なので OK）で、
    ゲートは通る。16回目が来ないことを、登録数を余分にして確かめる。
    """
    for _ in range(CANDIDATE_COUNT + 2):
        mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)
    client = OrsClient(Settings(api_key=API_KEY, base_url=ORS_BASE_URL))

    outcome = generate(client, QUERY)

    assert len(mocked.calls) == CANDIDATE_COUNT
    assert outcome.verdict is ApproachVerdict.OK
    assert len(outcome.candidates) == CANDIDATE_COUNT


def test_the_cached_path_also_sends_exactly_fifteen_http_requests(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """経路A の送信も15回であること（design.md 4.6.2 の表。非機能要件・API 利用）。

    経路A はゲートを飛ばす。**飛ばしたぶん多く投げていないこと**を、送信の数
    そのもので確かめる（フェイクが数えるのは「ポートを何回呼んだか」であって
    「何回送信したか」ではない）。16回目が来ないことを、登録数を余分にして見る。

    fixture の実応答は起点から 31.46m で、キャッシュ値 31.0m との差は 0.5m
    未満なので乖離にはならない。
    """
    for _ in range(CANDIDATE_COUNT + 2):
        mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)
    client = OrsClient(Settings(api_key=API_KEY, base_url=ORS_BASE_URL))

    outcome = generate(client, QUERY, cached_approach_m=CACHED_FIXTURE_M)

    assert len(mocked.calls) == CANDIDATE_COUNT
    assert len(outcome.candidates) == CANDIDATE_COUNT
    assert outcome.verdict is ApproachVerdict.OK
    assert outcome.cache_diverged is False
