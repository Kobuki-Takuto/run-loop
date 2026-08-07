"""画面の組み立てとイベント処理（T17: 起点の確定 / T18a: 実行と結果表示）。

design.md 1.3 / 4.6.1 / 4.6.3 / 6.1 / 8.2 / 8.4 / 9.1 / 10.2、requirements.md
AC-01-1 / AC-01-3 / AC-01-4 / AC-02-1〜5 / AC-03-3 / AC-04-1 / AC-05-1〜3 /
AC-06-2 / AC-07-2〜4。

画面は手動確認とする方針（design.md 10.4、CLAUDE.md）。ここに自動テストは無い。
**だからこの層を薄く保つ。** 判断（どれが方向転換か、どの候補を選ぶか、何と
表示するか）はすべて `runloop/` 側にあり、ここは配線と描画だけを持つ。

**プロバイダの組み立てをここで行う**（2026-08-07。tasks.md は `config.py` を
想定していたが変更した）。`ors/client.py` が `runloop.config` の定数を
import しているため、`config.py` が `ors/client.py` を import すると
循環 import になる（`config → ors.client → config`）。依存の向きが
上から下（design.md 1.3）の `ui/` はどちらも安全に import できるので、
組み立てをこちらに置く。

**画面の文言のうち、AC が内容を定める案内・エラー・表示値は `messages.py` から
取る。** ページタイトルや入力欄の見出しのような、AC が文面を定めていない
UI の飾りはここに直接書く。

**状態は `st.session_state` の2つだけ**（design.md 6.1）。`run`（`RunSession`）と
`origin_store`（読み書きの世代を保つため使い回すオブジェクト）。クリックの
一時状態はそれとは別に置くが、どれも**在庫の写しを持たない**——在庫の
出どころを `RunSession` の1つに保つ（片方だけ更新される事故を防ぐ）。
"""

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from streamlit_folium import st_folium

import runloop
from runloop import messages, session
from runloop.checkpoints import select_checkpoints
from runloop.config import SNAP_RADIUS_M, Settings, load_settings
from runloop.generation import generate
from runloop.local_storage import LocalStorageOriginStore
from runloop.models import (
    ApproachVerdict,
    Checkpoint,
    LatLon,
    RouteQuery,
    SelectionOutcome,
    classify_approach,
)
from runloop.ors.client import OrsClient
from runloop.persistence import LoadState, OriginRecord, OriginStore
from runloop.ports import ApiKeyMissing, RouteProviderError
from runloop.selection import select
from runloop.session import RunSession
from ui import map_view

# 実行の所要時間と消費回数をログに出す（非機能要件の実測に使う）。
# **画面には出さない**——どちらもユーザーの行動を変えない（design.md 9.2）
logging.basicConfig(level=logging.INFO)
_LOG = logging.getLogger(__name__)

# 目標距離の入力範囲（km）。要件は範囲を定めていないので、極端な値の扱いを
# T19 で確かめられるよう広く取る（design.md 11節・T19 の完了条件）
_MIN_KM = 0.1
_MAX_KM = 100.0
_DEFAULT_KM = 5.0
_STEP_KM = 0.1
_METRES_PER_KM = 1000.0

st.set_page_config(page_title="RunLoop")
st.title("RunLoop")

# import されるモジュールが置かれた場所。**`ui/app.py` 自身は含めない**
# （メインスクリプトは毎回読み直されるので、更新されていても古くならない）
_MODULE_ROOTS: Final = (
    Path(__file__).resolve().parent.parent / "runloop",
    Path(__file__).resolve().parent,
)
_MAIN_SCRIPT: Final = Path(__file__).resolve()


def _stale_modules() -> list[str]:
    """読み込み済みより新しいソースを探す（design.md 10.4 の手動確認の足場）。

    Streamlit は再実行のたびに**メインスクリプトだけ**を読み直し、
    `import` 済みのモジュールは `sys.modules` に残したままにする。
    そのため古いプロセスに繋がっていると、**画面だけが新しく中身が古い**
    状態になり、実在する関数が「無い」というエラーで落ちる。

    ここで検出して止めれば、`AttributeError` のトレースバックではなく
    **何をすればよいか**が画面に出る（design.md 9.2 と同じ考え方）。

    **この検査自体が古いモジュールに依存してはいけない。** `LOADED_AT` を
    直に読む形にしたら、この仕組みを入れる前の版が載ったプロセスで
    `AttributeError: module 'runloop' has no attribute 'LOADED_AT'` になった
    （2026-08-07 に実際に踏んだ）。**検出したい状態そのもので検出器が壊れる。**
    `getattr` で受けて、無ければ「確実に古い」と結論する。
    """
    loaded_at = getattr(runloop, "LOADED_AT", None)
    if loaded_at is None:
        return ["runloop/__init__.py（この検査を入れる前の版）"]

    stale: list[str] = []
    for root in _MODULE_ROOTS:
        for path in root.rglob("*.py"):
            if path.resolve() == _MAIN_SCRIPT:
                continue
            if path.stat().st_mtime > loaded_at:
                stale.append(path.name)
    return sorted(set(stale))


