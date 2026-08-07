"""ors/client.py のテスト（design.md 3.2 / 4.3 / 4.5 / 4.6.1 / 10.2）。

このファイルが固定するのは6つ。

1. **送るリクエストの形。** `avoid_features: [steps]` を必ず送ること（AC-03-1）、
   座標が `[経度, 緯度]` の順であること、タイムアウトが8秒であること
2. **ステータスの翻訳**（AC-06-1 の前提）。404 / 429 / 401・403 / 5xx / 接続 /
   タイムアウトが design.md 3.2 の6例外になること
3. **リトライ回数。** 429 は2回送信、404 は1回だけ（design.md 4.3）。
   **404 は無料枠を消費するため投げ直さない**
4. **`/v2/snap` の形。** `/geojson` を付けず `Accept: application/json` で投げ、
   `locations[0]` を読む。`null`（半径内に道路なし）を `snapped_distance = 0` と
   区別する（design.md 4.6.1。T05 の実測）
5. **残数の持ち回し。** `ProviderRoute` と例外が `ratelimit_remaining` を持ち、
   ログに出ること
6. **例外メッセージに API キーと座標が出ないこと**（非機能要件・セキュリティ、
   design.md 8.7）

**外部 API は叩かない**（CLAUDE.md）。`mocked` fixture を autouse にしてあるので、
このファイルのどのテストからも実ネットワークに出られない（登録していない URL への
送信は `responses` が接続エラーにする）。

directions の 200 応答には T05 の実 fixture を使う。snap の実応答は
`spike/out/` にあり fixture にしていない（`snapped_distance` の1値だけで形の検証に
実データを要さない。design.md 10.3）ので、実測値をこのファイルに直接書く。
"""

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import pytest
import requests
import responses

