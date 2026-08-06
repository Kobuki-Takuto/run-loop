"""config.py のテスト（design.md 3.3、requirements.md AC-06-2）。

このファイルが固定するのは4つ。

1. **要件由来の数値が1か所にあること。** `models.py` にある閾値は再定義せず
   参照し、それ以外のモジュールに数値リテラルとして散らないこと
2. **フォールバック値を置かないこと。** キーが無ければ `ApiKeyMissing` で止まる。
   `os.getenv(...) or <定数>` と `.get(key, <既定値>)` の形を `runloop/` に置かない
   （design.md 3.3。spike のフォールバック座標を本体に持ち込まない）
3. 欠落のメッセージにキー名と探した場所が入り、**値そのものは出ない**
4. 起点を環境変数から読まないこと（起点はブラウザの永続化のみ。ADR-0004）

「散っていない」の検査は AST（構文木）で数値リテラルを集めて行う。
ソースの文字列検索にすると、docstring の「15本投げる」のような**説明文の数字**に
反応してしまうため。AST なら文字列の中身は数値として現れない。
"""

import ast
import dataclasses
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from runloop import config, models
from runloop.config import (
    API_KEY_ENV_NAME,
    CACHE_DRIFT_TOLERANCE_M,
    CANDIDATE_COUNT,
    MAX_SEND_ATTEMPTS,
    ORS_BASE_URL,
    RATE_LIMIT_RETRY_WAIT_S,
    REQUEST_TIMEOUT_S,
    SNAP_CACHE_TTL_DAYS,
    SNAP_RADIUS_M,
    Settings,
    load_settings,
)
from runloop.ports import ApiKeyMissing

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNLOOP = PROJECT_ROOT / "runloop"

# `models.py` が定義元で、`config.py` は参照するだけの閾値（T02 の申し送り）
THRESHOLD_NAMES = ("TOLERANCE_M", "DEGENERATE_FACTOR", "APPROACH_OK_M", "APPROACH_REJECT_M")

# 定数の外に書かれてはいけない数値と、代わりに import すべき名前
BANNED_LITERALS: tuple[tuple[float | int, str], ...] = (
    (15, "CANDIDATE_COUNT"),
    (18, "MAX_SEND_ATTEMPTS"),
    (350, "SNAP_RADIUS_M"),
    (30, "SNAP_CACHE_TTL_DAYS"),
    (8.0, "REQUEST_TIMEOUT_S"),
    (10.0, "CACHE_DRIFT_TOLERANCE_M"),
    (300.0, "TOLERANCE_M / APPROACH_REJECT_M"),
    (50.0, "APPROACH_OK_M"),
)


def _module_ast(path: Path) -> ast.Module:
    """ファイルを構文木にする。"""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _where(path: Path, lineno: int) -> str:
    """違反箇所を `パス:行` で表す。"""
    return f"{path.relative_to(PROJECT_ROOT)}:{lineno}"


def _docstring_ids(tree: ast.Module) -> set[int]:
    """docstring の文字列ノードを識別する。

    docstring も構文木では文字列リテラルとして現れる。**説明文に書かれた名前を
    コードと同じに数えないため**に除く（この差が config.py の解説と実際の
    読み出しを区別する）。
    """
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, holders) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    ids.add(id(first.value))
    return ids


