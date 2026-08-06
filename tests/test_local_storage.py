"""local_storage.py のテスト（design.md 8.1 / 8.4 / 8.7、10.4）。

**localStorage への実際の読み書きは自動テストしない**（design.md 10.4）。
ブラウザが本当に値を保持するか（AC-07-1〜3）は手動確認の担当である。
ここで固定するのは、その手前にある**こちら側の組み立て方**——

1. 投げる JS 式（生のキーを読まず、必ず値が返る形にする。8.4）
2. 戻り値の解釈（`None` / `'{"v":null}'` / `'{"v":"{...}"}'` の3分岐と二重パース）
3. コンポーネントの `key` の動かし方（書き込んだら読み直せること）
4. やらないことの固定（`st.rerun()` を呼ばない・ログに座標を出さない）

**フェイクの評価器を挟む。** `streamlit_js_eval` は Streamlit の実行文脈が
無いと意味のある値を返さないが、このモジュールにとってそれは
「文字列を渡すと `object` が返る関数」でしかない。**投げた式と `key` を
記録できれば、上の1〜4はブラウザ無しで検査できる。**

**壊れ方の分類（8.6）と失効の規則（8.5）はここでは繰り返さない。**
`decode` に委ねていること（＝同じ規則が2か所に分かれていないこと）だけを見る。
"""

import ast
import inspect
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from streamlit_js_eval import streamlit_js_eval

from runloop.config import SNAP_CACHE_TTL_DAYS
from runloop.local_storage import STORAGE_KEY, LocalStorageOriginStore
from runloop.models import LatLon
from runloop.persistence import CorruptionKind, LoadState, OriginRecord, OriginStore, encode

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_STORAGE = PROJECT_ROOT / "runloop" / "local_storage.py"
LOGGER_NAME = "runloop.local_storage"

# 小数を長く取るのは**丸めていないこと**を見るため（AC-05-1「自宅の玄関を正確に」）
ORIGIN = LatLon(lat=31.5966123456789, lon=130.5571987654321)

NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
PROBED_AT = NOW - timedelta(hours=1)
SNAPPED_M = 0.7

RECORD = OriginRecord(origin=ORIGIN, snapped_distance_m=SNAPPED_M, probed_at=PROBED_AT)


class FakeJs:
    """`streamlit_js_eval` の代わり。投げられた式と `key` を記録する。

    **キーワード引数でしか受け取らない。** 位置引数で渡す実装は `TypeError` で
    落ちる。本物のコンポーネントは `*args` を別の意味（`want_output`）に使うので、
    渡し方そのものが正しさの一部である。
    """

    def __init__(self, returns: object = None) -> None:
        self.returns = returns
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, js_expressions: str, key: str) -> object:
        self.calls.append((js_expressions, key))
        return self.returns

    @property
    def scripts(self) -> list[str]:
        """投げた JS 式だけを並べる。"""
        return [js for js, _ in self.calls]

    @property
    def keys(self) -> list[str]:
        """使った `key` だけを並べる。"""
        return [key for _, key in self.calls]


def envelope(inner: str | None) -> str:
    """コンポーネントが返す封筒を作る（design.md 8.4 の表）。

    `v` は**文字列のまま**入る。ここで `json.dumps` に文字列を渡していることが、
    Python 側の二重パースが必要になる理由そのものである。
    """
    return json.dumps({"v": inner})


def store(js: FakeJs, *, now: datetime = NOW) -> LocalStorageOriginStore:
    """テスト用の組み立て。現在時刻は呼び出し側が決める（design.md 8.5）。"""
    return LocalStorageOriginStore(evaluate=js, now=lambda: now)


def _module_ast(path: Path) -> ast.Module:
    """ファイルを構文木にする（test_persistence.py と同じ手口）。"""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_names(tree: ast.Module) -> set[str]:
    """呼び出されている関数・メソッドの名前を集める（属性名だけを見る）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    """import されている名前を集める（`from x import y` の `y` を含む）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
            if node.module is not None:
                names.add(node.module)
    return names