from runloop.config import (
    ORS_BASE_URL,
    RATE_LIMIT_RETRY_WAIT_S,
    REQUEST_TIMEOUT_S,
    SNAP_RADIUS_M,
    Settings,
)
from runloop.geo import haversine
from runloop.models import LatLon, ProviderRoute, SnapResult
from runloop.ors.client import OrsClient
from runloop.ports import (
    ApiKeyRejected,
    MalformedRoute,
    ProviderUnavailable,
    RateLimited,
    RouteNotFound,
    RouteProvider,
    RouteProviderError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "ors_round_trip_5km_points3.json"

# **キーが漏れていないことを検査するための目印。** ヘッダにしか現れてはいけない
API_KEY: Final = "test-key-must-never-appear-in-messages"

# 起点。緯度と経度が桁で見分けられる値にする（取り違えを検出するため）
ORIGIN: Final = LatLon(lat=31.5966, lon=130.5571)

TARGET_M: Final = 5_000
SEED: Final = 1
POINTS: Final = 3

# 実際に投げる URL（design.md 4.6.1。**snap に `/geojson` を付けない**）
DIRECTIONS_URL: Final = f"{ORS_BASE_URL}/v2/directions/foot-walking/geojson"
SNAP_URL: Final = f"{ORS_BASE_URL}/v2/snap/foot-walking"

# ORS が残数を返すヘッダ（spike/out/ の実測。design.md 3.2）
REMAINING_HEADER: Final = "X-Ratelimit-Remaining"
REMAINING: Final = 1_999

# `/v2/snap` の実応答（2026-08-06 実測。FINDINGS スパイク6 / 要検証 #12）。
# **`name` キーが無い**（道の名前は返らなかった）
SNAP_DISTANCE_M: Final = 31.45
SNAP_PAYLOAD: Final[Mapping[str, Any]] = {
    "locations": [{"location": [130.556926, 31.596359], "snapped_distance": SNAP_DISTANCE_M}],
}


@pytest.fixture(autouse=True)
def mocked() -> Iterator[responses.RequestsMock]:
    """HTTP を差し替える。**このファイルのテストは実 API に出られない。**

    `assert_all_requests_are_fired=False` にしているのは、リトライしないことを
    検査するテストが「2件登録して1件しか使わない」形になるため。
    """
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """待機を記録して実際には眠らない。**待った秒数を検査に使う。**

    実際に眠るとテストが遅くなるだけでなく、「1秒待つ」という設計判断
    （design.md 4.3）が値として確かめられない。

    差し替えるのは `time` モジュールの属性である。client が
    `from time import sleep` で名前を束縛していると差し替えが効かず、
    待機を記録した検査（`no_wait == [1.0]`）が**空リストで落ちる**ので、
    「`import time` して `time.sleep()` を呼ぶ」形はテスト側から強制される。
    """
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return waits


@pytest.fixture(scope="module")
def route_payload() -> dict[str, Any]:
    """T05 の実レスポンス（points=3・instructions: true）。"""
    with FIXTURE.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = json.load(f)
    return loaded


def make_client(base_url: str = ORS_BASE_URL) -> OrsClient:
    """検査対象。**キーは `Settings` 経由で渡す**（`repr` に出ない型）。"""
    return OrsClient(Settings(api_key=API_KEY, base_url=base_url))


def call_round_trip(client: OrsClient) -> object:
    """`round_trip()` を既定の引数で1回呼ぶ。"""
    return client.round_trip(
        ORIGIN, length_m=TARGET_M, seed=SEED, points=POINTS, avoid_steps=True
    )


def call_snap(client: OrsClient) -> object:
    """`snap()` を本体と同じ半径で1回呼ぶ。"""
    return client.snap(ORIGIN, radius_m=SNAP_RADIUS_M)


# 2つのメソッドを同じ観点で検査するための対応。
# 名前 / 登録する URL / 呼び出し方 / 200 のときに返る本文
ENDPOINTS: Final = (
    pytest.param("round_trip", DIRECTIONS_URL, call_round_trip, id="round_trip"),
    pytest.param("snap", SNAP_URL, call_snap, id="snap"),
)

Call = Callable[[OrsClient], object]


def sent_body(request_index: int, mocked: responses.RequestsMock) -> dict[str, Any]:
    """送ったリクエスト本文を読む。"""
    raw = mocked.calls[request_index].request.body
    assert isinstance(raw, str | bytes)
    body: dict[str, Any] = json.loads(raw)
    return body


def sent_headers(request_index: int, mocked: responses.RequestsMock) -> Mapping[str, str | bytes]:
    """送ったリクエストヘッダを読む。"""
    return mocked.calls[request_index].request.headers


def sent_timeout(request_index: int, mocked: responses.RequestsMock) -> object:
    """`requests` に渡したタイムアウトを読む。

    `responses` が `PreparedRequest` に `req_kwargs` を後付けするので、
    型スタブには無い（`requests` 本来の属性ではない）。
    """
    request = mocked.calls[request_index].request
    kwargs: Mapping[str, object] = request.req_kwargs  # type: ignore[attr-defined]
    return kwargs["timeout"]


# --- Protocol への適合（design.md 3.1） --------------------------------------


def test_client_satisfies_the_route_provider_port() -> None:
    """`RouteProvider` として使えること。

    静的な適合（引数名と型）は下の注釈で mypy が見る。実行時の
    `isinstance` はメソッドの有無しか見ないので、両方を置く（T03 の申し送り）。
    """
    provider: RouteProvider = make_client()

    assert isinstance(provider, RouteProvider)


# --- 送るリクエストの形（AC-03-1、design.md 4.6.1） --------------------------


def test_round_trip_posts_to_the_directions_geojson_endpoint(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """directions は `/geojson` 付きの URL に POST すること。

    **snap とは URL の形が違う**（snap に `/geojson` を付けると 406。T05 の実測）。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    call_round_trip(make_client())

    assert mocked.calls[0].request.url == DIRECTIONS_URL
    assert mocked.calls[0].request.method == "POST"
    assert sent_headers(0, mocked)["Accept"] == "application/geo+json"


def test_round_trip_always_asks_to_avoid_steps(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """AC-03-1「生成されるコースに階段が含まれない」→ `avoid_features: [steps]`。

    アプリ側で階段を除く手段が無いので、**送らなければ AC-03-1 は満たせない。**
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    call_round_trip(make_client())

    assert sent_body(0, mocked)["options"]["avoid_features"] == ["steps"]


def test_round_trip_does_not_ask_to_avoid_steps_when_told_not_to(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """`avoid_steps=False` のとき条件を送らないこと。

    引数を無視して常に送る実装でも上のテストは通る。**引数が効いていること**を
    別に固定しないと、AC-03-1 が「たまたま既定で満たされている」状態と
    区別できない（ports.py「条件を黙って落とさずに明示的に扱う」）。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    make_client().round_trip(
        ORIGIN, length_m=TARGET_M, seed=SEED, points=POINTS, avoid_steps=False
    )

    assert "avoid_features" not in sent_body(0, mocked)["options"]


def test_round_trip_sends_coordinates_as_lon_lat(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """ORS の座標は `[経度, 緯度]` の順であること。

    取り違えても型は通り、**別の場所の周回が正常に返ってくる。**
    桁で判別できる値（緯度 31 / 経度 130）で固定する。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    call_round_trip(make_client())

    assert sent_body(0, mocked)["coordinates"] == [[ORIGIN.lon, ORIGIN.lat]]


def test_round_trip_sends_the_requested_length_points_and_seed(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """周回の指定が引数のとおりに入ること。

    シードは `generation.py` が15個作る（design.md 4.2）。ここで固定・無視すると
    15本が同じ経路になり、在庫が1本に潰れる。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    call_round_trip(make_client())

    round_trip = sent_body(0, mocked)["options"]["round_trip"]
    assert round_trip == {"length": TARGET_M, "points": POINTS, "seed": SEED}


def test_round_trip_sends_what_it_is_given_and_not_a_fixed_value(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """引数の値が本文に流れていること。**焼き付いた定数と区別する。**

    1回だけ投げるテストは、実装が `"seed": 1` と書いていても通ってしまう
    （既定の引数と同じ値だと見分けがつかない）。**2回投げて違う本文になること**
    を見れば、値を焼き付けた実装が落ちる。

    シードが効かないと15本が同じ経路になり、在庫（US-08）が1本に潰れる。
    しかも画面上は「コースが1本出る」ので正常に見え、引き直しで同じコースが
    出続けることでしか気づけない（design.md 4.2）。
    """
    for _ in range(2):
        mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)
    client = make_client()

    client.round_trip(ORIGIN, length_m=3_000, seed=11, points=2, avoid_steps=True)
    client.round_trip(ORIGIN, length_m=7_000, seed=22, points=4, avoid_steps=True)

    assert sent_body(0, mocked)["options"]["round_trip"] == {
        "length": 3_000,
        "points": 2,
        "seed": 11,
    }
    assert sent_body(1, mocked)["options"]["round_trip"] == {
        "length": 7_000,
        "points": 4,
        "seed": 22,
    }


def test_round_trip_asks_for_instructions_and_elevation(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """案内（AC-04）と標高（AC-03-3）を要求すること。

    どちらも既定では返らない。**要求を落とすと `steps` が空になり、
    チェックポイントが1件も出ない**（AC-04-1）。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    call_round_trip(make_client())
    body = sent_body(0, mocked)

    assert body["instructions"] is True
    assert body["elevation"] is True
    assert body["units"] == "m"


def test_requests_carry_the_api_key_in_the_authorization_header(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """キーはヘッダで送ること。**URL とクエリに入れない。**

    URL はログや履歴に残る（design.md 8.7 と同じ理由）。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    call_round_trip(make_client())

    assert sent_headers(0, mocked)["Authorization"] == API_KEY
    url = mocked.calls[0].request.url
    assert url is not None
    assert API_KEY not in url


def test_base_url_comes_from_the_settings(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """URL を焼き付けず `Settings.base_url` を使うこと（design.md 3.3）。"""
    other = "https://ors.example.test"
    mocked.add(responses.POST, f"{other}/v2/directions/foot-walking/geojson", json=route_payload)

    call_round_trip(make_client(base_url=other))

    url = mocked.calls[0].request.url
    assert url is not None
    assert url.startswith(other)


# --- タイムアウト（design.md 4.5） -------------------------------------------


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
def test_requests_use_the_configured_timeout(
    name: str, url: str, call: Call, mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """8秒のタイムアウトを渡すこと（design.md 4.5）。

    `timeout` を省くと `requests` は**無制限に待つ**。10秒の性能要件（7節）に
    対して、1本の応答待ちで画面が固まる経路が残る。
    """
    payload = route_payload if name == "round_trip" else dict(SNAP_PAYLOAD)
    mocked.add(responses.POST, url, json=payload, status=200)

    call(make_client())

    assert sent_timeout(0, mocked) == REQUEST_TIMEOUT_S
    assert REQUEST_TIMEOUT_S == 8.0


# --- ステータスの翻訳（design.md 3.2、AC-06-1 の前提） ----------------------

# design.md 3.2 の翻訳表。**ここに無い状態は ProviderUnavailable に寄せる**
TRANSLATIONS: Final = (
    (404, RouteNotFound),
    (429, RateLimited),
    (401, ApiKeyRejected),
    (403, ApiKeyRejected),
    (500, ProviderUnavailable),
    (502, ProviderUnavailable),
    (503, ProviderUnavailable),
)


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
@pytest.mark.parametrize(("status", "expected"), TRANSLATIONS)
def test_status_becomes_a_domain_exception(
    status: int,
    expected: type[RouteProviderError],
    name: str,
    url: str,
    call: Call,
    mocked: responses.RequestsMock,
    no_wait: list[float],
) -> None:
    """HTTP の数字を上位が判断できる語彙に翻訳すること（design.md 3.2）。

    数字のまま上げると、リトライすべきかどうかの判断（429 はする・404 は
    しない）が `generation.py` と `ui/` に散る。
    """
    mocked.add(responses.POST, url, json={"error": {"code": 2009}}, status=status)
    mocked.add(responses.POST, url, json={"error": {"code": 2009}}, status=status)

    with pytest.raises(expected):
        call(make_client())


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
def test_connection_error_becomes_provider_unavailable(
    name: str, url: str, call: Call, mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """接続できないことを一時的な障害として扱うこと（design.md 3.2）。

    `requests` の例外をそのまま上げると、上位が `requests` を知る必要が出て
    差し替え可能性（ADR-0001）が崩れる。
    """
    mocked.add(responses.POST, url, body=requests.ConnectionError("boom"))
    mocked.add(responses.POST, url, body=requests.ConnectionError("boom"))

    with pytest.raises(ProviderUnavailable):
        call(make_client())


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
def test_timeout_becomes_provider_unavailable(
    name: str, url: str, call: Call, mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """8秒で打ち切ったことを一時的な障害として扱うこと（design.md 3.2 / 4.5）。"""
    mocked.add(responses.POST, url, body=requests.ReadTimeout("too slow"))
    mocked.add(responses.POST, url, body=requests.ReadTimeout("too slow"))

    with pytest.raises(ProviderUnavailable):
        call(make_client())


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
def test_body_that_is_not_json_becomes_malformed_route(
    name: str, url: str, call: Call, mocked: responses.RequestsMock
) -> None:
    """200 でも JSON として読めなければ `MalformedRoute`（design.md 10.2）。

    `ValueError` をそのまま上げると、上位が「プロバイダ由来の失敗」を1か所で
    捕まえる経路（AC-06-4）が破れる。
    """
    mocked.add(responses.POST, url, body="<html>maintenance</html>", status=200)

    with pytest.raises(MalformedRoute):
        call(make_client())


def test_unexpected_status_is_translated_and_not_retried(
    mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """表に無い状態（400 など）も例外にし、**投げ直さない。**

    表に無いのは「こちらが送った内容を受け付けられなかった」場合であり、
    同じ本文を送り直しても結果は変わらず、無料枠を減らす可能性だけが残る。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2000}}, status=400)
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2000}}, status=400)

    with pytest.raises(RouteProviderError):
        call_round_trip(make_client())

    assert len(mocked.calls) == 1


