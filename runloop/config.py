"""要件由来の定数と、設定の読み込み（design.md 3.3）。

**読めなかった設定に代わりの値を埋めない。足りなければ例外で止める。**
`spike/` の4本は `HOME_LAT` / `HOME_LON` が無いとき市役所あたりの座標へ落ちるが、
本体では同じことをしない。理由はこの事故が**静かに起きる**ため。設定を忘れたまま
起動してもアプリは異常なく動き、返る結果は（その誤った起点については）距離も
獲得標高もすべて正しく計算される。**間違いに気づく手がかりが画面に無い。**
止まる側に倒せば、失敗は起動時に1回、名指しで現れる。

`os.getenv(...) or <定数>` と `.get(key, <それらしい既定値>)` の形をここに置かない。
既定値を持ってよいのは、ベース URL のように**間違っていても結果が壊れず、かつ
間違いに気づける設定**だけである。

閾値（許容誤差・異常値の倍率・接近距離）は `models.py` が定義元で、ここは
**参照して公開するだけ**。`models.py` は何にも依存しないため（design.md 1.3）、
`Candidate` のプロパティから `config.py` を import できないという向きの制約がある。
値を書き写すと、片方だけ直したときに静かに食い違う（T02 の申し送り）。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from dotenv import load_dotenv

from runloop.models import (
    APPROACH_OK_M,
    APPROACH_REJECT_M,
    DEGENERATE_FACTOR,
    TOLERANCE_M,
)
from runloop.ports import ApiKeyMissing

__all__ = [
    "API_KEY_ENV_NAME",
    "APPROACH_OK_M",
    "APPROACH_REJECT_M",
    "CACHE_DRIFT_TOLERANCE_M",
    "CANDIDATE_COUNT",
    "DEGENERATE_FACTOR",
    "MAX_SEND_ATTEMPTS",
    "ORS_BASE_URL",
    "RATE_LIMIT_RETRY_WAIT_S",
    "REQUEST_TIMEOUT_S",
    "SNAP_CACHE_TTL_DAYS",
    "SNAP_RADIUS_M",
    "TOLERANCE_M",
    "Settings",
    "load_settings",
]

# --- 外部 API（design.md 3.3 / 4.3 / 4.5 / 4.6.1） ---------------------------

# ベース URL は既定値を持ってよい。間違っていれば接続が失敗して気づける
ORS_BASE_URL: Final = "https://api.openrouteservice.org"

# キーを探す名前。メッセージにそのまま出す（値は出さない）
API_KEY_ENV_NAME: Final = "ORS_API_KEY"

# 1回の実行で投げる本数。無料枠の消費もこの数で数える（design.md 4.1 / 7節）
CANDIDATE_COUNT: Final = 15

# 実際の送信回数の上限。429 のリトライを含む（design.md 4.3）。
# CANDIDATE_COUNT より大きくないと、1回リトライした時点で上限を破る
MAX_SEND_ATTEMPTS: Final = 18

# 1呼び出しの制限。実測の最大は 2.64 秒で、その約3倍（design.md 4.5。要検証）。
#
# **10秒の性能要件（requirements.md 7節）との関係**（2026-08-07 に追記、要検証 #17）。
# 経路 A は15本を同時に投げるので、実行全体の所要はおおよそ**最も遅い1本**で決まる。
# 8.0 秒 × 1回 = 8.0 秒 < 10 秒なので要件の内側に収まるが、**投げ直すと
# 8 + 8 = 16 秒**になり要件を破る。そのため `ors/client.py` は
# **タイムアウトを投げ直さない**（接続エラーは即座に失敗するので投げ直す）。
#
# **この値そのものが適切かは未確定**（要検証 #17 の A 案）。T19 第4段で
# 所要 8.9 秒（最遅の1本がタイムアウト直前）を観測しており、正常な呼び出しが
# どこまで遅くなりうるかは Community Cloud の実測（要検証 #10）を待つ。
REQUEST_TIMEOUT_S: Final = 8.0

# 429 の待機。回復しなければ欠測として続行する（design.md 4.3）
RATE_LIMIT_RETRY_WAIT_S: Final = 1.0

# `/v2/snap` の半径。公開 API の上限いっぱいで投げる（design.md 4.6.1）。
# 判定は APPROACH_REJECT_M（300m）のままで、300〜350m の起点にも実測値を付ける
SNAP_RADIUS_M: Final = 350

# --- 永続化（design.md 8.5 / 8.5.1） ----------------------------------------

# スナップ距離の失効。道路の新設・廃止で結果が変わりうる（設計判断。実測なし）
SNAP_CACHE_TTL_DAYS: Final = 30

# キャッシュ値と実測値の許容差。これを超えたらキャッシュを破棄する
CACHE_DRIFT_TOLERANCE_M: Final = 10.0


@dataclass(frozen=True)
class Settings:
    """1回の起動で使う設定。

    **起点を持たない。** 起点はブラウザの永続化のみで、環境変数から読む口を
    開けない（design.md 3.3 の表、ADR-0004）。読める口があると、spike と同じ
    フォールバック座標の事故に戻る。

    `api_key` は `repr` に出さない。データクラスの既定の `repr` は全項目を出すため、
    この型が例外メッセージやログに混ざるとキーが漏れる。**型の側で隠す**
    （非機能要件・セキュリティ）。
    """

    api_key: str = field(repr=False)
    base_url: str = ORS_BASE_URL


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """設定を読む。キーが無ければ `ApiKeyMissing`（AC-06-2）。

    `env` に読み出し元を渡す。`ui/` は `st.secrets` を渡す（`runloop/` は
    Streamlit を import できない。design.md 1.2 / 3.3）。省いたときだけ `.env` を
    読み込んで `os.environ` を見る。**渡されたときは `os.environ` に触れない。**

    キーの欠落を**送信の前**に捕まえるのが AC-06-2 の要点である。空文字や空白だけの
    値を通すと、送信して 401 で失敗し「設定が必要」と伝えられない。
    """
    if env is None:
        # 既存の環境変数は上書きしない（Community Cloud の Secrets を尊重する）
        load_dotenv()
        env = os.environ

    if API_KEY_ENV_NAME not in env:
        raise ApiKeyMissing(_missing_key_message())

    api_key = env[API_KEY_ENV_NAME].strip()
    if not api_key:
        raise ApiKeyMissing(_missing_key_message())

    return Settings(api_key=api_key)


def _missing_key_message() -> str:
    """キー欠落のメッセージ。キー名と探した場所を名指しし、値は出さない。

    文言は AC-06-2 の画面にも使うが、**表示用の文言は `messages.py` の責務**
    （T12）。ここは例外メッセージ（ログと開発者向け）に限る。
    """
    return (
        f"{API_KEY_ENV_NAME} が見つかりません。"
        ".env（ローカル実行）または Streamlit の Secrets（Community Cloud）に"
        "設定してください。"
    )
