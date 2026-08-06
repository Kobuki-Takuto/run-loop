"""`OriginStore` の localStorage 実装（design.md 8.1 / 8.4、ADR-0004）。

**ブラウザ側の事情をここだけに閉じる。** 投げる JS 式、コンポーネントの `key`、
戻り値の封筒——このファイルの外にはどれも出ない。`persistence.py` は
「読めた値をどう信じるか」だけを持ち、保存先を知らない。
**8月12日のスパイク5で方式が変わった場合、差し替えるのはこのファイルである。**

**`get_local_storage` ヘルパを使わない**（design.md 8.1）。ヘルパは
「まだ読めていない」と「保存が無い」をどちらも `None` で返して区別できない。
汎用の JS 評価に降りて、**必ず値が返る式**を評価する。

    JSON.stringify({v: localStorage.getItem("runloop.origin.v1")})

戻り値が `None` なら未読、`'{"v":null}'` なら保存なし、`'{"v":"{...}"}'` なら
値あり。3つが別の値になるので判別が成立する（2026-08-05 に実測。FINDINGS
スパイク5）。`v` は JS 側で文字列のまま保たれるので、**Python 側は二重に
パースする**（封筒を解いてから中身を解く）。

**`st.rerun()` を呼ばない**（design.md 8.4）。`setItem` の直後に再実行すると
書き込みが取り消される報告がある（ADR-0004）。そもそもこのモジュールは
Streamlit を import できない（design.md 1.2）ので、構造としても呼べない。

**座標を URL・ログ・エラーメッセージに出さない**（design.md 8.7）。
保存の JS 式には座標が載る（載らなければ保存にならない）ので、
**式そのものをログに出さない。**
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Final, Protocol

from streamlit_js_eval import streamlit_js_eval

from runloop.persistence import (
    CorruptionKind,
    OriginLoad,
    OriginRecord,
    decode,
    encode,
    pending,
)

_LOG = logging.getLogger(__name__)

# localStorage のキー（design.md 8.4）。**版を名前に入れる。**
# 形式を変えたときにキーごと別にできるので、古い値が残ったままでも衝突しない
# （`schema_version` による破棄と二重になるが、こちらは読む前に効く）
STORAGE_KEY: Final = "runloop.origin.v1"

# 封筒の中身のキー。生の `getItem` の結果をこれで包むことで、
# 「未読（コンポーネントが値を返していない）」と「保存なし（`v` が `null`）」が
# **別の値**になる（design.md 8.4）
_ENVELOPE_KEY: Final = "v"

# **必ず値が返る式**。キーは `json.dumps` で JS の文字列リテラルにする
# （素朴に埋め込むと、キーに引用符が入ったときに式が壊れる）
_READ_JS: Final = (
    f"JSON.stringify({{{_ENVELOPE_KEY}: localStorage.getItem({json.dumps(STORAGE_KEY)})}})"
)


class JsEvaluator(Protocol):
    """`streamlit_js_eval` の呼び出し口だけを写した型。

    本体は型情報を同梱していないので、**呼び方をこちらで固定する。**
    キーワード専用にしているのは、本物が `*args` を別の意味
    （`want_output`）に使うためで、位置引数で渡すと静かに解釈が変わる。
    """

    def __call__(self, *, js_expressions: str, key: str) -> object: ...


class LocalStorageOriginStore:
    """`OriginStore` の localStorage 実装（design.md 8.3 の Protocol に適合する）。

    **世代番号（`_generation`）を持つ。** Streamlit のコンポーネントは `key` が
    同じ間は再評価されない。書き込んだあとも同じ `key` のままだと、保存した
    直後の画面が保存前の値を映し続ける（起点を上書きしたのに古い起点が
    残って見える。AC-07-3）。逆に毎回変えると、そのたびに新しい
    コンポーネントになって `None`（未読）から始まり、**永久に `PENDING` の
    ままになる。** どちらも画面上は正常に見えるので、
    「読むだけなら据え置き、書いたら進める」を構造で決める。

    **このオブジェクトは再実行をまたいで生き残る必要がある。** Streamlit は
    操作のたびにスクリプトを頭から流し直すので、`ui/` 側が
    `st.session_state` に1つ置いて使い回す（T17 の配線）。
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        evaluate: JsEvaluator = streamlit_js_eval,
    ) -> None:
        """現在時刻の読み出しを引数で受ける（design.md 8.5、T13 / T14 と同じ判断）。

        `datetime.now()` をここで呼ばないのは、時刻を読む場所を `ui/` の1か所に
        寄せるためと、失効の経路をテストから動かせるようにするため。
        **組み立て時の1回ではなく読み出しのたびに呼ぶ**——Streamlit の
        セッションは何時間も生きるので、開いたままの利用者では失効の判定が
        止まってしまう。
        """
        self._now = now
        self._evaluate = evaluate
        self._generation = 0

    def load(self) -> OriginLoad:
        """保存された起点を読む。**`None` を返さない**（design.md 8.4）。"""
        raw = self._evaluate(
            js_expressions=_READ_JS,
            key=f"runloop_origin_read_{self._generation}",
        )
        if raw is None:
            # 未読。**ここで「保存なし」と判断しない**（AC-07-2 と AC-07-4 が混ざる）
            return pending()
        # 壊れ方の分類（8.6）と失効（8.5 条件3）は `decode` の担当。
        # 同じ規則をこちらにも書くと、片方だけ直したときに静かに食い違う
        return decode(self._unwrap(raw), now=self._now())

    def save(self, record: OriginRecord) -> None:
        """起点とスナップ距離を**同時に**書く（AC-07-1、design.md 8.3）。

        保存する JSON は二重引用符を含むので、`json.dumps` で JS の文字列
        リテラルにしてから埋める。素朴に埋め込むと式が壊れて**黙って保存
        されない**——書き込みは戻り値を見ないので、画面上は成功と区別がつかない。
        """
        script = f"localStorage.setItem({json.dumps(STORAGE_KEY)}, {json.dumps(encode(record))})"
        self._write(script, "save")

    def clear(self) -> None:
        """保存を破棄する（design.md 8.5 条件2 / 8.6）。"""
        self._write(f"localStorage.removeItem({json.dumps(STORAGE_KEY)})", "clear")

    # --- 内側 -----------------------------------------------------------------

    def _write(self, script: str, what: str) -> None:
        """書き込みの式を投げ、世代を進める（次の読み出しで読み直される）。

        **式をログに出さない**（座標が載っている。design.md 8.7）。
        **読み出しの式を混ぜない**——1つの式で読み書きを兼ねると、保存の失敗が
        「読めた値が古い」という形で現れ、どちらが壊れたのか切り分けられない。

        `key` の区切りをハイフンではなく下線にしている。ハイフンだと
        f 文字列の中に `"-"` という定数が現れ、**ORS の「名前なし」の値
        （`"-"`）が `ors/` の外に漏れていないか**を見るテスト
        （`tests/test_ors_mapper.py`）が反応する。あちらの検査は
        f 文字列の中まで見る作りで、それは意図どおり（`f"{name or '-'}"` の
        形の漏れを捕まえるため）なので、こちらの区切りを変える。
        """
        self._evaluate(js_expressions=script, key=f"runloop_origin_{what}_{self._generation}")
        self._generation += 1

    def _unwrap(self, raw: object) -> str | None:
        """封筒から保存された文字列を取り出す（design.md 8.4 の二重パースの1回目）。

        `None` を返すのは「保存が無い」場合と「封筒が壊れていた」場合の2つで、
        **どちらも `decode` が `EMPTY` にする。** 呼び出し側のすることが同じ
        （初回起動と同じ画面を出す。AC-07-4）なので、戻り値では区別しない。
        区別が要るのはログだけで、そちらには分類が出る。

        **壊れていても `PENDING` にしない。** `PENDING` は実行ボタンを無効に
        する状態なので、そこに落とすとアプリが操作を受け付けないまま固まる。
        """
        if not isinstance(raw, str):
            self._discard("コンポーネントの戻り値")
            return None
        try:
            envelope: object = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._discard("読み出しの封筒")
            return None
        if not isinstance(envelope, dict) or _ENVELOPE_KEY not in envelope:
            self._discard("読み出しの封筒")
            return None

        inner: object = envelope[_ENVELOPE_KEY]
        if inner is None:
            # 保存なし（AC-07-4）。**壊れていないのでログを出さない**——
            # 起動のたびに出ると本物の破損（8.6）が埋もれる
            return None
        if not isinstance(inner, str):
            # `getItem` は文字列か `null` しか返さないので、ここに来るのは
            # 封筒の作り方が変わったとき。値ではなく場所だけを記録する
            self._discard(f"読み出しの封筒の {_ENVELOPE_KEY}")
            return None
        return inner

    def _discard(self, where: str) -> None:
        """封筒の破損をログに出す（design.md 8.6 / 8.7）。**分類と場所だけ。**

        黙って捨てる設計ではログが唯一の手がかりになる。値（座標）は出さない。
        分類が `UNREADABLE` なのは、壊れているのが保存された値ではなく
        **こちらが組み立てた封筒**だからで、8.6 の表では「読めない」に当たる。
        """
        _LOG.warning(
            "保存された起点の値を破棄した: 分類=%s 場所=%s",
            CorruptionKind.UNREADABLE.value,
            where,
        )
