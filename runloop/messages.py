"""画面文言の生成（design.md 9.1 / 9.2）。**表示はしない。**

文言をここに集めるのは、AC-01-3 / AC-01-4 / AC-02-3〜5 が「何と表示するか」を
そのまま基準にしているためである。画面に文字列リテラルを書くと、Streamlit の
画面を自動テストしない方針（CLAUDE.md）では誰も検査できなくなる。

**丸めを行うのはこのモジュールだけである**（design.md 2.2）。判定は生の float で
行い（`models.py`）、丸めた値で判定しない。表示のために丸めた値で判定すると、
±300m の境界付近で表示と判定が食い違う。

**ログと画面を分ける**（design.md 9.2）。例外メッセージ・HTTP ステータス・残数は
ログの領分で、ここが返すのは行動可能な文言だけである。ユーザーは走る直前の
スマートフォンでこれを読む。ステータス番号は行動を変えない。

**どの文言も次の行動を含む**（design.md 9.2）。US-06 の目的は「自分で対処するか
諦めるかを判断したい」であり、状態の報告だけでは判断できない。
値を整形するだけの3つ（`total_distance` / `distance_error` / `ascent`）は
文言ではなく表示値なので、この規律の対象外である。
"""

from collections.abc import Mapping
from typing import Final

from runloop.config import API_KEY_ENV_NAME
from runloop.models import (
    APPROACH_OK_M,
    TOLERANCE_M,
    Candidate,
    Checkpoint,
    TurnDirection,
)
from runloop.ports import (
    ApiKeyMissing,
    ApiKeyRejected,
    MalformedRoute,
    ProviderUnavailable,
    RateLimited,
    RouteNotFound,
    RouteProviderError,
)

# 表示の単位換算。要件由来の閾値ではないので、ここに置いてよい（design.md 3.3）
_METRES_PER_KM: Final = 1000.0

# --- 表示値（AC-02-1 / AC-02-2 / AC-03-3） ----------------------------------


def total_distance(candidate: Candidate) -> str:
    """合計距離（AC-02-1）。キロメートル、小数第2位まで。

    `total_m` を使う。ループ距離を表示すると、接近距離のある起点で
    「走る距離」と食い違う（design.md 2.2）。
    """
    return f"合計距離 {candidate.total_m / _METRES_PER_KM:.2f} km"


def distance_error(candidate: Candidate) -> str:
    """距離誤差（AC-02-2）。メートル単位、符号付き。

    キロメートルに直さない。誤差は走行中に調整する量なので、
    0.12 km ではなく 125 m のほうが行動に直結する。
    """
    return f"目標との差 {candidate.error_m:+.0f} m"


def ascent(candidate: Candidate) -> str:
    """獲得標高（AC-03-3）。**ループ区間のみである注記を必ず付ける。**

    接近区間の標高はレスポンスに存在せず加算できない（design.md 4.5）。
    注記なしで出すと、接近距離が大きい起点で実態と合わない値になる。
    """
    return f"獲得標高 {candidate.ascent_m:.0f} m（周回部分のみ。起点との往復は含みません）"


# 方向転換の向きの表記（AC-04-2）。**`TurnDirection` と1対1にする。**
# 辞書に無い向きがあれば `KeyError` で気づける（黙って無表記にしない）
_DIRECTION_LABELS: Final[dict[TurnDirection, str]] = {
    TurnDirection.TURN_LEFT: "左折",
    TurnDirection.TURN_RIGHT: "右折",
    TurnDirection.SHARP_LEFT: "鋭角左折",
    TurnDirection.SHARP_RIGHT: "鋭角右折",
    TurnDirection.SLIGHT_LEFT: "緩い左折",
    TurnDirection.SLIGHT_RIGHT: "緩い右折",
}


def checkpoint_line(checkpoint: Checkpoint) -> str:
    """チェックポイント1件の表示（AC-04-2 / AC-04-4）。

    **標準形は requirements.md AC-04-4 が文面で定めている**——
    「起点から 1.2km 地点を左折」。単位（キロメートル）も桁（小数第1位）も
    そこに書かれているので、ここで変えない。

    地点名は取得できた場合のみ併記する。名前がないことは異常ではなく、
    実測では 71/71 = 100% が名前なしで**こちらが通常の経路**である
    （design.md 7.1）。`None` を文字列にせず、括弧ごと出さない。

    **地図の吹き出しと一覧の両方がこの1か所を使う**（`ui/map_view.py` /
    `ui/app.py`）。表記を画面側に置くと、AC-04-2 が定める内容が2か所に分かれる。
    """
    label = _DIRECTION_LABELS[checkpoint.direction]
    km = checkpoint.distance_from_origin_m / _METRES_PER_KM
    text = f"{checkpoint.order}. 起点から {km:.1f} km 地点を{label}"
    if checkpoint.name is not None:
        text += f"（{checkpoint.name}）"
    return text