# --- 1. 3状態（design.md 8.4） ------------------------------------------------


def test_component_returning_none_is_pending() -> None:
    """コンポーネントの `None` は「未読」であって「保存なし」ではない（8.4）。

    初回のスクリプト実行では必ずこれが返る。`EMPTY` と解釈すると、毎回の起動で
    AC-07-4 の案内が一瞬出てから復元表示に切り替わる（AC-07-2 と混ざる）。
    """
    js = FakeJs(returns=None)

    load = store(js).load()

    assert load.state is LoadState.PENDING
    assert load.origin is None
    assert load.snapped_distance_m is None


def test_null_value_is_empty() -> None:
    """`v` が `null`（キーが無い）なら「保存なし」（AC-07-4）。

    `PENDING` との違いは**コンポーネントが値を返したかどうか**だけである。

    対照（値がある封筒）を並べるのは、**常に `EMPTY` を返す実装**でも
    片方だけなら通ってしまうため。
    """
    js = FakeJs(returns=envelope(None))

    load = store(js).load()
    stored = store(FakeJs(returns=envelope(encode(RECORD)))).load()

    assert load.state is LoadState.EMPTY
    assert load.origin is None
    assert stored.state is not LoadState.EMPTY


def test_stored_value_is_loaded() -> None:
    """`v` が文字列なら復元する（AC-07-2）。**座標を丸めない**（AC-05-1）。

    封筒の中身は `encode` の出力そのもの。ここが通ることで
    **二重パース**（封筒を解いてから中身を解く）が固定される——
    一重で済ませる実装は `{"v": "..."}` を保存された値そのものとして読み、
    `schema_version` が無い値として捨てる。
    """
    js = FakeJs(returns=envelope(encode(RECORD)))

    load = store(js).load()

    assert load.state is LoadState.LOADED
    assert load.origin == ORIGIN
    assert load.snapped_distance_m == SNAPPED_M


def test_inner_value_is_a_string_in_the_envelope() -> None:
    """封筒の `v` が**文字列**であること（design.md 8.4 の実測表）。

    テスト自身の前提の確認である。`v` にオブジェクトが入る形を想定して
    テストを書くと、二重パースを検査したつもりで一重の実装が通ってしまう。
    """
    raw = envelope(encode(RECORD))

    outer = json.loads(raw)

    assert isinstance(outer["v"], str)


def test_load_returns_origin_load_in_all_three_states() -> None:
    """3状態のいずれでも `None` を返さない（design.md 8.4、`OriginStore` の約束）。"""
    states = [
        store(FakeJs(returns=None)).load().state,
        store(FakeJs(returns=envelope(None))).load().state,
        store(FakeJs(returns=envelope(encode(RECORD)))).load().state,
    ]

    assert states == [LoadState.PENDING, LoadState.EMPTY, LoadState.LOADED]


# --- 2. 読み出しの JS 式（生のキーを読まない） --------------------------------


def test_read_script_always_returns_a_value() -> None:
    """`getItem` の結果を**包んで**返すこと（design.md 8.4）。

    生のキーを読むと、キーが無いときの `null` が Python の `None` になり、
    未読と同じ値になる。**包めば3つが別の値で返る**（上の3テスト）。
    """
    js = FakeJs(returns=None)

    store(js).load()

    assert len(js.scripts) == 1
    script = js.scripts[0]
    assert "JSON.stringify" in script
    assert "localStorage.getItem" in script
    assert STORAGE_KEY in script


def test_storage_key_is_versioned() -> None:
    """保存キーが `runloop.origin.v1`（design.md 8.4 の式）。

    版を名前に入れておくと、形式を変えたときに**古い値を読まずに済ませる**
    逃げ道が残る（`schema_version` による破棄と二重になるが、こちらは
    キーごと別になるので古い値が残ったままでも衝突しない）。
    """
    assert STORAGE_KEY == "runloop.origin.v1"


