"""ORS への1回の呼び出しと、HTTP の事実の翻訳（design.md 3.2 / 4.3 / 4.5 / 4.6.1）。

**責務は「1回投げて、結果をドメインの語彙にする」ことに閉じる。** 何本投げるか、
並列にするか、途中で打ち切るかは `generation.py`（T08）の判断である。
ここで本数を持つと、二段投入（経路B）と一括並列（経路A）の切り替えが
2か所に分かれる。

このモジュールから外に出るのは `ports.py` の6例外と `models.py` の型だけで、
`requests` と HTTP ステータスは出さない（design.md 3.2）。

**リトライの判断は観測した事実（429 / 5xx / 接続 / タイムアウト）で行い、
例外の型では行わない。** design.md 4.3 の表は例外の型で書かれているが、
型で決めると表に無い 4xx（400 など）が `ProviderUnavailable` の規則に乗って
2回送られる。送り直しても結果は変わらず、無料枠を減らす可能性だけが残る。

**メッセージとログに載せてよいのは「どこで何が起きたか」だけ。** リクエスト
ヘッダ（= API キー）と応答の本文（座標を含む）は入れない
（非機能要件・セキュリティ、design.md 8.7）。
"""

import logging
import time
from collections.abc import Mapping
from typing import Any, Final

import requests

from runloop.config import RATE_LIMIT_RETRY_WAIT_S, REQUEST_TIMEOUT_S, Settings
from runloop.models import LatLon, ProviderRoute, SnapResult
from runloop.ors.mapper import to_provider_route, to_snap_result
from runloop.ports import (
    ApiKeyRejected,
    MalformedRoute,
    ProviderUnavailable,
    RateLimited,
    RouteNotFound,
    RouteProviderError,
)

_LOG: Final = logging.getLogger(__name__)

# 徒歩のプロファイル。走行は foot-walking で扱う（スパイク1以降すべてこれ）
_PROFILE: Final = "foot-walking"

# **URL の形が2つで違う。** snap に `/geojson` を付けると 406（`code: 8007`）で、
# T05 で実際に1回無駄に送った（design.md 4.6.1 / 11節 #15）
_DIRECTIONS_PATH: Final = f"/v2/directions/{_PROFILE}/geojson"
_SNAP_PATH: Final = f"/v2/snap/{_PROFILE}"

_ACCEPT_GEOJSON: Final = "application/geo+json"
_ACCEPT_JSON: Final = "application/json"

# 残数のヘッダ（実測。design.md 3.2）。無料枠の消費は設計判断の前提なので、
# 成功時も失敗時も観測値を落とさない
_REMAINING_HEADER: Final = "X-Ratelimit-Remaining"

# 投げ直す回数。design.md 4.3 の「1回」で、1回の呼び出しにつき最大2回送信になる。
# 1回の実行を通した上限（`MAX_SEND_ATTEMPTS`）は generation.py の担当
_RETRY_LIMIT: Final = 1

# 観測する HTTP の状態（design.md 3.2 の翻訳表）
_OK: Final = 200
_UNAUTHORIZED: Final = 401
_FORBIDDEN: Final = 403
_NOT_FOUND: Final = 404
_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR_FLOOR: Final = 500

# ログと例外メッセージに使う呼び出しの名前。**URL も本文も出さない**
_DIRECTIONS: Final = "directions"
_SNAP: Final = "snap"