# --- リトライ回数（design.md 4.3 / 10.2「リトライ回数」） --------------------


def test_rate_limited_is_sent_twice(
    mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """429 は1回だけ投げ直すこと（design.md 4.3）。**枠を消費しないので試行が安い。**"""
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2000}}, status=429)
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2000}}, status=429)

    with pytest.raises(RateLimited):
        call_round_trip(make_client())

    assert len(mocked.calls) == 2


def test_rate_limited_waits_one_second_before_retrying(
    mocked: responses.RequestsMock, no_wait: list[float], route_payload: dict[str, Any]
) -> None:
    """待機は1秒であること（design.md 4.3）。

    毎分ウィンドウの回復には最大60秒かかりうるが、それを待つと10秒の性能要件
    （7節）を破る。**回復しなければ欠測として続行する側に倒す。**
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2000}}, status=429)
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    call_round_trip(make_client())

    assert no_wait == [RATE_LIMIT_RETRY_WAIT_S]
    assert RATE_LIMIT_RETRY_WAIT_S == 1.0


def test_rate_limited_returns_the_route_when_the_retry_succeeds(
    mocked: responses.RequestsMock, no_wait: list[float], route_payload: dict[str, Any]
) -> None:
    """2回目が通ったら候補として返すこと（欠測にしない）。"""
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2000}}, status=429)
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    route = call_round_trip(make_client())

    assert isinstance(route, ProviderRoute)
    assert len(mocked.calls) == 2


def test_route_not_found_is_sent_only_once(
    mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """404 は投げ直さないこと（design.md 4.3 / requirements.md 4.6.1）。

    **404 は無料枠を消費する**（FINDINGS の初回の記述を訂正済み）。同じ起点・
    同じシードでは決定的に再現すると考えられ、投げ直すのは枠を減らすだけになる。
    この事故は画面に現れず、**無料枠の減りとしてしか観測できない。**
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2009}}, status=404)
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2009}}, status=404)

    with pytest.raises(RouteNotFound):
        call_round_trip(make_client())

    assert len(mocked.calls) == 1
    assert no_wait == []