def _string_literals(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """docstring を除く文字列リテラルを (行, 値) で列挙する。"""
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.lineno, node.value


def _numeric_literals(tree: ast.Module) -> Iterator[tuple[int, float | int]]:
    """構文木に現れる数値リテラルを (行, 値) で列挙する。

    文字列（docstring・コメントは構文木に残らない）は対象外。
    `bool` は `int` の派生なので除く（`True` を 1 と数えない）。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            if not isinstance(node.value, bool):
                yield node.lineno, node.value


# --- 閾値は models.py から参照する（T02 の申し送り） -------------------------


@pytest.mark.parametrize("name", THRESHOLD_NAMES)
def test_threshold_is_the_same_object_as_in_models(name: str) -> None:
    """`config.py` の閾値が `models.py` と同一の値であること。

    T02 の実装メモの申し送り。`models.py` は何にも依存しないため定義元をそちらに
    置いており（design.md 1.3）、`config.py` は参照して公開する側になる。
    値を書き写すと、片方だけ直したときに静かに食い違う。
    """
    assert getattr(config, name) == getattr(models, name)


def test_config_imports_thresholds_instead_of_redefining_them() -> None:
    """`config.py` が閾値を**代入で定義していない**こと。

    値が一致するだけのテストでは、同じ数値を書き写しても通ってしまう。
    構文木で「`models` から import している」「代入していない」を見る。
    """
    tree = _module_ast(RUNLOOP / "config.py")

    imported: set[str] = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "runloop.models"
        for alias in node.names
    }
    assigned: set[str] = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }

    assert set(THRESHOLD_NAMES) <= imported, "閾値を models.py から import していない"
    assert set(THRESHOLD_NAMES) & assigned == set(), "閾値を config.py で再定義している"


# --- 要件由来の数値（design.md 4.1 / 4.3 / 4.5 / 4.6.1 / 8.5 / 8.5.1） -------


def test_constants_match_requirements() -> None:
    """定数が要件・設計の数値そのものであること。

    ここが唯一の定義元になる。値を変えることは要件を変えることなので、
    テストが落ちて気づくようにしておく。
    """
    assert CANDIDATE_COUNT == 15  # 1回の実行で投げる本数（design.md 4.1 / 7節）
    assert MAX_SEND_ATTEMPTS == 18  # 429 のリトライを含む送信上限（design.md 4.3）
    assert REQUEST_TIMEOUT_S == 8.0  # 1呼び出しの制限（design.md 4.5）
    assert RATE_LIMIT_RETRY_WAIT_S == 1.0  # 429 の待機（design.md 4.3）
    assert SNAP_RADIUS_M == 350  # 公開 API の上限（design.md 4.6.1）
    assert SNAP_CACHE_TTL_DAYS == 30  # スナップ距離の失効（design.md 8.5 条件3）
    assert CACHE_DRIFT_TOLERANCE_M == 10.0  # キャッシュとの乖離の許容（design.md 8.5.1）


def test_send_limit_leaves_room_for_retries() -> None:
    """送信上限が本数より大きいこと（design.md 4.3）。

    無料枠の消費で数えて15回、実際の送信は 429 のリトライを含めて最大18回。
    ここが逆転すると、リトライした時点で上限を破る。
    """
    assert MAX_SEND_ATTEMPTS > CANDIDATE_COUNT


def test_snap_radius_reaches_beyond_the_reject_threshold() -> None:
    """スナップ半径が拒否の閾値より大きいこと（design.md 4.6.1）。

    半径を上限いっぱいで投げるのは、300〜350m の起点にも**実測値が付く**ため。
    半径を 300 に合わせると、拒否すべき起点が「道路なし」（距離を言えない変種）に
    落ちてしまい、AC-01-3 の文言が距離つきで出せない。
    """
    assert SNAP_RADIUS_M > models.APPROACH_REJECT_M


def test_requirement_numbers_are_not_hardcoded_outside_config() -> None:
    """要件由来の数値が他のモジュールに散っていないこと（design.md 1.2 / 3.3）。

    定義元は `config.py`（閾値は `models.py`）だけ。使う側は import する。
    """
    offenders: list[str] = []
    for path in sorted(RUNLOOP.rglob("*.py")):
        if path.name in {"config.py", "models.py"}:
            continue
        for lineno, value in _numeric_literals(_module_ast(path)):
            for banned_value, constant_name in BANNED_LITERALS:
                # 型も見る。`10` と `10.0` は == で等しいので、値だけでは
                # 無関係な整数を乖離の許容値と誤認する
                if value == banned_value and type(value) is type(banned_value):
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{lineno}: "
                        f"{value}（{constant_name} を import する）"
                    )

    assert offenders == [], "要件由来の数値が定数の外に書かれている: " + ", ".join(offenders)


# --- キーの欠落（AC-06-2、design.md 3.3） -----------------------------------


def test_key_is_read_from_the_given_mapping() -> None:
    """渡された設定からキーを読むこと。

    読み出し元を引数で受けるのは、`ui/` が `st.secrets` を渡せるようにするため。
    `runloop/` は Streamlit を import できない（design.md 1.2）。
    """
    settings = load_settings({API_KEY_ENV_NAME: "test-key-value"})

    assert settings.api_key == "test-key-value"
    assert settings.base_url == ORS_BASE_URL


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_blank_key_is_treated_as_missing(value: str) -> None:
    """空白だけのキーを「設定済み」と扱わないこと。

    `.env` に `ORS_API_KEY=` と書いただけの状態を通すと、送信して 401 で
    失敗する。AC-06-2 は「未設定」を送信の**前**に捕まえることを求めている。
    """
    with pytest.raises(ApiKeyMissing):
        load_settings({API_KEY_ENV_NAME: value})


def test_missing_key_raises_before_any_request() -> None:
    """キーが無ければ `ApiKeyMissing` で止まること（AC-06-2）。

    `load_settings()` の時点で落ちることが「1回も送信しない」の担保になる。
    プロバイダを組み立てる前に失敗するので、送信する経路に到達しない。
    """
    with pytest.raises(ApiKeyMissing):
        load_settings({})


def test_missing_key_message_names_the_key_and_where_it_looked() -> None:
    """メッセージにキー名と探した場所が入ること（design.md 3.3「設定エラーの伝え方」）。"""
    with pytest.raises(ApiKeyMissing) as excinfo:
        load_settings({})

    message = str(excinfo.value)
    assert API_KEY_ENV_NAME in message
    assert ".env" in message
    assert "Secrets" in message


def test_settings_does_not_leak_the_key_in_its_representation() -> None:
    """`Settings` の表示にキーの値が出ないこと（非機能要件・セキュリティ）。

    データクラスの既定の `repr` はすべてのフィールドを出す。`Settings` が
    例外メッセージやログに混ざるのは避けられないので、**型の側で**隠す。
    """
    settings = load_settings({API_KEY_ENV_NAME: "super-secret-value"})

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings)
    assert settings.api_key == "super-secret-value"


# --- フォールバック値を置かない（design.md 3.3） -----------------------------


def test_no_fallback_pattern_in_runloop() -> None:
    """`os.getenv(...) or <定数>` と `.get(key, <既定値>)` が `runloop/` に無いこと。

    この事故は**静かに起きる**。設定を忘れたまま起動してもアプリは正常に見え、
    返る結果は（その誤った前提のもとでは）すべて正しく計算される。
    間違いに気づく手がかりが画面に無いので、止まる側に倒す（design.md 3.3）。
    """
    offenders: list[str] = []
    for path in sorted(RUNLOOP.rglob("*.py")):
        for node in ast.walk(_module_ast(path)):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                if any(_is_lookup_call(value) for value in node.values):
                    offenders.append(f"{_where(path, node.lineno)}: 取得 or 既定値")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) >= 2
            ):
                offenders.append(f"{_where(path, node.lineno)}: .get(key, 既定値)")

    assert offenders == [], "フォールバック値を埋めている: " + ", ".join(offenders)


def _is_lookup_call(node: ast.expr) -> bool:
    """`os.getenv(...)` / `environ.get(...)` の呼び出しか。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in {"getenv", "get"}
    return isinstance(func, ast.Name) and func.id == "getenv"


# --- 起点を環境変数から読まない（ADR-0004、design.md 3.3 の表） --------------


def test_origin_is_not_read_from_the_environment() -> None:
    """起点が設定に含まれないこと。

    起点はブラウザの永続化のみ（8節）。環境変数から読める口を開けると、
    spike と同じフォールバック座標の事故に戻る。
    """
    field_names = {field.name for field in dataclasses.fields(Settings)}

    assert field_names == {"api_key", "base_url"}, "設定が想定外の項目を持っている"


def test_home_coordinates_are_ignored_even_if_present() -> None:
    """`HOME_LAT` / `HOME_LON` が設定にあっても読まないこと。"""
    settings = load_settings(
        {API_KEY_ENV_NAME: "test-key-value", "HOME_LAT": "31.5966", "HOME_LON": "130.5571"}
    )

    assert "31.5966" not in repr(settings)
    assert "130.5571" not in repr(settings)


def test_runloop_never_looks_up_home_coordinates() -> None:
    """`runloop/` のコードが `HOME_LAT` / `HOME_LON` を引かないこと。

    検査は構文木の文字列リテラルに対して行い、**docstring は除く。**
    `config.py` は「spike と同じフォールバックをしない理由」を説明する中で
    この名前に触れており、説明文と実際の読み出しは区別しなければならない。
    """
    offenders: list[str] = []
    for path in sorted(RUNLOOP.rglob("*.py")):
        for lineno, value in _string_literals(_module_ast(path)):
            if "HOME_LAT" in value or "HOME_LON" in value:
                offenders.append(f"{_where(path, lineno)}: {value}")

    assert offenders == [], "起点を環境変数から読もうとしている: " + ", ".join(offenders)


def test_load_settings_does_not_touch_the_process_environment() -> None:
    """設定を渡したとき `os.environ` を書き換えないこと。

    テストが互いに影響しないようにするためと、`ui/` が `st.secrets` を
    渡す経路（T17）で環境変数を汚さないため。
    """
    before = dict(os.environ)

    load_settings({API_KEY_ENV_NAME: "test-key-value"})

    assert dict(os.environ) == before


def test_settings_is_frozen() -> None:
    """設定を後から書き換えられないこと（design.md 2.1 と同じ方針）。"""
    settings = load_settings({API_KEY_ENV_NAME: "test-key-value"})

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.api_key = "other"  # type: ignore[misc]
