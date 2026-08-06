"""T15 の手動確認スクリプト（AC-07-1〜4）。使い捨て。

**本体の `LocalStorageOriginStore` をそのまま呼ぶ。** スパイク5（2026-08-05）は
使い捨てのコードで localStorage の挙動を確かめたが、本体のクラスでの実測は
まだ無い。ここを埋めるのが目的なので、**このファイルにブラウザ側の知識を
書かない**（保存キーも JS 式も `runloop/local_storage.py` が持つ）。

    uv run streamlit run spike/localstorage/check_store.py

design.md 10.4 のとおり localStorage への実際の読み書きは自動テストしない。
その代わりに確かめることを画面に出す:

1. `PENDING` → `EMPTY`（または `LOADED`）に切り替わる（design.md 8.4）
2. 保存すると次の読み出しで `LOADED` になる（AC-07-1）
3. タブを閉じて開き直しても `LOADED` のまま（AC-07-2）
4. 別の座標で保存すると上書きされる（AC-07-3）
5. 破棄すると `EMPTY` に戻る（AC-07-4 の入口）
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# `streamlit run` はスクリプトのあるディレクトリを sys.path に入れるが、
# プロジェクト直下は入れない（pytest の pythonpath 設定はここには効かない）。
# spike → runloop の向きにだけ依存する（T05 と同じ規律）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from runloop.local_storage import STORAGE_KEY, LocalStorageOriginStore  # noqa: E402
from runloop.models import LatLon  # noqa: E402
from runloop.persistence import LoadState, OriginRecord  # noqa: E402

st.set_page_config(page_title="T15 手動確認", page_icon="📍")
st.title("T15: 起点の永続化（手動確認）")

# **ストアは再実行をまたいで生かす。** 世代番号（読み直しの制御）を持つので、
# 毎回作り直すと書き込んだあとに読み直されない。T17 の `ui/app.py` も同じ形にする。
if "origin_store" not in st.session_state:
    st.session_state["origin_store"] = LocalStorageOriginStore(now=lambda: datetime.now(UTC))
store: LocalStorageOriginStore = st.session_state["origin_store"]

load = store.load()

st.subheader(f"状態: {load.state.value}")
if load.state is LoadState.PENDING:
    st.warning(
        "PENDING — 読み出し未完了。**ここで「保存なし」と判断してはいけない**（design.md 8.4）。"
        "一瞬で次の状態に切り替われば正常。切り替わらないならコンポーネントが返っていない"
    )
elif load.state is LoadState.EMPTY:
    st.info("EMPTY — 読み出し完了・保存なし。起点の指定を促す画面を出してよい状態（AC-07-4）")
else:
    st.success("LOADED — 保存された起点が復元された（AC-07-2）")
    st.write("起点:", load.origin)
    st.write(
        "スナップ距離:",
        load.snapped_distance_m if load.snapped_distance_m is not None else "（無効・失効）",
    )

st.divider()

# 既定値は自宅ではない座標（鹿児島中央駅付近）。**このファイルに自宅座標を書かない**
lat = st.number_input("lat", value=31.5830000, format="%.7f")
lon = st.number_input("lon", value=130.5410000, format="%.7f")
distance = st.number_input("snapped_distance_m", value=0.7, min_value=0.0, max_value=300.0)

if st.button("この起点を保存する", type="primary"):
    store.save(
        OriginRecord(
            origin=LatLon(lat=lat, lon=lon),
            snapped_distance_m=distance,
            probed_at=datetime.now(UTC),
        )
    )
    # **`st.rerun()` を呼ばない**（`setItem` が取り消される報告がある。design.md 8.4）
    st.caption("書き込みを投げた。下の「読み直す」を押すか、ページを再読み込みして確認する")

if st.button("破棄する"):
    store.clear()
    st.caption("削除を投げた。「読み直す」を押して EMPTY に戻ることを確認する")

st.button("読み直す")

st.divider()
st.caption(
    f"開発者ツール（F12）→ Application → Local Storage に `{STORAGE_KEY}` が"
    "**1つのキーに JSON 1件**として入っていることも確認する（項目ごとに"
    "キーが分かれていないこと。design.md 8.2）。"
    "`stMetricsConfig` などは Streamlit 自身が置くもので、これとは別"
)