def test_helper_for_local_storage_is_not_used() -> None:
    """`get_local_storage` ヘルパを使わない（design.md 8.1、T15 の完了条件）。

    ヘルパは「未読」と「保存なし」をどちらも `None` で返して区別できない。
    **汎用の JS 評価に降りられることが `streamlit-js-eval` を選んだ理由**
    （ADR-0004）なので、ヘルパに戻ると選定の利益が消える。
    """
    tree = _module_ast(LOCAL_STORAGE)

    assert "get_local_storage" not in _imported_names(tree)
    assert "get_local_storage" not in _called_names(tree)


# --- 3. 書き込みと破棄（AC-07-1 / AC-07-3） -----------------------------------


def test_save_writes_the_encoded_record() -> None:
    """`save()` が `encode` の出力をそのまま `setItem` で書く（AC-07-1）。

    **`json.dumps` で JS の文字列リテラルにする。** 保存する JSON は
    二重引用符を含むので、素朴に埋め込むと式が壊れて**黙って保存されない**
    （書き込みは戻り値を見ないので、画面上は成功と区別がつかない）。
    """
    js = FakeJs()

    store(js).save(RECORD)

    assert len(js.scripts) == 1
    script = js.scripts[0]
    assert "localStorage.setItem" in script
    assert json.dumps(STORAGE_KEY) in script
    assert json.dumps(encode(RECORD)) in script


def test_save_does_not_rerun() -> None:
    """書き込みの直後に `st.rerun()` を呼ばない（design.md 8.4、T15 の完了条件）。

    `setItem` が取り消される報告がある（ADR-0004）。保存は起点の確定操作で
    行い、その操作が自然に起こす再実行に任せる。

    **構造でも守られている**（このモジュールは Streamlit を import できない。
    design.md 1.2、`tests/test_layering.py`）が、`st` を受け取って呼ぶ形に
    変えれば破れるので、呼び出しの名前としても固定する。
    """
    tree = _module_ast(LOCAL_STORAGE)

    assert "rerun" not in _called_names(tree)
    assert "experimental_rerun" not in _called_names(tree)


def test_clear_removes_the_key() -> None:
    """`clear()` が `removeItem` を投げる（design.md 8.5 条件2 / 8.6 の破棄）。"""
    js = FakeJs()

    store(js).clear()

    assert len(js.scripts) == 1
    script = js.scripts[0]
    assert "localStorage.removeItem" in script
    assert json.dumps(STORAGE_KEY) in script


def test_write_does_not_read() -> None:
    """書き込みの式に `getItem` を混ぜない。

    1つの式で読み書きを兼ねると、保存の失敗が「読めた値が古い」という形で
    現れ、どちらが壊れているのか切り分けられなくなる。

    件数を先に見るのは、**1つも投げない実装**（＝そもそも保存しない）でも
    「`getItem` が無い」は成り立ってしまうため。
    """
    js = FakeJs()

    store(js).save(RECORD)
    store(js).clear()

    assert len(js.scripts) == 2
    assert not any("getItem" in script for script in js.scripts)


# --- 4. コンポーネントの key（読み直せること） -------------------------------


def test_repeated_reads_use_the_same_key() -> None:
    """再実行のたびに `key` を変えない。

    Streamlit は操作のたびにスクリプトを上から流し直す。`key` が毎回変われば
    毎回**新しいコンポーネント**になり、そのたびに `None`（未読）から始まる。
    **永久に `PENDING` のままになり、保存された起点が一度も復元されない。**
    """
    js = FakeJs(returns=None)
    origin_store = store(js)

    origin_store.load()
    origin_store.load()

    assert js.keys[0] == js.keys[1]


def test_key_changes_after_a_write() -> None:
    """書き込んだら読み出しの `key` が変わる（＝読み直される）。

    同じ `key` のままだと JS が再評価されず、**保存した直後の画面が
    保存前の値を映し続ける。** 起点を上書きしたのに古い起点が残って見える
    （AC-07-3 が画面上で成立しない）。
    """
    js = FakeJs(returns=None)
    origin_store = store(js)

    origin_store.load()
    before = js.keys[-1]
    origin_store.save(RECORD)
    origin_store.load()

    assert js.keys[-1] != before


