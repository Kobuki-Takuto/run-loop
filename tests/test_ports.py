"""ports.py のテスト（design.md 3.1 / 3.2、10.1）。

このファイルが固定するのは4つ。

1. `RouteProvider` が `round_trip()` と `snap()` の2メソッドを要求すること。
   フェイクが適合し、片方を欠いたクラスは適合しないこと（design.md 3.1）
2. design.md 3.2 の翻訳表の6例外が定義され、基底 `RouteProviderError` を
   共有していて、かつ互いに区別できること
3. **異常ルート（合計距離が目標の3倍超）を例外にしていないこと。**
   例外の一覧がちょうど6件であることで、あとから「異常ルート用の例外」が
   足されたら落ちるようにしている（design.md 3.2 / AC-01-5）
4. プロバイダがドメインの閾値（±300m・3倍・接近距離）と目標距離を
   知らないこと（design.md 3.1「責務の境界」）

`ProviderRoute` / `SnapResult` のテストもここに置く。`models.py` に定義するが、
存在理由がポートの境界（この Protocol の戻り値）にあるため。
"""

import dataclasses
import re
from pathlib import Path

import pytest

from runloop import ports
from runloop.models import LatLon, ProviderRoute, SnapResult
from runloop.ports import (
    ApiKeyMissing,
    ApiKeyRejected,
    MalformedRoute,
    ProviderUnavailable,
    RateLimited,
    RouteNotFound,
    RouteProvider,
    RouteProviderError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ORIGIN = LatLon(lat=31.5966, lon=130.5571)
# 実測のスナップ先（起点から 0.7m。FINDINGS スパイク1）
SNAPPED_START = LatLon(lat=31.59663, lon=130.55712)

# design.md 3.2 の翻訳表。ここがちょうど6件であることを検査に使う
TRANSLATION_TABLE_EXCEPTIONS = (
    ApiKeyMissing,
    ApiKeyRejected,
    RouteNotFound,
    RateLimited,
    ProviderUnavailable,
    MalformedRoute,
)


def make_provider_route(
    *,
    seed: int = 1,
    loop_m: float = 6_177.4,
    snapped_start: LatLon = SNAPPED_START,
    ratelimit_remaining: int | None = None,
) -> ProviderRoute:
    """テスト用のプロバイダ応答を組む。値はスパイク1の実測に合わせている。"""
    return ProviderRoute(
        seed=seed,
        loop_m=loop_m,
        ascent_m=67.2,
        descent_m=67.2,
        snapped_start=snapped_start,
        geometry=(ORIGIN, snapped_start),
        ratelimit_remaining=ratelimit_remaining,
    )


class FakeProvider:
    """`RouteProvider` に適合する最小のフェイク。

    T07 以降の `ors/client.py` の代わりに使う。ここでは「Protocol に適合する
    実装が書けること」と「異常ルートが例外にならずに戻ること」だけを見る。
    """

    def __init__(
        self,
        *,
        route: ProviderRoute | None = None,
        snap_result: SnapResult | None = None,
    ) -> None:
        self._route = route if route is not None else make_provider_route()
        self._snap_result = snap_result
        self.round_trip_calls = 0
        self.snap_calls = 0

    def round_trip(
        self,
        origin: LatLon,
        length_m: int,
        seed: int,
        points: int,
        avoid_steps: bool,
    ) -> ProviderRoute:
        self.round_trip_calls += 1
        return self._route

    def snap(self, point: LatLon, radius_m: int) -> SnapResult | None:
        self.snap_calls += 1
        return self._snap_result


class ProviderWithoutSnap:
    """`snap()` を持たない不完全な実装（design.md 3.1 の2メソッド要求を検査する）。"""

    def round_trip(
        self,
        origin: LatLon,
        length_m: int,
        seed: int,
        points: int,
        avoid_steps: bool,
    ) -> ProviderRoute:
        return make_provider_route()


def _as_route_provider(provider: RouteProvider) -> RouteProvider:
    """静的な適合検査。`FakeProvider` が適合しなければ mypy がここで落ちる。"""
    return provider


# --- Protocol（design.md 3.1） ------------------------------------------------


def test_fake_provider_satisfies_protocol() -> None:
    """フェイクが Protocol に適合すること（完了条件「mypy が通る」の実行時側）。"""
    fake = FakeProvider()

    assert isinstance(fake, RouteProvider)
    assert _as_route_provider(fake) is fake


def test_provider_without_snap_does_not_satisfy_protocol() -> None:
    """`snap()` を欠いた実装が適合しないこと。

    `snap()` をポートに含めるのは design.md 3.1 の判断（起点が道路からどれだけ
    離れているかはプロバイダにしか答えられない）。片方だけの実装を通すと、
    起点確定のプローブ（4.6.1）を別の口から呼ぶ実装が書けてしまう。
    """
    assert not isinstance(ProviderWithoutSnap(), RouteProvider)


def test_round_trip_returns_provider_route() -> None:
    """`round_trip()` が `ProviderRoute` を1本返すこと。"""
    fake = FakeProvider()

    route = fake.round_trip(origin=ORIGIN, length_m=5_000, seed=42, points=3, avoid_steps=True)

    assert isinstance(route, ProviderRoute)
    assert route.seed == 1
    assert fake.round_trip_calls == 1


def test_snap_returns_none_when_out_of_radius() -> None:
    """半径内に道路がないとき `snap()` が `None` を返せること（design.md 3.1 / 4.6.1）。

    `None` は「圏外」を意味し、距離 0 と区別する。AC-01-3 の文言が
    「距離を含まない変種」に分かれる根拠がこの区別である（T12）。
    """
    fake = FakeProvider(snap_result=None)

    assert fake.snap(point=ORIGIN, radius_m=350) is None
    assert fake.snap_calls == 1


def test_snap_result_carries_distance_and_optional_name() -> None:
    """`SnapResult` が `snapped_distance_m` と `name` を持つこと（design.md 3.1）。"""
    result = SnapResult(snapped_distance_m=0.7, name=None)

    assert result.snapped_distance_m == 0.7
    assert result.name is None
    assert SnapResult(snapped_distance_m=12.5, name="国道225号").name == "国道225号"


# --- ProviderRoute（design.md 3.1 / 2.1） ------------------------------------


def test_provider_route_is_frozen() -> None:
    """生の成果を後から書き換えられないこと（design.md 2.1）。"""
    route = make_provider_route()

    with pytest.raises(dataclasses.FrozenInstanceError):
        route.loop_m = 1.0  # type: ignore[misc]


def test_provider_route_has_no_approach_distance() -> None:
    """`ProviderRoute` が接近距離を持たないこと（design.md 3.1）。

    接近距離は「起点」というアプリ側の概念との差であり、プロバイダの成果物ではない。
    `generation.py` が `geo.haversine(origin, snapped_start)` で算出する。
    ここに `approach_m` を持たせると、算出場所が2か所になる。
    """
    field_names = {field.name for field in dataclasses.fields(ProviderRoute)}

    assert "snapped_start" in field_names
    assert "approach_m" not in field_names
    assert "total_m" not in field_names


def test_provider_route_carries_ratelimit_remaining() -> None:
    """残数ヘッダを持ち回れること（design.md 3.2「観測値の持ち回し」）。

    画面には出さずログに出す値なので、既定は `None`（未取得）を許す。
    """
    assert make_provider_route().ratelimit_remaining is None
    assert make_provider_route(ratelimit_remaining=1_987).ratelimit_remaining == 1_987


# --- 異常ルートを例外にしない（design.md 3.2 / AC-01-5） ----------------------


def test_degenerate_route_is_returned_not_raised() -> None:
    """目標の3倍を超えるルートが例外にならずに返ること。

    415km は 200 で返る正常なレスポンスであり、「候補として使えない」は
    選択基準（AC-01-5）である。例外にすると、除外件数を数えて AC-06-3 の
    判定に使う経路（T10）が作れない。
    """
    degenerate = make_provider_route(loop_m=415_000.0)
    fake = FakeProvider(route=degenerate)

    route = fake.round_trip(origin=ORIGIN, length_m=5_000, seed=1, points=3, avoid_steps=True)

    assert route.loop_m == 415_000.0


def test_exceptions_are_exactly_the_translation_table() -> None:
    """`ports.py` の例外がちょうど design.md 3.2 の6件であること。

    件数を固定するのは、「異常ルート用の例外」が後から足されたときに
    落とすため（AC-01-5 の除外は選択側 T10 の責務）。
    """
    defined = {
        name
        for name, obj in vars(ports).items()
        if isinstance(obj, type)
        and issubclass(obj, RouteProviderError)
        and obj is not RouteProviderError
    }
    expected = {exc.__name__ for exc in TRANSLATION_TABLE_EXCEPTIONS}

    assert defined == expected


# --- ドメイン例外（design.md 3.2） ------------------------------------------


@pytest.mark.parametrize("exception_type", TRANSLATION_TABLE_EXCEPTIONS)
def test_exception_shares_the_base_class(exception_type: type[RouteProviderError]) -> None:
    """6例外すべてが `RouteProviderError` を継承すること。

    上位（T18b）が「プロバイダ由来の失敗」を1か所で捕まえられるようにする
    （AC-06-4「いずれの異常時もアプリが停止しない」）。
    """
    assert issubclass(exception_type, RouteProviderError)

    with pytest.raises(RouteProviderError):
        raise exception_type("失敗した")


def test_exceptions_are_distinguishable() -> None:
    """種類ごとに捕まえ分けられること。

    `RouteNotFound` と `RateLimited` はリトライすべきかが正反対で（design.md 4.3）、
    404 は無料枠を消費するため投げ直さない。1つの例外にまとめると T07 でこの
    判断が書けない。
    """
    with pytest.raises(RouteNotFound):
        raise RouteNotFound("この起点・シードではルートが作れない")

    with pytest.raises(RateLimited):
        raise RateLimited("毎分制限")

    with pytest.raises(RouteNotFound):
        try:
            raise RouteNotFound("翻訳表の 404")
        except RateLimited:  # pragma: no cover - 捕まらないことが期待値
            pytest.fail("RouteNotFound が RateLimited として捕まった")


@pytest.mark.parametrize("exception_type", TRANSLATION_TABLE_EXCEPTIONS)
def test_exception_carries_ratelimit_remaining(
    exception_type: type[RouteProviderError],
) -> None:
    """例外も残数を持ち回れること（design.md 3.2「観測値の持ち回し」）。

    キーワード専用にしているのは、メッセージの第2引数と取り違えないため。
    """
    assert exception_type("失敗した").ratelimit_remaining is None
    assert exception_type("失敗した", ratelimit_remaining=0).ratelimit_remaining == 0


def test_exception_message_is_preserved() -> None:
    """メッセージが失われないこと（ログに出す側 T07 / T18b の前提）。"""
    error = ProviderUnavailable("一時的な障害")

    assert str(error) == "一時的な障害"


# --- 責務の境界（design.md 3.1） --------------------------------------------

# ドメイン規則の語彙。プロバイダの境界に現れてはいけない
DOMAIN_RULE_NAMES = (
    "TOLERANCE_M",
    "DEGENERATE_FACTOR",
    "APPROACH_OK_M",
    "APPROACH_REJECT_M",
    "is_within_tolerance",
    "is_degenerate",
    "classify_approach",
    "target_m",
)


def test_ports_does_not_know_domain_rules() -> None:
    """プロバイダが目標距離と閾値を知らないこと（design.md 3.1「責務の境界」）。

    ±300m・3倍・接近距離の閾値は要件由来のドメイン規則で、プロバイダを
    替えても変わらない。ポート側に置くと、実装を足すたびに規則が複製される。
    `round_trip()` が受け取るのは `length_m`（要求する長さ）であって
    `target_m`（判定の基準）ではない。
    """
    source = (PROJECT_ROOT / "runloop" / "ports.py").read_text(encoding="utf-8")
    offenders = [name for name in DOMAIN_RULE_NAMES if re.search(rf"\b{name}\b", source)]

    assert offenders == [], f"ports.py がドメイン規則を知っている: {', '.join(offenders)}"


def test_ports_does_not_import_requests() -> None:
    """ポートに HTTP の語彙を持ち込まないこと（design.md 1.2 / 3.2）。

    `requests` と HTTP ステータスは `ors/` の中だけの知識である。
    """
    source = (PROJECT_ROOT / "runloop" / "ports.py").read_text(encoding="utf-8")

    assert not re.search(r"^\s*(?:import|from)\s+requests\b", source, flags=re.MULTILINE)