class OrsClient:
    """`RouteProvider` の OpenRouteService 実装。

    `Settings` ごと受け取るのは、キーとベース URL が対になっているためと、
    `Settings.api_key` が `repr=False` でログに漏れない型だからである
    （キーを裸の `str` で持ち回すと、この型の保護から外れる）。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def round_trip(
        self,
        origin: LatLon,
        length_m: int,
        seed: int,
        points: int,
        avoid_steps: bool,
    ) -> ProviderRoute:
        """周回ルートを1本取得する（design.md 4.1）。

        `seed` を `to_provider_route()` に渡し直すのは、応答の `metadata` の
        echo を読むと「応答が要求を反映している」という未検証の仮定が増えるため
        （T06 の申し送り）。**こちらの台帳の値**を候補に焼き付ける。
        """
        payload, remaining = self._post(
            path=_DIRECTIONS_PATH,
            body=_round_trip_body(
                origin, length_m=length_m, seed=seed, points=points, avoid_steps=avoid_steps
            ),
            accept=_ACCEPT_GEOJSON,
            what=_DIRECTIONS,
        )
        return to_provider_route(payload, seed=seed, ratelimit_remaining=remaining)

    def snap(self, point: LatLon, radius_m: int) -> SnapResult | None:
        """点を最寄りの道路に寄せる。**半径内に道路がなければ `None`**（AC-01-3）。

        半径を引数で受けるのは `ports.py` の Protocol がそう定めているためで、
        値（350）は呼び出し側が `config.SNAP_RADIUS_M` から渡す。

        残数は `SnapResult` に載せられない（型が持たない）。**snap も枠を
        消費する**ので（T05 の実測）、ログだけが消費を観測する手段になる。
        """
        payload, _ = self._post(
            path=_SNAP_PATH,
            # 座標は [経度, 緯度] の順（directions と同じ）
            body={"locations": [[point.lon, point.lat]], "radius": radius_m},
            accept=_ACCEPT_JSON,
            what=_SNAP,
        )
        return to_snap_result(payload)

    def _post(
        self,
        *,
        path: str,
        body: Mapping[str, Any],
        accept: str,
        what: str,
    ) -> tuple[object, int | None]:
        """1回投げる。必要なら**1回だけ**投げ直し、失敗はドメイン例外にする。

        返すのは応答の本文と残数で、型への変換は `mapper.py` が行う。
        変換で `MalformedRoute` になった場合はここを抜けた後なので、
        **投げ直されない**（同じ応答が返るため。design.md 4.3）。
        """
        url = f"{self._settings.base_url}{path}"
        headers = {
            # キーはヘッダだけに置く。URL に入れるとログと履歴に残る
            "Authorization": self._settings.api_key,
            "Content-Type": _ACCEPT_JSON,
            "Accept": accept,
        }

        sends = 0
        while True:
            sends += 1
            try:
                response = requests.post(
                    url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_S
                )
            except requests.RequestException as exc:
                # 接続できない、または8秒で打ち切った（design.md 4.5）。
                # 例外の文言をそのまま載せず、種類の名前だけにする
                if sends <= _RETRY_LIMIT:
                    _LOG.debug("%s に到達できず、待たずに投げ直す（%d 回目）", what, sends)
                    continue
                raise ProviderUnavailable(
                    f"{what} に到達できない（{type(exc).__name__}）"
                ) from exc

            remaining = _remaining(response.headers)
            _LOG.debug(
                "%s: HTTP %d / 残り呼び出し可能数 %s", what, response.status_code, remaining
            )

            if response.status_code == _OK:
                return _payload(response, what=what, remaining=remaining), remaining

            if sends <= _RETRY_LIMIT and _is_retryable(response.status_code):
                if response.status_code == _TOO_MANY_REQUESTS:
                    # 毎分ウィンドウの回復には最大60秒かかりうるが、待つと10秒の
                    # 性能要件（7節）を破る。回復しなければ欠測として続行する
                    time.sleep(RATE_LIMIT_RETRY_WAIT_S)
                _LOG.debug("%s を投げ直す（%d 回目）", what, sends)
                continue

            raise _translate(response.status_code, what=what, remaining=remaining)


def _round_trip_body(
    origin: LatLon,
    *,
    length_m: int,
    seed: int,
    points: int,
    avoid_steps: bool,
) -> dict[str, object]:
    """directions のリクエスト本文。

    **座標は `[経度, 緯度]` の順**（緯度・経度ではない）。取り違えても型は通り、
    別の場所の周回が正常に返る。`mapper.py` の読み取りと合わせて、
    ここが順序を扱う唯一の場所である。

    `instructions` と `elevation` はどちらも既定では返らない。落とすと
    チェックポイント（AC-04-1）と獲得標高（AC-03-3）が出せなくなる。
    """
    options: dict[str, object] = {
        "round_trip": {"length": length_m, "points": points, "seed": seed},
    }
    if avoid_steps:
        # AC-03-1「生成されるコースに階段が含まれない」。アプリ側で階段を除く
        # 手段が無いので、送らなければこの基準は満たせない
        options["avoid_features"] = ["steps"]

    return {
        "coordinates": [[origin.lon, origin.lat]],
        "options": options,
        "instructions": True,
        "instructions_format": "text",
        "elevation": True,
        "units": "m",
    }


def _payload(response: requests.Response, *, what: str, remaining: int | None) -> object:
    """200 の本文を JSON として読む。読めなければ `MalformedRoute`。

    `ValueError` をそのまま上げると、上位が「プロバイダ由来の失敗」を1か所で
    捕まえる経路（AC-06-4）が破れる。**本文をメッセージに入れない**
    （HTML のエラーページに座標が含まれうる）。
    """
    try:
        parsed: object = response.json()
    except ValueError as exc:
        raise MalformedRoute(
            f"{what} の応答が JSON ではない", ratelimit_remaining=remaining
        ) from exc
    return parsed


def _remaining(headers: Mapping[str, str]) -> int | None:
    """残数ヘッダを読む。読めなければ `None`。

    **代わりの数を埋めない**（`ports.py`）。0 を埋めると「枠を使い切った」と
    読めてしまい、観測できていないことと区別がつかなくなる。
    """
    if _REMAINING_HEADER not in headers:
        return None
    try:
        return int(headers[_REMAINING_HEADER])
    except ValueError:
        return None


def _is_retryable(status: int) -> bool:
    """もう1回投げてよい状態か（design.md 4.3）。

    - 429 … 枠を消費しないので試行が安い。**1秒待ってから**投げ直す
    - 5xx … 一過性の障害を1回だけ吸収する。**待たない**（10秒の性能要件）

    **404 をここに入れない。** 404 は無料枠を消費し、同じ起点・同じシードでは
    決定的に再現すると考えられる。投げ直すのは枠を減らすだけになる。
    この事故は画面に現れず、無料枠の減りとしてしか観測できない。

    表に無い状態（400 など）も入れない。こちらが送った内容を受け付けられ
    なかった場合であり、同じ本文を送り直しても結果は変わらない。
    """
    return status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR_FLOOR


def _translate(status: int, *, what: str, remaining: int | None) -> RouteProviderError:
    """HTTP の数字をドメイン例外にする（design.md 3.2 の翻訳表）。

    数字のまま上げると、リトライすべきかどうかの判断（429 はする・404 は
    しない）が `generation.py` と `ui/` に散る。

    表に無い状態は `ProviderUnavailable` に寄せる。上位は欠測として続行でき
    （AC-06-4）、`MalformedRoute` にすると「200 だが読めない」という別の事実と
    混ざる。
    """
    message = f"{what} が HTTP {status} を返した"
    if status == _NOT_FOUND:
        return RouteNotFound(message, ratelimit_remaining=remaining)
    if status == _TOO_MANY_REQUESTS:
        return RateLimited(message, ratelimit_remaining=remaining)
    if status in (_UNAUTHORIZED, _FORBIDDEN):
        return ApiKeyRejected(message, ratelimit_remaining=remaining)
    return ProviderUnavailable(message, ratelimit_remaining=remaining)