@pytest.mark.parametrize("timeout_error", [requests.ReadTimeout, requests.ConnectTimeout])
def test_timeout_is_sent_only_once(
    timeout_error: type[requests.RequestException],
    mocked: responses.RequestsMock,
    no_wait: list[float],
) -> None:
    """**タイムアウトは投げ直さない**（2026-08-07、要検証 #17）。

    タイムアウトは 8.0 秒（`REQUEST_TIMEOUT_S`）待ってから起きる。投げ直すと
    **8 + 8 = 16 秒**かかり、**10 秒の性能要件を破る**（requirements.md 7節）。
    経路 A は15本を同時に投げるので、この1本が実行全体を決めてしまう。

    **接続エラーとは分けて扱う**（下のテストが対照）。接続拒否や名前解決の失敗は
    即座に返るので、投げ直しても時間を食わない。**「待ってから失敗したか」で
    分ける**のであって、例外の型でリトライを決めているのではない（design.md 4.3）。

    `ConnectTimeout` も対象にするのは、`timeout` を単一の値で渡すと接続と読み取りの
    両方に効くためである（8秒待ってから失敗する点は `ReadTimeout` と同じ）。

    欠測として続行するのは 404 と同じ扱いで、design.md 4.4 と整合する。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, body=timeout_error("too slow"))
    mocked.add(responses.POST, DIRECTIONS_URL, body=timeout_error("too slow"))

    with pytest.raises(ProviderUnavailable):
        call_round_trip(make_client())

    assert len(mocked.calls) == 1, "タイムアウトを投げ直している（16 秒かかりうる）"
    assert no_wait == []


def test_connection_error_is_still_retried_once(
    mocked: responses.RequestsMock, no_wait: list[float], route_payload: dict[str, Any]
) -> None:
    """接続エラーは**これまでどおり1回投げ直す**（design.md 4.3）。

    タイムアウトだけを分けたことの対照。ここまで一緒に止めてしまうと、
    一過性の接続失敗を吸収できなくなる（こちらは待たずに失敗するので、
    投げ直しても 10 秒要件を脅かさない）。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, body=requests.ConnectionError("boom"))
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    route = call_round_trip(make_client())

    assert len(mocked.calls) == 2, "接続エラーを投げ直していない"
    assert isinstance(route, ProviderRoute)
    assert route.seed == SEED
    assert no_wait == []


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_sent_only_once(
    status: int, mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """401 / 403 は投げ直さないこと（design.md 4.3）。

    再試行しても結果が変わらない。即座に AC-06-2 の設定案内へ落とす。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2099}}, status=status)
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 2099}}, status=status)

    with pytest.raises(ApiKeyRejected):
        call_round_trip(make_client())

    assert len(mocked.calls) == 1


def test_provider_unavailable_is_sent_twice_without_waiting(
    mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """5xx は1回だけ即時に投げ直すこと（design.md 4.3）。

    待たないのは、一過性の障害を吸収する目的に待機が要らず、
    10秒の性能要件（7節）に近づけないため。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 0}}, status=503)
    mocked.add(responses.POST, DIRECTIONS_URL, json={"error": {"code": 0}}, status=503)

    with pytest.raises(ProviderUnavailable):
        call_round_trip(make_client())

    assert len(mocked.calls) == 2
    assert no_wait == []


