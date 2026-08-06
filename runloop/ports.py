"""外部のルート提供サービスとの境界（design.md 3.1 / 3.2）。

このモジュールが持つのは2つだけ。**ルート提供者に何を頼めるか**（`RouteProvider`）と、
**どう失敗しうるか**（ドメイン例外）である。

`requests` と HTTP ステータスをここに持ち込まない。上位層（`generation.py` /
`ui/`）が扱う語彙は、この6例外だけにする。理由は、リトライすべきかどうかの判断が
例外の種類ごとに正反対であり（design.md 4.3）、HTTP の数字のまま上げると
その判断が上位に散るため。翻訳は `ors/client.py` の責務（design.md 3.2）。

プロバイダは「1本のルートを取ってくる」ことだけを知る。目標距離、許容誤差、
異常値の倍率、接近距離の閾値は**一切知らない**。これらは要件由来のドメイン規則で、
プロバイダを替えても変わらないため、実装ごとに複製したくない（design.md 3.1）。
"""

from typing import Protocol, runtime_checkable

from runloop.models import LatLon, ProviderRoute, SnapResult


class RouteProviderError(Exception):
    """プロバイダ由来の失敗の基底。

    上位が「プロバイダで何かが起きた」を1か所で捕まえられるようにする
    （AC-06-4「いずれの異常時も、アプリが停止せず再実行できる状態を保つ」）。
    個別の判断が必要な場所では、下の具体的な型で捕まえ分ける。

    `ratelimit_remaining` はレスポンスヘッダの残数（design.md 3.2）。
    無料枠の消費は設計判断の前提なので、失敗したときも観測値を落とさない。
    **画面には出さずログに出す。** 取得できなければ None で、代わりの数を埋めない。

    例外メッセージにリクエストヘッダ（= API キー）を含めないこと
    （非機能要件・セキュリティ）。
    """

    def __init__(self, message: str, *, ratelimit_remaining: int | None = None) -> None:
        super().__init__(message)
        self.ratelimit_remaining = ratelimit_remaining


class ApiKeyMissing(RouteProviderError):
    """キーが設定されていない。**呼び出す前**に判定する（AC-06-2）。

    1回も送信せずにこれを上げる。メッセージにはキー名と探した場所を入れ、
    値そのものは入れない（design.md 3.3）。
    """


class ApiKeyRejected(RouteProviderError):
    """キーが無効（401 / 403）。設定を直す必要がある。"""


class RouteNotFound(RouteProviderError):
    """この起点・シードでは周回が作れない（404 / `code: 2009`）。

    **投げ直さない。** 404 は無料枠を消費するため、同じ条件で再送すると
    枠を減らすだけになる（design.md 4.3 / requirements.md 4.6.1）。
    欠測として扱い、残りの候補で続行する（AC-06-4）。
    """


class RateLimited(RouteProviderError):
    """毎分の呼び出し制限（429）。枠は消費しない。

    こちらは短い待機の後に1回だけ投げ直す（design.md 4.3）。
    `RouteNotFound` と判断が正反対なので、同じ型にまとめない。
    """


class ProviderUnavailable(RouteProviderError):
    """一時的な障害（5xx / 接続エラー / タイムアウト）。"""


class MalformedRoute(RouteProviderError):
    """200 だが応答が想定の形でなく、変換できない（design.md 10.2）。

    **ルートが極端に長いことは、これに含めない。** それは 200 で返る正常な
    応答であり、候補として使えるかどうかは選択基準（AC-01-5）である。
    除外を例外にすると、除外件数を数えて AC-06-3 の判定に使う経路が作れない。
    """


@runtime_checkable
class RouteProvider(Protocol):
    """ルート提供サービスに頼めること。

    実装は `ors/` に置き、上位からは名前で参照しない（`config.py` が組み立てて
    注入する）。抽象化の粒度を「1本の取得」にしているのは、並列化・本数・
    打ち切りが API 利用の非機能要件に属する判断で、プロバイダごとに書き直したく
    ないため。15本投げるロジックは `generation.py` に1つだけ置く（design.md 3.1）。

    `runtime_checkable` にしているのは、テストでフェイクの適合を実行時にも
    確かめるため。**メソッドの有無しか見ないので、静的な適合は mypy が担保する。**
    """

    def round_trip(
        self,
        origin: LatLon,
        length_m: int,
        seed: int,
        points: int,
        avoid_steps: bool,
    ) -> ProviderRoute:
        """周回ルートを1本取得する。

        `length_m` は「要求する周回の長さ」であって判定の基準ではない。
        返ってきた長さが要求と違っていても、それは失敗ではない
        （どれだけ違うかを評価するのはドメイン側の仕事）。

        `avoid_steps` は階段回避（AC-03-1）。プロバイダが対応しない場合でも、
        条件を黙って落とさずに実装側で明示的に扱う。
        """
        ...

    def snap(self, point: LatLon, radius_m: int) -> SnapResult | None:
        """点を最寄りの道路に寄せ、その距離を返す。**半径内に道路がなければ None。**

        起点確定時のプローブに使う（design.md 4.6.1）。`None` と
        `snapped_distance_m = 0.0` は意味が違うので、`None` を 0 に潰さない。

        このメソッドをポートに含めるのは、「起点が道路からどれだけ離れているか」は
        プロバイダにしか答えられない問いであり、`round_trip()` と同じ外部境界に
        あるため。持たないサービスには、`round_trip()` を1本投げて
        `snapped_start` から求める実装を書けばよい（design.md 3.1）。
        """
        ...
