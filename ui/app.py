"""起点の確定フロー（T17）。

design.md 4.6.1 / 4.6.3 / 8.2 / 8.4 / 9.1 / 10.2、requirements.md
AC-01-3 / AC-05-1 / AC-05-3 / AC-06-2 / AC-07-4。

画面は手動確認とする方針（design.md 10.4、CLAUDE.md）。ここに自動テストは無い。

**プロバイダの組み立てをここで行う**（2026-08-07。tasks.md は `config.py` を
想定していたが変更した）。`ors/client.py` が `runloop.config` の定数を
import しているため、`config.py` が `ors/client.py` を import すると
循環 import になる（`config → ors.client → config`）。依存の向きが
上から下（design.md 1.3）の `ui/` はどちらも安全に import できるので、
組み立てをこちらに置く。

**画面の文言のうち、AC が内容を定める案内・エラーは `messages.py` から取る**
（起点未指定・起点拒否・道路なし・プロバイダ失敗）。ページタイトルや
ボタンの見出しのような、AC が文面を定めていない UI の飾りはここに直接書く。
"""

from datetime import UTC, datetime

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from streamlit_folium import st_folium

from runloop import messages
from runloop.config import SNAP_RADIUS_M, Settings, load_settings
from runloop.local_storage import LocalStorageOriginStore
from runloop.models import ApproachVerdict, LatLon, classify_approach
from runloop.ors.client import OrsClient
from runloop.persistence import LoadState, OriginRecord, OriginStore
from runloop.ports import ApiKeyMissing, RouteProviderError
from ui import map_view

st.set_page_config(page_title="RunLoop")
st.title("RunLoop")


def _load_settings() -> Settings | None:
    """設定を読む。キーが無ければ画面に案内を出して `None`（AC-06-2）。

    `st.secrets` は参照しただけでは例外を出さない（遅延評価のプロキシの
    ため）。実際に読もうとした時点（`in` / `[]`）で初めて、`secrets.toml` が
    どこにも無ければ `StreamlitSecretNotFoundError` を送出する（Streamlit
    Community Cloud では存在する）。ローカルではその場合を「渡さなかった」
    ことにして `.env` / `os.environ` に委ねる（design.md 3.3）。
    **`secrets.toml` はあるがキーが無い場合はここにフォールバックしない**
    （`ApiKeyMissing` をそのまま伝える）。渡されたときは `os.environ` に
    触れないという契約（design.md 3.3）に合わせる。
    """
    try:
        return load_settings(st.secrets)
    except StreamlitSecretNotFoundError:
        pass
    except ApiKeyMissing as error:
        st.error(messages.provider_failure(error))
        return None

    try:
        return load_settings(None)
    except ApiKeyMissing as error:
        st.error(messages.provider_failure(error))
        return None


def _store() -> OriginStore:
    """`OriginStore` をセッションをまたいで使い回す。

    Streamlit は操作のたびにスクリプトを頭から流し直すため、コンポーネントの
    世代番号（design.md 8.4）を保つには同じインスタンスを使い回す必要がある。
    """
    if "origin_store" not in st.session_state:
        st.session_state["origin_store"] = LocalStorageOriginStore(
            now=lambda: datetime.now(UTC)
        )
    store: OriginStore = st.session_state["origin_store"]
    return store


def _confirm_origin(point: LatLon, *, settings: Settings, store: OriginStore) -> LatLon | None:
    """起点を確定する（design.md 4.6.1）。確定できればその起点を返す。

    **接近ゲートを通った起点だけを保存する**（design.md 8.2 の不変則）。
    ここで投げるのは `snap()` だけで、directions は1回も送らない。
    """
    provider = OrsClient(settings)
    try:
        snap_result = provider.snap(point, SNAP_RADIUS_M)
    except RouteProviderError as error:
        st.error(messages.provider_failure(error))
        return None

    if snap_result is None:
        st.error(messages.origin_no_road())
        return None

    if classify_approach(snap_result.snapped_distance_m) is ApproachVerdict.REJECT:
        st.error(messages.origin_rejected(snap_result.snapped_distance_m))
        return None

    store.save(
        OriginRecord(
            origin=point,
            snapped_distance_m=snap_result.snapped_distance_m,
            probed_at=datetime.now(UTC),
        )
    )
    return point


settings = _load_settings()
store = _store()
load = store.load()

pending_click: LatLon | None = st.session_state.get("pending_click")

# 表示に使う起点。優先順位は「クリックした未確定の候補」＞「保存済みの値」
# （design.md 8.4 の3状態。PENDING の間はどちらも出さない）
display_origin: LatLon | None = None
if load.state is LoadState.LOADED:
    display_origin = load.origin
if pending_click is not None:
    display_origin = pending_click

# --- レイアウト: 地図の位置を先に確保し、あとから中身を差し込む -------------
# ボタンの判定（押されたか）を地図の描画より前に済ませるための構造。
# 確定した起点をこの回の地図に映すには、地図の中身を作る前に確定処理を
# 終えている必要がある（design.md 8.4 の「読み直すと1手古い起点が出る」）
map_slot = st.empty()

if load.state is LoadState.EMPTY and pending_click is None:
    st.info(messages.origin_missing())

if pending_click is not None:
    confirm_disabled = load.state is LoadState.PENDING or settings is None
    if st.button("この地点を起点にする", disabled=confirm_disabled):
        if settings is not None:
            confirmed = _confirm_origin(pending_click, settings=settings, store=store)
            if confirmed is not None:
                display_origin = confirmed
                st.session_state["pending_click"] = None

with map_slot:
    folium_map = map_view.build_map(origin=display_origin)
    result = st_folium(folium_map, width=700, height=500, key="origin_map")

last_clicked = result.get("last_clicked")
if last_clicked is not None:
    clicked_point = LatLon(lat=last_clicked["lat"], lon=last_clicked["lng"])
    # **クリックした地点そのものが変わったときだけ**候補にする。確定直後は
    # コンポーネントがまだ同じ地点を返し続けるので、これが無いと確定した
    # 起点が即座にまた未確定の候補として現れてしまう
    if clicked_point != st.session_state.get("last_seen_click"):
        st.session_state["last_seen_click"] = clicked_point
        st.session_state["pending_click"] = clicked_point
        st.rerun()