def test_malformed_response_is_sent_only_once(mocked: responses.RequestsMock) -> None:
    """`MalformedRoute` は投げ直さないこと（design.md 4.3）。

    同じ応答が返る。その1本を欠測として続行する。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json={"features": []}, status=200)
    mocked.add(responses.POST, DIRECTIONS_URL, json={"features": []}, status=200)

    with pytest.raises(MalformedRoute):
        call_round_trip(make_client())

    assert len(mocked.calls) == 1


# --- 200 の変換（design.md 10.2）。詳細は T06 のテストが持つ ------------------


def test_successful_response_becomes_a_provider_route(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """200 の本文を `ProviderRoute` にして返すこと。

    変換そのもの（キー名・`"-"`・type 番号）は `mapper.py` の責務で、
    T06 のテストが持つ。ここでは**client が mapper を通していること**だけを見る。
    `seed` は応答の echo ではなく**こちらが要求した値**が入る（T06 の申し送り）。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    route = call_round_trip(make_client())

    assert isinstance(route, ProviderRoute)
    assert route.seed == SEED
    assert route.loop_m == pytest.approx(4_479.4)
    assert len(route.steps) == 71


def test_client_does_not_compute_the_approach_distance(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """接近距離を client が持たないこと（design.md 3.1 / 4.4）。

    接近距離は「起点」というアプリ側の概念との差であり、プロバイダの成果物では
    ない。算出は `generation.py` が `geo.haversine(起点, snapped_start)` で行う。
    ここで持たせると、閾値（50m / 300m）を知る層が増える。
    """
    mocked.add(responses.POST, DIRECTIONS_URL, json=route_payload, status=200)

    route = call_round_trip(make_client())

    assert isinstance(route, ProviderRoute)
    assert not hasattr(route, "approach_m")
    # 上位が算出できる材料（スナップ先）は持っている
    assert haversine(ORIGIN, route.snapped_start) == pytest.approx(31.45, abs=0.5)


# --- `/v2/snap`（design.md 4.6.1。T05 の実測を反映する） ----------------------


def test_snap_posts_to_the_plain_endpoint_with_accept_json(
    mocked: responses.RequestsMock,
) -> None:
    """**`/geojson` を付けない。** 素の `/v2/snap/{profile}` に JSON で投げること。

    T05 の実測: `/v2/snap/foot-walking/geojson` は 406（`code: 8007`
    「この応答形式は未対応」）を返す。directions が `/geojson` を取るので
    同じ形だと思い込むのが罠で、**実際に1回無駄に送った**
    （design.md 4.6.1 / 11節 #15）。
    """
    mocked.add(responses.POST, SNAP_URL, json=dict(SNAP_PAYLOAD), status=200)

    call_snap(make_client())

    url = mocked.calls[0].request.url
    assert url is not None
    assert url == SNAP_URL
    assert not url.endswith("/geojson")
    assert sent_headers(0, mocked)["Accept"] == "application/json"


def test_snap_sends_the_point_and_the_requested_radius(
    mocked: responses.RequestsMock,
) -> None:
    """`locations` と `radius` を送ること。座標は `[経度, 緯度]` の順。

    半径は引数で受ける。**呼び出し側（T17）が `SNAP_RADIUS_M` を渡す**ので、
    ここに 350 を焼き付けない（`ports.py` の Protocol がそう定めている）。
    """
    mocked.add(responses.POST, SNAP_URL, json=dict(SNAP_PAYLOAD), status=200)

    call_snap(make_client())
    body = sent_body(0, mocked)

    assert body["locations"] == [[ORIGIN.lon, ORIGIN.lat]]
    assert body["radius"] == SNAP_RADIUS_M
    assert SNAP_RADIUS_M == 350


def test_snap_reads_the_first_location(mocked: responses.RequestsMock) -> None:
    """`locations[0]` の `snapped_distance` を返すこと（T05 の実測値）。"""
    mocked.add(responses.POST, SNAP_URL, json=dict(SNAP_PAYLOAD), status=200)

    result = call_snap(make_client())

    assert result == SnapResult(snapped_distance_m=SNAP_DISTANCE_M)


def test_snap_returns_none_when_no_road_is_within_the_radius(
    mocked: responses.RequestsMock,
) -> None:
    """`locations[0]` が `null` なら `None` を返すこと（design.md 4.6.1）。

    **`snapped_distance_m = 0.0` に潰さない。** 圏外の文言には距離を含めない
    （「420m 離れています」と言えない）ので、区別が画面の文言に効く
    （design.md 9.1 / 10.1、T12）。
    """
    mocked.add(responses.POST, SNAP_URL, json={"locations": [None]}, status=200)

    result = call_snap(make_client())

    assert result is None


def test_snap_keeps_zero_distance_distinct_from_no_road(
    mocked: responses.RequestsMock,
) -> None:
    """ちょうど道路上（0.0m）は `None` ではないこと。

    `if not snapped_distance` と書くと 0.0 が圏外になり、**道路の真上を
    クリックした起点が拒否される。**
    """
    mocked.add(
        responses.POST,
        SNAP_URL,
        json={"locations": [{"location": [130.5571, 31.5966], "snapped_distance": 0.0}]},
        status=200,
    )

    result = call_snap(make_client())

    assert result == SnapResult(snapped_distance_m=0.0)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("名前なし（実測。キーが無い）", None),
        ('ORS の "-"', "-"),
        ("名前あり", "県道21号"),
    ],
)
def test_snap_normalises_the_no_name_value(
    name: str, raw: str | None, mocked: responses.RequestsMock
) -> None:
    """`"-"` を `None` にすること（AC-04-4）。`"-"` をそのまま画面に出さない。"""
    location: dict[str, Any] = {"location": [130.5571, 31.5966], "snapped_distance": 12.3}
    if raw is not None:
        location["name"] = raw
    mocked.add(responses.POST, SNAP_URL, json={"locations": [location]}, status=200)

    result = call_snap(make_client())

    assert isinstance(result, SnapResult)
    assert result.name == (raw if raw not in (None, "-") else None)


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("locations が無い", {"metadata": {}}),
        ("locations が配列でない", {"locations": {"0": {}}}),
        ("locations が空", {"locations": []}),
        ("snapped_distance が無い", {"locations": [{"location": [130.5, 31.5]}]}),
        ("snapped_distance が数値でない", {"locations": [{"snapped_distance": "31.45"}]}),
        ("locations[0] が辞書でない", {"locations": ["31.45"]}),
    ],
)
def test_snap_raises_malformed_route_for_unexpected_shapes(
    label: str, payload: dict[str, Any], mocked: responses.RequestsMock
) -> None:
    """想定外の形は `MalformedRoute`。**既定値で埋めない**（design.md 3.3、T04）。

    `null` は「半径内に道路なし」という**意味のある値**で、これらとは違う。
    形が読めないことを 0m や圏外に丸めると、起点の判定（AC-01-3）が静かに狂う。
    """
    mocked.add(responses.POST, SNAP_URL, json=payload, status=200)

    with pytest.raises(MalformedRoute):
        call_snap(make_client())