_stale = _stale_modules()
if _stale:
    st.error(
        "サーバーが古いコードで動いています（更新済み: "
        + "、".join(_stale)
        + "）。ターミナルで Ctrl+C を押して止めてから、"
        "`uv run streamlit run ui/app.py` で起動し直してください。"
    )
    st.stop()


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
        st.session_state["origin_store"] = LocalStorageOriginStore(now=lambda: datetime.now(UTC))
    store: OriginStore = st.session_state["origin_store"]
    return store


def _confirm_origin(
    point: LatLon, *, settings: Settings, store: OriginStore
) -> OriginRecord | None:
    """起点を確定する（design.md 4.6.1）。確定できれば保存した記録を返す。

    **接近ゲートを通った起点だけを保存する**（design.md 8.2 の不変則）。
    ここで投げるのは `snap()` だけで、directions は1回も送らない
    （AC-01-3 の確定時判定。design.md 4.6.3 / 10.2）。
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

    record = OriginRecord(
        origin=point,
        snapped_distance_m=snap_result.snapped_distance_m,
        probed_at=datetime.now(UTC),
    )
    store.save(record)
    # 起点が変われば在庫の前提が変わる（design.md 6.2）。**新しい起点の
    # 画面に古いコースを残さない**——`is_stale` でも落ちるが、確定した
    # 時点で捨てるほうが「いつ消えたか」が1か所に見える
    st.session_state["run"] = None
    return record


def _search(
    *,
    query: RouteQuery,
    settings: Settings,
    cached_approach_m: float | None,
) -> RunSession:
    """15本投げて在庫を作る（AC-01-1。design.md 4.6.2 の2経路）。

    **ここでは例外を捕まえない。** 捕まえるのは呼び出し側の1か所
    （`_run_search`）で、`RouteProviderError` とその他の例外を分けて扱う
    （AC-06-1 / AC-06-4）。捕まえる場所を散らすと、どの経路が画面に
    何を出すのかが追えなくなる。

    所要時間と消費回数をログに出す。非機能要件（10秒以内・15回以内）の
    実測はこのログで行い、**画面には出さない**（design.md 9.2）。
    """
    provider = OrsClient(settings)
    started = time.monotonic()
    outcome = generate(provider, query, cached_approach_m=cached_approach_m)
    elapsed = time.monotonic() - started

    path = "A（キャッシュ利用）" if cached_approach_m is not None else "B（二段投入）"
    _LOG.info(
        "実行: %.1f 秒 / 消費 %d 回 / 候補 %d 本 / 経路%s",
        elapsed,
        outcome.calls_consumed,
        len(outcome.candidates),
        path,
    )
    # **画面の折りたたみにも出す**（2026-08-07 承認）。非機能要件（10秒以内・
    # 15回以内）は実測しないと確かめようがないが、ログが利用者の手元の
    # ターミナルに出るかは環境に依存し、こちらから保証できなかった。
    # design.md 9.2「画面には行動可能な文言だけ」の例外で、**畳んだ中**に置く
    # ことで主要な画面を汚さない。**失敗の内訳は入れない**（design.md 4.4）
    st.session_state["run_log"] = (
        f"所要 {elapsed:.1f} 秒 ／ API 消費 {outcome.calls_consumed} 回 ／ "
        f"候補 {len(outcome.candidates)} 本 ／ 経路{path}"
    )

    if outcome.cache_diverged:
        # 実測がキャッシュから 10m を超えて離れた（design.md 8.5.1）。
        # **このセッションではキャッシュを使わない**ようにして、以後は
        # 経路 B に落とす。保存そのものを消す手段は `OriginStore` に無い
        # （`clear()` は起点も消し、8.5 の「起点は残す」に反する）
        _LOG.warning("スナップ距離のキャッシュが実測と乖離した。以後この実行では使わない")
        st.session_state["cache_diverged"] = True

    return session.start(
        query, outcome, select(outcome), generated_at=datetime.now(UTC)
    )


def _run_search(
    *,
    query: RouteQuery,
    settings: Settings,
    cached_approach_m: float | None,
) -> RunSession | None:
    """実行を1か所で包み、**どの失敗でもアプリを止めない**（AC-06-4）。

    Streamlit は未捕捉の例外でトレースバックを画面に出し、以降の描画を
    止める。それでは「再実行できる状態を保つ」を満たせない（design.md 9.2）。

    **2段に分けて捕まえる。** プロバイダ由来（`RouteProviderError`）は
    種類ごとに次の行動が違うので `messages.provider_failure` が翻訳する
    （AC-06-1）。それ以外は「予期しないエラー」に寄せる（AC-06-4）。
    **例外の型・ステータス・残数はログへ。画面には行動可能な文言だけ**
    （design.md 9.2）。

    失敗しても `st.session_state["run"]` を触らない——**前の結果を残す**
    ほうが、入力を保って再実行できる状態に近い。
    """
    try:
        return _search(
            query=query, settings=settings, cached_approach_m=cached_approach_m
        )
    except RouteProviderError as error:
        # 残数も型名もここでログに落とす（画面には出さない）
        _LOG.warning(
            "実行が失敗した: %s / 残り呼び出し可能数 %s",
            type(error).__name__,
            error.ratelimit_remaining,
        )
        st.error(messages.provider_failure(error))
        return None
    except Exception:
        # **握りつぶさない。** 画面には行動可能な文言を出し、
        # 原因はトレースバックごとログに残す（design.md 9.2）
        _LOG.exception("予期しない失敗で実行を中断した")
        st.error(messages.unexpected_error())
        return None


def _show_result(run: RunSession, checkpoints: tuple[Checkpoint, ...]) -> None:
    """選ばれた1本を表示する（AC-01-4 / AC-02-1〜5 / AC-03-3 / AC-04-1）。

    **文言はすべて `messages.py` から取る。** 表示するかどうかの判断
    （50m 以下では接近距離を出さない、など）も文言側が `None` を返す形で
    持っており、ここでは分岐しない。

    **結論ごとに出すものが違う**（design.md 5.2）。`NO_CANDIDATE` は
    1本も出せていない（AC-06-3）、`ORIGIN_REJECTED` は起点が悪い（AC-01-3）。
    どちらも表示する候補が無いので、文言だけを出して戻る。
    """
    if run.outcome is SelectionOutcome.ORIGIN_REJECTED:
        # 起点が 300m 超。**候補があっても表示しない**（design.md 5.2）。
        # 起点確定時のゲート（T17）を通っていれば通常ここには来ないが、
        # キャッシュを使った経路 A では実測で初めて分かる場合がある
        if run.approach_m is not None:
            st.error(messages.origin_rejected(run.approach_m))
        else:
            st.error(messages.origin_no_road())
        return

    if run.outcome is SelectionOutcome.NO_CANDIDATE:
        # 全滅、または異常値の除外で0件（AC-06-3 / AC-01-5）。
        # **原因が分かるならそれを言う**（AC-06-1）。15本すべてが接続不能で
        # 失敗したのに「起点を道路の近くに指定し直すか」とだけ出すと、
        # 起点は悪くないのに起点を疑わせる（2026-08-07 の実機確認で判明）
        cause = messages.failure_summary(run.failures)
        st.error(cause if cause is not None else messages.no_candidate())
        return

    current = run.current
    if current is None:
        return

    if run.outcome is SelectionOutcome.COMPROMISED:
        # AC-01-4「条件を満たすコースがなかった旨」。1本は出したうえで添える
        st.warning(messages.compromised())

    st.subheader("このコース")
    st.write(messages.total_distance(current))
    st.write(messages.distance_error(current))
    st.write(messages.ascent(current))
    st.info(messages.adjustment_advice(current))

    notice = messages.approach_notice(current.approach_m)
    if notice is not None:
        st.warning(notice)

    if checkpoints:
        # 0件は異常ではない（design.md 7.3）。見出しごと出さない
        st.subheader("チェックポイント")
        for checkpoint in checkpoints:
            st.write(messages.checkpoint_line(checkpoint))


# --- 画面の組み立て ---------------------------------------------------------

settings = _load_settings()
store = _store()
load = store.load()

pending_click: LatLon | None = st.session_state.get("pending_click")

# 起点と、そのスナップ距離（経路 A の入力）。確定した回はその値を優先する——
# `save()` は再実行を起こさず、直後の `load()` は保存前の値を返すため
# （design.md 8.4 の T17 への申し送り）
origin: LatLon | None = load.origin if load.state is LoadState.LOADED else None
cached_approach_m: float | None = load.snapped_distance_m

# 地図の位置を先に確保し、あとから中身を差し込む。ボタンの判定を地図の描画より
# 前に済ませるための構造（確定・実行の結果をこの回の地図に映すため）
map_slot = st.empty()

if load.state is LoadState.EMPTY and pending_click is None:
    st.info(messages.origin_missing())

if pending_click is not None:
    confirm_disabled = load.state is LoadState.PENDING or settings is None
    if st.button("この地点を起点にする", disabled=confirm_disabled) and settings is not None:
        confirmed = _confirm_origin(pending_click, settings=settings, store=store)
        if confirmed is not None:
            origin = confirmed.origin
            cached_approach_m = confirmed.snapped_distance_m
            st.session_state["pending_click"] = None
            pending_click = None

# 表示に使う起点。未確定のクリックがあればそちらを優先して見せる
display_origin = pending_click if pending_click is not None else origin

target_km = st.number_input(
    "目標距離（km）",
    min_value=_MIN_KM,
    max_value=_MAX_KM,
    value=_DEFAULT_KM,
    step=_STEP_KM,
)
target_m = int(round(target_km * _METRES_PER_KM))

run: RunSession | None = st.session_state.get("run")
query = RouteQuery(origin=origin, target_m=target_m) if origin is not None else None

# 条件が変わった在庫は破棄する（design.md 6.2）。目標 5km の在庫は 3km の
# ±300m を満たさず、そこから出すと AC-08-4 が壊れる
if run is not None and query is not None and session.is_stale(run, query):
    run = None
    st.session_state["run"] = None

search_disabled = query is None or load.state is LoadState.PENDING or settings is None

# **「探す」と「引き直し」を同じ画面に並べる**（design.md 6.3）。在庫が
# 尽きたときの次の行動が「もう一度探す」なので、離れた場所にあると辿れない
search_column, reroll_column = st.columns(2)

with search_column:
    search_clicked = st.button("コースを探す", type="primary", disabled=search_disabled)

with reroll_column:
    # 引き直せるのは在庫が2本以上あるときだけ。**在庫が1本でも押せる形にする**
    # ——押した結果「これ以上ない」と伝えるのが AC-08-3 の要求で、
    # ボタンを消すと「尽きた」ことを伝える機会が無くなる
    reroll_clicked = st.button("別のコースを見る", disabled=run is None or not run.stock)

if search_clicked and query is not None and settings is not None:
    run = _run_search(
        query=query,
        settings=settings,
        cached_approach_m=None
        if st.session_state.get("cache_diverged")
        else cached_approach_m,
    )
    # 失敗した回（`None`）では前の結果を残す。入力を保って再実行できる
    # 状態に近い（AC-06-4）
    if run is not None:
        st.session_state["run"] = run
    else:
        run = st.session_state.get("run")

if reroll_clicked and run is not None:
    # **API を呼ばない**（AC-08-2）。在庫の次の1本へカーソルを進めるだけで、
    # `session.reroll` は `models` 以外を import していないので呼ぶ手段がない
    reroll_result = session.reroll(run)
    run = reroll_result.session
    st.session_state["run"] = run
    if not reroll_result.advanced:
        # 在庫の末尾（AC-08-3）。**例外ではなく通常の経路**——悲観側の
        # 見積もりでは5回に1回は引き直しが1回もできない（design.md 6.3）
        st.warning(messages.stock_exhausted())

current_candidate = run.current if run is not None else None
checkpoints: tuple[Checkpoint, ...] = ()
if current_candidate is not None:
    checkpoints = select_checkpoints(
        current_candidate.turns,
        approach_m=current_candidate.approach_m,
        loop_m=current_candidate.loop_m,
    )

with map_slot:
    # `key` を表示するコースごとに変える。同じ `key` のままだとコンポーネントが
    # 再描画されず、探したのに古い地図が残る（AC-01-1 が画面上で満たされない）
    map_key = "map"
    if run is not None and current_candidate is not None:
        map_key = f"map_{run.generated_at.timestamp()}_{run.cursor}"
    result = st_folium(
        map_view.build_map(
            origin=display_origin, candidate=current_candidate, checkpoints=checkpoints
        ),
        width=700,
        height=500,
        key=map_key,
        # **クリックだけを受け取る。** 既定では地図の移動・拡大でも値が返り、
        # そのたびに Streamlit が再実行して地図を作り直すため、**ドラッグした
        # 位置が初期位置に戻る**（2026-08-07 の実機確認で判明）。
        # ここで絞ると、パンとズームでは再実行が起きず操作が保たれる
        returned_objects=["last_clicked"],
    )

if run is not None:
    _show_result(run, checkpoints)

run_log: str | None = st.session_state.get("run_log")
if run_log is not None:
    # 非機能要件（10秒以内・15回以内）を実測するための欄。**畳んでおく**
    with st.expander("実行の記録"):
        st.text(run_log)

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
