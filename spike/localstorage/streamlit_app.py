"""Community Cloud で localStorage が保持されるかを確かめる使い捨てスパイク。

確認するのは ADR-0004「決めるために確かめること」の #1〜#4。

1. アプリを開き直したときに保存値が復元されるか（Community Cloud 上で）
2. 初回描画で design.md 8.4 の判別式が PENDING と EMPTY を分けるか
3. iPhone の Safari（ホーム画面に追加）で保持されるか
4. プライベートブラウズやサイトデータ削除で消えたときの挙動

本体（runloop/）と pyproject.toml には何も足さない。依存はこのディレクトリの
requirements.txt にだけ書く。検証が終わったらディレクトリごと消してよい。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final

import streamlit as st

# 本体の依存に足していないので、ローカルの `uv run mypy .` からは見えない。
# pyproject.toml を触らずに済ませるため、無視指定はこの1行に閉じ込める。
from streamlit_js_eval import streamlit_js_eval  # type: ignore[import-not-found]

KEY: Final[str] = "runloop.spike.v1"
# 生のキーを読むと「未読」も「保存なし」も None になって区別できない（design.md 8.4）。
# 必ず値が返る式を評価し、戻り値が None かどうかで PENDING を切り分ける。
READ_JS: Final[str] = f'JSON.stringify({{v: localStorage.getItem("{KEY}")}})'

# 同じ key のままだと JS が再評価されない。書き込み後に読み直すため世代番号を持つ。
gen: int = st.session_state.setdefault("gen", 0)
raw: Any = streamlit_js_eval(js_expressions=READ_JS, key=f"read{gen}")
st.code(f"戻り値: {raw!r}  型: {type(raw).__name__}  世代: {gen}")

if raw is None:
    st.warning("PENDING — 読み出し未完了。ここで「保存なし」と判断してはいけない")
elif (stored := json.loads(raw)["v"]) is None:
    st.info("EMPTY — 読み出し完了・保存なし（AC-07-4 の画面を出してよい状態）")
else:
    st.success("LOADED — 読み出し完了・値あり（AC-07-2）")
    st.json(json.loads(stored))

st.write("現在時刻（UTC）:", datetime.now(UTC).isoformat(timespec="seconds"))

if st.button("保存する"):
    value: str = json.dumps({"saved_at": datetime.now(UTC).isoformat(timespec="seconds")})
    # setItem の直後に st.rerun() は呼ばない（ADR-0004: 書き込みが取り消される報告あり）。
    js: str = f"localStorage.setItem({json.dumps(KEY)}, {json.dumps(value)})"
    streamlit_js_eval(js_expressions=js, key=f"save{gen}")
    st.session_state["gen"] = gen + 1
    st.caption("書き込みを投げた。「読み直す」を押すか、ページを再読み込みして確認する")

if st.button("削除する"):
    streamlit_js_eval(js_expressions=f"localStorage.removeItem({json.dumps(KEY)})", key=f"del{gen}")
    st.session_state["gen"] = gen + 1

st.button("読み直す")  # 押すと再実行され、新しい世代で読み直される