# --- 残数の持ち回し（design.md 3.2） ----------------------------------------


def test_route_carries_the_remaining_quota(
    mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """残数ヘッダを `ProviderRoute` に載せること（design.md 3.2）。

    無料枠の消費は設計判断の前提（4.6.1）で、実運用で前提が崩れたことに
    気づく手段が必要である。**本文からは読めない**のでヘッダから取る。
    """
    mocked.add(
        responses.POST,
        DIRECTIONS_URL,
        json=route_payload,
        status=200,
        headers={REMAINING_HEADER: str(REMAINING)},
    )

    route = call_round_trip(make_client())

    assert isinstance(route, ProviderRoute)
    assert route.ratelimit_remaining == REMAINING


@pytest.mark.parametrize("header_value", [None, "unknown"])
def test_missing_or_unreadable_quota_is_left_as_none(
    header_value: str | None, mocked: responses.RequestsMock, route_payload: dict[str, Any]
) -> None:
    """読めない残数は `None` にすること。**代わりの数を埋めない**（ports.py）。

    0 を埋めると「枠を使い切った」と読めてしまう。
    """
    headers = {} if header_value is None else {REMAINING_HEADER: header_value}
    mocked.add(
        responses.POST, DIRECTIONS_URL, json=route_payload, status=200, headers=headers
    )

    route = call_round_trip(make_client())

    assert isinstance(route, ProviderRoute)
    assert route.ratelimit_remaining is None


@pytest.mark.parametrize(("status", "expected"), TRANSLATIONS)
def test_errors_carry_the_remaining_quota(
    status: int,
    expected: type[RouteProviderError],
    mocked: responses.RequestsMock,
    no_wait: list[float],
) -> None:
    """失敗したときも観測値を落とさないこと（design.md 3.2）。

    枠を消費する失敗（404）があるので、**失敗時の残数のほうが重要である。**
    """
    for _ in range(2):
        mocked.add(
            responses.POST,
            DIRECTIONS_URL,
            json={"error": {"code": 2009}},
            status=status,
            headers={REMAINING_HEADER: str(REMAINING)},
        )

    with pytest.raises(expected) as excinfo:
        call_round_trip(make_client())

    assert excinfo.value.ratelimit_remaining == REMAINING


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
def test_remaining_quota_is_logged(
    name: str,
    url: str,
    call: Call,
    mocked: responses.RequestsMock,
    route_payload: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """残数をログに出すこと（画面には出さない。design.md 3.2 / 9.2）。

    snap も枠を消費する（T05 の実測）ので、**snap の残数もログに出す。**
    `SnapResult` は残数を持たない型なので、ログだけが観測の手段になる。
    """
    payload = route_payload if name == "round_trip" else dict(SNAP_PAYLOAD)
    mocked.add(
        responses.POST, url, json=payload, status=200, headers={REMAINING_HEADER: str(REMAINING)}
    )

    with caplog.at_level(logging.DEBUG):
        call(make_client())

    assert str(REMAINING) in caplog.text


# --- セキュリティとプライバシー（非機能要件、design.md 8.7） -----------------


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
@pytest.mark.parametrize(("status", "expected"), TRANSLATIONS)
def test_error_messages_do_not_leak_the_api_key(
    status: int,
    expected: type[RouteProviderError],
    name: str,
    url: str,
    call: Call,
    mocked: responses.RequestsMock,
    no_wait: list[float],
) -> None:
    """例外メッセージにリクエストヘッダ（= API キー）を含めないこと。

    例外はログにも画面にも流れうる（AC-06-1）。**キーを含む可能性のある値を
    メッセージに組み込まない**（非機能要件・セキュリティ）。
    """
    for _ in range(2):
        mocked.add(responses.POST, url, json={"error": {"code": 2009}}, status=status)

    with pytest.raises(expected) as excinfo:
        call(make_client())

    message = str(excinfo.value)
    assert API_KEY not in message
    assert "Authorization" not in message


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
def test_error_messages_do_not_leak_the_origin(
    name: str, url: str, call: Call, mocked: responses.RequestsMock, no_wait: list[float]
) -> None:
    """例外メッセージに座標を含めないこと（design.md 8.7）。

    起点は自宅である。メッセージは「どこで何が起きたか」だけにする。
    """
    mocked.add(responses.POST, url, json={"error": {"code": 2009}}, status=404)

    with pytest.raises(RouteProviderError) as excinfo:
        call(make_client())

    message = str(excinfo.value)
    assert "31.59" not in message
    assert "130.55" not in message


@pytest.mark.parametrize(("name", "url", "call"), ENDPOINTS)
def test_logs_do_not_leak_the_api_key_or_the_origin(
    name: str,
    url: str,
    call: Call,
    mocked: responses.RequestsMock,
    route_payload: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ログにキーと座標を出さないこと（design.md 8.7、非機能要件）。

    残数を出すためにログを使うので、**同じ行に応答の本文やヘッダを
    まとめて出さない**ことを固定する。
    """
    payload = route_payload if name == "round_trip" else dict(SNAP_PAYLOAD)
    mocked.add(
        responses.POST, url, json=payload, status=200, headers={REMAINING_HEADER: str(REMAINING)}
    )

    with caplog.at_level(logging.DEBUG):
        call(make_client())

    assert API_KEY not in caplog.text
    assert "31.59" not in caplog.text
    assert "130.55" not in caplog.text