# --- 調整の案内（AC-02-3 / AC-02-4） ----------------------------------------


def adjustment_advice(candidate: Candidate) -> str:
    """走行中の調整の案内。符号で切り替わる（AC-02-3 / AC-02-4）。

    ちょうど 0 の分岐は要件が定めていない境界で、設計で埋めた（design.md 9.1 の行、
    9.2 の理由）。どちらかに倒すと「0m ぶん足してください」を出すことになる。
    """
    error_m = candidate.error_m
    if error_m < 0:
        return f"目標まで {-error_m:.0f} m 足りません。起点付近で往復して足してください。"
    if error_m > 0:
        return f"目標より {error_m:.0f} m 長いです。最後の周回を手前で切り上げてください。"
    return "目標距離ちょうどです。このまま走ってください。"


# --- 接近距離の注意（AC-02-5） ----------------------------------------------


def approach_notice(approach_m: float) -> str | None:
    """接近距離の注意（AC-02-5）。50m 以下では `None`（何も出さない）。

    **ずれの向きを必ず伝える。** 接近距離は直線距離で、実際の道のりはこれより
    長い（design.md 4.4）。過小評価であることを言わないと、ユーザーは
    「足りない」と思って余分に走る判断をしてしまう。

    区分の定義元は `models.classify_approach` で、境界（ちょうど 50.0m は OK）は
    そちらが持つ。ここでは同じ定数を参照するだけで、不等号を書き直さない。

    300m 超はそもそも結果を表示しないので（AC-01-3）、この文言は使わない。
    呼び出し側は `origin_rejected()` を出す。
    """
    if approach_m <= APPROACH_OK_M:
        return None
    return (
        f"起点から道路まで {approach_m:.0f} m あります。"
        f"往復ぶん {approach_m * 2:.0f} m を合計距離に含めていますが、"
        "直線距離での計算なので、実際はこれより長くなります。"
        "この起点では距離の精度が落ちるため、走行中の調整はこの差を見込んでください。"
    )


# --- 起点の拒否（AC-01-3、design.md 4.6.1） ---------------------------------


def origin_rejected(approach_m: float) -> str:
    """接近距離が 300m 超（AC-01-3）。**次にすべき操作を名指しする。**

    小数は出さない。420.3m と 420m の差はユーザーの操作を変えない。
    """
    return f"起点が道路から {approach_m:.0f} m 離れています。最寄りの道路上をクリックしてください。"


def origin_no_road() -> str:
    """半径内に道路が見つからない変種（design.md 4.6.1）。**距離を言わない。**

    `snap` が `null` を返したときは測れていない。半径の値も、それらしい距離も
    書かない（観測していない数を画面に出さない）。次にすべき操作は上と同じである。
    """
    return "この地点の近くに道路が見つかりません。最寄りの道路上をクリックしてください。"


def origin_missing() -> str:
    """起点が未指定のまま実行しようとした（AC-05-3）。"""
    return "起点が指定されていません。地図をクリックして起点を指定してください。"


# --- 結論の文言（AC-01-4 / AC-06-3） ----------------------------------------


def compromised() -> str:
    """±300m を満たす候補が0件で、誤差最小の1本を出す場合（AC-01-4）。

    許容誤差そのものを文言に出す。「条件」とだけ言われても、何が満たせなかったのか
    が分からず、目標距離を変えるべきかの判断ができない。
    """
    return (
        f"条件（目標距離との差 ±{TOLERANCE_M:.0f} m 以内）を満たすコースが見つかりませんでした。"
        "最も近い1本を表示しています。目標距離を変えるか、引き直してください。"
    )


def no_candidate() -> str:
    """候補が1件も得られなかった（AC-06-3。異常値除外で0件になった場合も同じ）。"""
    return (
        "コースの候補が得られませんでした。"
        "起点を道路の近くに指定し直すか、もう一度実行してください。"
    )