def test_key_changes_after_clear() -> None:
    """破棄したときも同じく読み直される（`clear()` の直後に古い値を返さない）。"""
    js = FakeJs(returns=None)
    origin_store = store(js)

    origin_store.load()
    before = js.keys[-1]
    origin_store.clear()
    origin_store.load()

    assert js.keys[-1] != before


def test_each_write_uses_a_distinct_key() -> None:
    """書き込みの `key` も毎回変える。

    同じ `key` で2回目の `setItem` を投げると、コンポーネントが
    「同じ入力」と見なして評価しない——**2回目の起点の変更が保存されない。**
    """
    js = FakeJs()
    origin_store = store(js)

    origin_store.save(RECORD)
    origin_store.save(RECORD)

    assert js.keys[0] != js.keys[1]


# --- 5. 現在時刻は外から受ける（design.md 8.5 条件3） ------------------------


def test_expiry_uses_the_injected_clock() -> None:
    """失効の判定に、渡された現在時刻を使う（`decode` に委ねている）。

    30日超でスナップ距離だけが失効し、**起点は残る**（design.md 8.5 条件3）。
    対照（1日後）を並べるのは、常に距離を捨てる実装でも片方だけなら
    通ってしまうため。
    """
    js_fresh = FakeJs(returns=envelope(encode(RECORD)))
    js_stale = FakeJs(returns=envelope(encode(RECORD)))

    fresh = store(js_fresh, now=PROBED_AT + timedelta(days=1)).load()
    stale = store(js_stale, now=PROBED_AT + timedelta(days=SNAP_CACHE_TTL_DAYS, seconds=1)).load()

    assert fresh.snapped_distance_m == SNAPPED_M
    assert stale.snapped_distance_m is None
    assert stale.origin == ORIGIN


def test_clock_is_read_at_each_load() -> None:
    """現在時刻を組み立て時ではなく**読み出しのたびに**取る。

    Streamlit のセッションは何時間も生き続ける。組み立て時の1回だけだと、
    アプリを開いたままにした利用者では失効の判定が止まったままになる。
    """
    js = FakeJs(returns=envelope(encode(RECORD)))
    ticks: list[datetime] = []

    def clock() -> datetime:
        ticks.append(NOW)
        return NOW

    origin_store = LocalStorageOriginStore(evaluate=js, now=clock)
    origin_store.load()
    origin_store.load()

    assert len(ticks) == 2


def test_module_does_not_read_the_clock_itself() -> None:
    """モジュールが自分で現在時刻を読まない（時刻の入口を `ui/` に寄せる）。

    T13 / T14 と同じ判断。テストから失効の経路を動かせなくなるため。
    """
    called = _called_names(_module_ast(LOCAL_STORAGE))

    assert "now" not in called
    assert "utcnow" not in called
    assert "time" not in called


def test_does_not_sleep() -> None:
    """読み出しを `time.sleep()` で待たない（design.md 8.4）。

    待ち時間は環境で変わるので、待てば十分という保証がどこにもない。
    「まだ読めていない」は `PENDING` という状態で表す。
    """
    assert "sleep" not in _called_names(_module_ast(LOCAL_STORAGE))


# --- 6. 壊れた封筒（design.md 8.6 の扱いに合わせる） -------------------------


@pytest.mark.parametrize(
    ("returns", "why"),
    [
        ("{", "JSON として読めない"),
        ('{"other":null}', "`v` が無い"),
        ('{"v":123}', "`v` が文字列でも null でもない"),
        ('"just a string"', "最上位が辞書ではない"),
    ],
)
def test_broken_envelope_becomes_empty(returns: str, why: str) -> None:
    """封筒が壊れていたら「保存なし」に落とす（design.md 8.6 と同じ扱い）。

    **`PENDING` にしない。** `PENDING` は実行ボタンを無効にする状態なので、
    そこに落とすとアプリが操作を受け付けないまま固まる。壊れているときの
    正しい復帰先は初回起動と同じ画面である（AC-07-4）。

    対照を並べるのは、**常に `EMPTY` を返す実装**でも壊れた側だけなら
    通ってしまうため。
    """
    load = store(FakeJs(returns=returns)).load()
    intact = store(FakeJs(returns=envelope(encode(RECORD)))).load()

    assert load.state is LoadState.EMPTY, why
    assert load.origin is None
    assert intact.state is LoadState.LOADED


def test_broken_envelope_is_logged_with_its_kind(caplog: pytest.LogCaptureFixture) -> None:
    """封筒の破損はログに分類だけを出す（design.md 8.6 / 8.7）。

    黙って捨てる設計ではログが唯一の手がかりになる。**座標は出さない。**
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    store(FakeJs(returns="{")).load()

    assert len(caplog.records) == 1
    assert CorruptionKind.UNREADABLE.value in caplog.records[0].getMessage()


def test_no_log_for_normal_reads(caplog: pytest.LogCaptureFixture) -> None:
    """正常な読み出し（3状態のいずれも）でログを出さない。

    未読も保存なしも通常の経路である。ここで警告を出すと、起動のたびに
    出続けて本物の破損（8.6）が埋もれる。

    対照（壊れた封筒）を並べるのは、**1件もログを出さない実装**でも
    通ってしまうため。
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    store(FakeJs(returns=None)).load()
    store(FakeJs(returns=envelope(None))).load()
    store(FakeJs(returns=envelope(encode(RECORD)))).load()

    assert caplog.records == []

    store(FakeJs(returns="{")).load()

    assert len(caplog.records) == 1


def test_logs_never_contain_coordinates(caplog: pytest.LogCaptureFixture) -> None:
    """ログに座標を出さない（design.md 8.7）。保存も読み出しも。

    保存する JS 式には座標が載る（載らなければ保存にならない）。
    **その式をログに出さない**ことが、この検査の中身である。

    読ませる値を「`lon` が欠けた壊れた値」にしてあるので、破棄のログが
    1件出る。**その1件が出ていることを先に確かめる**——ログが空のままなら、
    座標が出ないのは当たり前で何も検査していない。
    """
    caplog.set_level(logging.DEBUG)
    js = FakeJs(returns=envelope(json.dumps({"schema_version": 1, "lat": ORIGIN.lat})))
    origin_store = store(js)

    origin_store.load()
    origin_store.save(RECORD)
    origin_store.clear()

    assert caplog.records != []
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert str(ORIGIN.lat) not in logged
    assert str(ORIGIN.lon) not in logged


def test_save_and_clear_do_not_log(caplog: pytest.LogCaptureFixture) -> None:
    """書き込みと破棄はログを出さない（座標が漏れる経路を作らない）。

    投げた件数を先に見るのは、**何もしない実装**でも「ログが空」は
    成り立ってしまうため。
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    js = FakeJs()
    origin_store = store(js)

    origin_store.save(RECORD)
    origin_store.clear()

    assert len(js.calls) == 2
    assert caplog.records == []


# --- 7. ポートへの適合（design.md 8.3） --------------------------------------


def test_store_satisfies_the_port() -> None:
    """`OriginStore` に適合する（`ui/app.py` はポートにしか触らない）。

    `runtime_checkable` はメソッドの有無しか見ないので、静的な適合は
    mypy が担保する（T03 / T14 と同じ）。
    """
    assert isinstance(store(FakeJs()), OriginStore)


def test_real_evaluator_is_the_default() -> None:
    """既定の評価器が `streamlit_js_eval` 本体であること（design.md 8.1）。

    引数で差し替えられるのはテストのためだが、**既定が本物でなければ
    `ui/app.py` がブラウザ側の事情を知ることになる。** どの JS を投げるかは
    このモジュールに閉じる、という 8.1 の趣旨が配線に漏れる。

    差し替えられること自体は他のテスト全部（フェイクを渡している）が示している。
    """
    default = inspect.signature(LocalStorageOriginStore).parameters["evaluate"].default

    assert default is streamlit_js_eval