def stock_exhausted() -> str:
    """在庫を出し切った（AC-08-3）。**黙って同じコースを再表示しない。**

    全滅（`no_candidate`）とは状況が違う。あちらは1本も出せていないが、
    こちらは出したうえで**次が無い**。悲観側の見積もりでは5回に1回は
    引き直しが1回もできない（design.md 6.3）ので、これは異常ではなく
    **通常の経路**である。

    次の行動は「もう一度探す」しかない。引き直しは在庫から出すので
    API を使わないが（AC-08-2）、尽きたあとに新しいコースを得るには
    再実行するしかない。**それを名指しする**（design.md 9.2 / 6.3）。
    """
    return (
        "これ以上の候補がありません。"
        "別のコースが見たいときは、もう一度探すか、目標距離を変えてください。"
    )


# --- 失敗の翻訳（AC-06-1 / AC-06-2 / AC-06-4） ------------------------------

# キーの未設定と無効を同じ文言にするのは、ユーザーがすべきことが両方とも
# 「キーの設定を確かめる」だからである（design.md 9.1「同上」）。
# どちらだったかは行動を変えない。原因の切り分けはログの仕事（9.2）
_API_KEY_TEXT: Final = (
    f"{API_KEY_ENV_NAME} が設定されていないか、無効です。"
    ".env（ローカル実行）または Streamlit の Secrets（Community Cloud）に"
    "設定してください。"
)

# ルートを取れなかった系。起点確定時の1回では画面に出る
# （実行中の欠測は generation.py が吸収するので、ここには来ない）
_UNAVAILABLE_ROUTE_TEXT: Final = (
    "コースを取得できませんでした。起点を少し動かすか、もう一度実行してください。"
)

# 型ごとの翻訳表（design.md 9.1）。**型で引く**のは、例外の種類ごとに
# 次の行動が違うためである（待つ／設定を直す／通信を確かめる）
_FAILURE_TEXTS: Final[dict[type[RouteProviderError], str]] = {
    ApiKeyMissing: _API_KEY_TEXT,
    ApiKeyRejected: _API_KEY_TEXT,
    RateLimited: (
        "地図サービスの呼び出しが混み合っています。1分ほど待ってから、もう一度実行してください。"
    ),
    ProviderUnavailable: (
        "地図サービスに接続できませんでした。通信の状態を確かめて、もう一度実行してください。"
    ),
    RouteNotFound: _UNAVAILABLE_ROUTE_TEXT,
    MalformedRoute: _UNAVAILABLE_ROUTE_TEXT,
}


def provider_failure(error: RouteProviderError) -> str:
    """プロバイダ由来の失敗を文言にする（AC-06-1 / AC-06-2）。

    **例外メッセージを画面に流さない。** ステータス番号は行動を変えず、
    リクエストの中身にはキーが混ざりうる（design.md 9.2、非機能要件・セキュリティ）。
    翻訳は型で引き、表にない型は「取得できませんでした」に寄せる——
    画面を止めないことのほうが、正確な分類より優先する（AC-06-4）。
    """
    error_type = type(error)
    if error_type in _FAILURE_TEXTS:
        return _FAILURE_TEXTS[error_type]
    return _UNAVAILABLE_ROUTE_TEXT


def failure_summary(failures: Mapping[str, int]) -> str | None:
    """全滅の原因を型の名前から文言にする（AC-06-1）。

    `generation.py` は失敗を例外にせず**種類ごとに数えて**返す
    （design.md 4.4）。そのため上位に届くのは例外そのものではなく
    「型の名前 → 件数」であり、`provider_failure` の型で引く表を使えない。
    ここで名前から引き直す。

    **最も多い種類を採る。** 15本のうち 429 が 12 件・404 が 3 件なら、
    伝えるべきは「混み合っている」である。

    知らない名前と、失敗が無い場合は `None`。**それらしい文言を当てない**——
    表に無い名前に既定を当てると実際とは違う原因を案内しうる。呼び出し側は
    `None` のとき AC-06-3 の一般的な文言（`no_candidate`）に落とす。
    """
    if not failures:
        return None
    dominant = max(failures.items(), key=lambda item: item[1])[0]
    for error_type, text in _FAILURE_TEXTS.items():
        if error_type.__name__ == dominant:
            return text
    return None


def unexpected_error() -> str:
    """ドメイン例外に該当しない失敗（AC-06-4、design.md 9.1 の最終行）。

    入力（起点・目標距離）は保たれることを伝える。再実行してよいと分からないと、
    ユーザーは最初からやり直すか諦めるかを選べない。
    """
    return "予期しないエラーが発生しました。入力はそのまま残っています。もう一度実行してください。"
