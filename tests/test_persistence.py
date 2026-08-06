"""persistence.py のテスト（design.md 8.2〜8.7、10.1 の persistence の4行）。

このファイルが固定するのは**「読めた値をどう信じるか」**である。保存先（localStorage）
の事情は T15 の担当で、ここに出てこない。design.md 8.6 の分類表と 8.5 条件3の失効は
保存先が変わっても変わらないため、純関数として先に固定する。

節の構成。

1. ポート（design.md 8.3 / 8.4）— `load()` が `None` を返さない形であること
2. 3状態（design.md 8.4）— 未読・保存なし・値ありが**別の値**で表されること
3. 書き出しと往復（design.md 8.2、AC-05-1）— 4項目が同時に書かれ、座標を丸めないこと
4. 壊れた値（design.md 8.6）— 分類表の各行が破棄され、**座標だけは救われる**こと
5. 失効（design.md 8.5 条件3）— 30日超でスナップ距離のみ失効し、起点は残ること
6. ログ（design.md 8.6 / 8.7）— 分類だけが出て、座標も値も出ないこと
7. 実装の規律 — 待たない・丸めない・時刻を自分で読まない

**ログの検査を分類ごとに行う。** 「何か警告が出た」だけでは、design.md 8.6 の表の
どの行が起きたのか区別できない。黙って捨てる設計（8.6）では**ログだけが唯一の
手がかり**なので、分類の取り違えは事実上気づけないまま残る。
"""

import ast
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import get_type_hints

import pytest

from runloop.config import SNAP_CACHE_TTL_DAYS
from runloop.models import LatLon
from runloop.persistence import (
    SCHEMA_VERSION,
    CorruptionKind,
    LoadState,
    OriginLoad,
    OriginRecord,
    OriginStore,
    decode,
    empty,
    encode,
    pending,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PERSISTENCE = PROJECT_ROOT / "runloop" / "persistence.py"
LOGGER_NAME = "runloop.persistence"

# 小数を長く取るのは**丸めていないこと**を見るため（AC-05-1「自宅の玄関を正確に」）。
# 5桁に丸める実装（約1mの誤差）でも、桁を落としたテストでは通ってしまう
ORIGIN = LatLon(lat=31.5966123456789, lon=130.5571987654321)

# 現在時刻はテストから渡す（design.md 8.5 の失効判定の入力）
NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
PROBED_AT = NOW - timedelta(hours=1)

# スナップ距離の実測値（FINDINGS スパイク3。自宅座標で 0.7m）
SNAPPED_M = 0.7

TTL = timedelta(days=SNAP_CACHE_TTL_DAYS)


def stored_json(*, drop: str | None = None, **overrides: object) -> str:
    """保存されている JSON 文字列を組む（design.md 8.2 の4項目）。

    各テストは着目する1項目だけを `overrides` で壊す。`drop` はキーの欠落を作る。
    """
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "lat": ORIGIN.lat,
        "lon": ORIGIN.lon,
        "snapped_distance_m": SNAPPED_M,
        "probed_at": PROBED_AT.isoformat(),
    }
    payload.update(overrides)
    if drop is not None:
        del payload[drop]
    return json.dumps(payload)


def _module_ast(path: Path) -> ast.Module:
    """ファイルを構文木にする（test_config.py と同じ手口）。"""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_names(tree: ast.Module) -> set[str]:
    """呼び出されている関数・メソッドの名前を集める。

    `round(...)` は `Name`、`time.sleep(...)` や `datetime.now(...)` は
    `Attribute` として現れる。**属性名だけを見る**ので、
    `from time import sleep` の形でも `sleep` として拾える。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


# --- 1. ポート（design.md 8.3 / 8.4） ----------------------------------------


def test_load_never_returns_none() -> None:
    """`load()` の戻り値が `OriginLoad` そのものであること（design.md 8.4）。

    `OriginLoad | None` にすると「まだ読めていない」を `None` で表す形に戻り、
    「保存が無い」（AC-07-4）と区別できなくなる。区別できないと、毎回の起動で
    起点の案内が一瞬出てから復元表示に切り替わる（AC-07-2 と AC-07-4 が混ざる）。
    """
    hints = get_type_hints(OriginStore.load)

    assert hints["return"] is OriginLoad


def test_store_has_exactly_the_three_operations() -> None:
    """ポートが `load` / `save` / `clear` の3つであること（design.md 8.3）。

    数を固定するのは、あとから「座標だけ保存する」口が生えることを防ぐため
    （下の `test_save_takes_a_whole_record` と対になる）。
    """
    operations = {name for name in vars(OriginStore) if not name.startswith("_")}

    assert operations == {"load", "save", "clear"}


def test_save_takes_a_whole_record() -> None:
    """`save()` が `OriginRecord` を受け取ること（design.md 8.3）。

    `LatLon` を受ける形にすると、座標だけ新しくスナップ距離が古い組み合わせが
    作れてしまい、4.6 の一括並列が誤った起点に対して走る。
    """
    hints = get_type_hints(OriginStore.save)

    assert hints["record"] is OriginRecord


def test_record_cannot_be_built_without_the_snap_distance() -> None:
    """`OriginRecord` の3項目に既定値が無いこと（design.md 8.3）。

    既定値があると「座標だけ保存する」呼び出しが書けてしまい、
    **同時に書かれる**という保証が型から消える。
    """
    fields = {field.name: field for field in dataclasses.fields(OriginRecord)}

    assert set(fields) == {"origin", "snapped_distance_m", "probed_at"}
    for name, field in fields.items():
        assert field.default is dataclasses.MISSING, f"{name} に既定値がある"
        assert field.default_factory is dataclasses.MISSING, f"{name} に既定値がある"


def test_a_fake_store_conforms_to_the_port() -> None:
    """3つのメソッドを持つ実装がポートに適合すること。

    `runtime_checkable` は**メソッドの有無しか見ない**（T03 と同じ）。
    静的な適合は mypy が担保する。
    """

    class FakeStore:
        def load(self) -> OriginLoad:
            return empty()

        def save(self, record: OriginRecord) -> None:
            return None

        def clear(self) -> None:
            return None

    store: OriginStore = FakeStore()

    assert isinstance(store, OriginStore)


def test_a_store_without_clear_does_not_conform() -> None:
    """`clear()` を欠いた実装が適合しないこと。

    破棄の口が無いと、8.5 条件2（版が不明）と 8.6（壊れた値）で
    捨てたはずの値が localStorage に残り続ける。
    """

    class HalfStore:
        def load(self) -> OriginLoad:
            return empty()

        def save(self, record: OriginRecord) -> None:
            return None

    assert not isinstance(HalfStore(), OriginStore)


@pytest.mark.parametrize("kind", [OriginRecord, OriginLoad])
def test_values_are_frozen(kind: type) -> None:
    """読み書きする値が不変であること（design.md 2.1 と同じ方針）。"""
    assert dataclasses.fields(kind)  # データクラスであること
    assert kind.__dataclass_params__.frozen, f"{kind.__name__} が frozen ではない"  # type: ignore[attr-defined]


# --- 2. 3状態（design.md 8.4） ----------------------------------------------


def test_unread_is_pending_and_carries_no_origin() -> None:
    """未読が `PENDING` で、起点を持たないこと（design.md 8.4）。

    `PENDING` に起点が入っていると、読み出しが終わる前に地図へ復元表示が出る。
    この状態で出せるのは地図だけで、起点の案内も復元表示もしない。
    """
    load = pending()

    assert load.state is LoadState.PENDING
    assert load.origin is None
    assert load.snapped_distance_m is None


def test_missing_key_is_empty() -> None:
    """キーが無い（保存なし）が `EMPTY` になること（AC-07-4）。

    `None` を渡すのは「読み出しは終わったが値が無い」という意味である。
    未読との区別は呼び出し側（T15）が付け、ここは受け取った事実を型にする。

    **対照を同じテストに置く。** 「常に `EMPTY` を返す」実装でも
    `EMPTY` の主張だけは通ってしまうため、値があるときは `EMPTY` に
    ならないことを併せて見る。
    """
    load = decode(None, now=NOW)

    assert load.state is LoadState.EMPTY
    assert load.origin is None
    assert load.snapped_distance_m is None
    assert decode(stored_json(), now=NOW).state is not LoadState.EMPTY


def test_stored_value_is_loaded() -> None:
    """値があれば `LOADED` で、起点とスナップ距離が読めること（AC-07-2）。"""
    load = decode(stored_json(), now=NOW)

    assert load.state is LoadState.LOADED
    assert load.origin == ORIGIN
    assert load.snapped_distance_m == SNAPPED_M


def test_the_three_states_are_distinguishable() -> None:
    """3状態が互いに別の値であること（design.md 8.4 の要点）。

    ここが崩れると「まだ読めていない」と「保存が無い」が同じ値になり、
    区別する仕組みを持った意味が消える。
    """
    states = {pending().state, empty().state, decode(stored_json(), now=NOW).state}

    assert states == {LoadState.PENDING, LoadState.EMPTY, LoadState.LOADED}


# --- 3. 書き出しと往復（design.md 8.2、AC-05-1） -----------------------------


def test_encode_writes_all_four_items_at_once() -> None:
    """書き出しに 8.2 の4項目がすべて入ること（design.md 8.3）。

    座標だけ、あるいはスナップ距離だけが書かれる形を作らない。
    片方だけ古い組み合わせは、経路 A が誤った起点に15回使う原因になる（4.6.3）。
    """
    record = OriginRecord(origin=ORIGIN, snapped_distance_m=SNAPPED_M, probed_at=PROBED_AT)

    payload = json.loads(encode(record))

    assert set(payload) == {"schema_version", "lat", "lon", "snapped_distance_m", "probed_at"}
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["snapped_distance_m"] == SNAPPED_M


def test_encode_does_not_round_the_coordinates() -> None:
    """書き出した文字列に座標の全桁が入っていること（AC-05-1）。

    プライバシーのための丸めは要件と衝突する（design.md 8.7）。守るのは
    保存場所を選ぶ側であって、値を鈍らせる側ではない。
    """
    record = OriginRecord(origin=ORIGIN, snapped_distance_m=SNAPPED_M, probed_at=PROBED_AT)

    text = encode(record)

    assert repr(ORIGIN.lat) in text
    assert repr(ORIGIN.lon) in text


def test_round_trip_preserves_the_origin_exactly() -> None:
    """書き出して読み戻すと、座標が**厳密に**一致すること（AC-05-1 / AC-07-2）。"""
    record = OriginRecord(origin=ORIGIN, snapped_distance_m=SNAPPED_M, probed_at=PROBED_AT)

    load = decode(encode(record), now=NOW)

    assert load.state is LoadState.LOADED
    assert load.origin is not None
    assert load.origin.lat == ORIGIN.lat
    assert load.origin.lon == ORIGIN.lon
    assert load.snapped_distance_m == SNAPPED_M


def test_encode_writes_the_timestamp_in_utc() -> None:
    """`probed_at` を UTC の ISO 8601 で書くこと（design.md 8.2）。

    別のオフセットで渡されても同じ瞬間を UTC で書く。オフセットを保ったまま
    書いても読み戻せるが、**保存形式が入力の時計に依存する**状態になり、
    保存された値を目で読んだときに鮮度を判断できない。
    """
    jst = timezone(timedelta(hours=9))
    record = OriginRecord(
        origin=ORIGIN,
        snapped_distance_m=SNAPPED_M,
        probed_at=PROBED_AT.astimezone(jst),
    )

    written = json.loads(encode(record))["probed_at"]

    assert written.endswith("+00:00")
    assert datetime.fromisoformat(written) == PROBED_AT


def test_encode_rejects_a_naive_timestamp() -> None:
    """時差の無い `probed_at` を書き出さないこと（design.md 8.2）。

    タイムゾーンが無い日時を「たぶん UTC」あるいは「たぶん現地」と解釈すると、
    JST では9時間ずれた瞬間が保存される。ずれたまま保存すると読み出し側は
    未来の日時（8.6）として毎回スナップ距離を捨て、**気づく手がかりが無い。**
    """
    record = OriginRecord(
        origin=ORIGIN,
        snapped_distance_m=SNAPPED_M,
        probed_at=PROBED_AT.replace(tzinfo=None),
    )

    with pytest.raises(ValueError):
        encode(record)


# --- 4. 壊れた値（design.md 8.6） -------------------------------------------


@dataclasses.dataclass(frozen=True)
class Corrupt:
    """design.md 8.6 の分類表の1行ぶん。

    `keeps_origin` は「座標だけ救う」（8.6 の部分復旧）に当たるかどうかである。
    起点の復元は US-07 の要求そのもので、スナップ距離は最適化にすぎない。
    **軽い方の失敗で重い方を巻き添えにしない。**
    """

    label: str
    stored: str
    kind: CorruptionKind
    keeps_origin: bool


CORRUPT_CASES: tuple[Corrupt, ...] = (
    # 読めない（JSON としてパースできない、あるいは辞書ではない）
    Corrupt("空文字", "", CorruptionKind.UNREADABLE, keeps_origin=False),
    Corrupt("JSON ではない", "not json at all", CorruptionKind.UNREADABLE, keeps_origin=False),
    Corrupt("辞書ではない", "[1, 2, 3]", CorruptionKind.UNREADABLE, keeps_origin=False),
    Corrupt("文字列", '"31.5966"', CorruptionKind.UNREADABLE, keeps_origin=False),
    # 版が不明（8.5 条件2。**全体を破棄する**。形式の意味が変わっている）
    Corrupt(
        "版が未来",
        stored_json(schema_version=SCHEMA_VERSION + 1),
        CorruptionKind.UNKNOWN_VERSION,
        keeps_origin=False,
    ),
    Corrupt(
        "版が無い",
        stored_json(drop="schema_version"),
        CorruptionKind.UNKNOWN_VERSION,
        keeps_origin=False,
    ),
    Corrupt(
        "版が文字列",
        stored_json(schema_version="1"),
        CorruptionKind.UNKNOWN_VERSION,
        keeps_origin=False,
    ),
    # 欠けている / 型が違う / 範囲外（座標側 → 救えないので全体を破棄）
    Corrupt("lat が無い", stored_json(drop="lat"), CorruptionKind.MISSING_KEY, keeps_origin=False),
    Corrupt("lon が無い", stored_json(drop="lon"), CorruptionKind.MISSING_KEY, keeps_origin=False),
    Corrupt(
        "lat が文字列",
        stored_json(lat="31.5966123456789"),
        CorruptionKind.WRONG_TYPE,
        keeps_origin=False,
    ),
    Corrupt("lon が null", stored_json(lon=None), CorruptionKind.WRONG_TYPE, keeps_origin=False),
    Corrupt("lat が真偽値", stored_json(lat=True), CorruptionKind.WRONG_TYPE, keeps_origin=False),
    Corrupt("lat が北極超", stored_json(lat=90.1), CorruptionKind.OUT_OF_RANGE, keeps_origin=False),
    Corrupt(
        "lat が南極超", stored_json(lat=-90.1), CorruptionKind.OUT_OF_RANGE, keeps_origin=False
    ),
    Corrupt(
        "lon が範囲外", stored_json(lon=180.1), CorruptionKind.OUT_OF_RANGE, keeps_origin=False
    ),
    # スナップ距離側 → **座標は救う**（8.6 の部分復旧）
    Corrupt(
        "距離が無い",
        stored_json(drop="snapped_distance_m"),
        CorruptionKind.MISSING_KEY,
        keeps_origin=True,
    ),
    Corrupt(
        "距離が null",
        stored_json(snapped_distance_m=None),
        CorruptionKind.WRONG_TYPE,
        keeps_origin=True,
    ),
    Corrupt(
        "距離が文字列",
        stored_json(snapped_distance_m="0.7"),
        CorruptionKind.WRONG_TYPE,
        keeps_origin=True,
    ),
    Corrupt(
        "距離が負",
        stored_json(snapped_distance_m=-0.1),
        CorruptionKind.OUT_OF_RANGE,
        keeps_origin=True,
    ),
    # 不変則違反（8.2）。ゲートを通った起点だけを保存するので 300m 超は保存されえない
    Corrupt(
        "距離が 300m 超",
        stored_json(snapped_distance_m=300.1),
        CorruptionKind.INVARIANT_VIOLATION,
        keeps_origin=True,
    ),
    # 日時が不正 → 鮮度が判定できない。距離だけ捨てる
    Corrupt(
        "日時が無い",
        stored_json(drop="probed_at"),
        CorruptionKind.MISSING_KEY,
        keeps_origin=True,
    ),
    Corrupt(
        "日時が読めない",
        stored_json(probed_at="きのう"),
        CorruptionKind.BAD_TIMESTAMP,
        keeps_origin=True,
    ),
    Corrupt(
        "日時が数値",
        stored_json(probed_at=1_754_470_800),
        CorruptionKind.BAD_TIMESTAMP,
        keeps_origin=True,
    ),
    Corrupt(
        "日時に時差が無い",
        stored_json(probed_at=PROBED_AT.replace(tzinfo=None).isoformat()),
        CorruptionKind.BAD_TIMESTAMP,
        keeps_origin=True,
    ),
    Corrupt(
        "日時が未来",
        stored_json(probed_at=(NOW + timedelta(seconds=1)).isoformat()),
        CorruptionKind.BAD_TIMESTAMP,
        keeps_origin=True,
    ),
)

CORRUPT_IDS = [case.label for case in CORRUPT_CASES]


def test_corruption_kinds_match_the_design_table() -> None:
    """分類が design.md 8.6 の表の7行ぶんであること。

    ログに出るのは分類だけなので（8.7）、語彙が増減すると表と対応が取れなくなる。
    """
    assert {kind.name for kind in CorruptionKind} == {
        "UNREADABLE",
        "UNKNOWN_VERSION",
        "MISSING_KEY",
        "WRONG_TYPE",
        "OUT_OF_RANGE",
        "INVARIANT_VIOLATION",
        "BAD_TIMESTAMP",
    }


def test_an_unknown_version_is_reported_before_anything_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """版が未知なら、他の項目の壊れ方を報告しないこと（design.md 8.5 条件2）。

    **2か所が同時に壊れている値で、どちらを言うかが決まる。** 版が違えば
    `lat` の意味そのものが保証されない（8.5 条件2 が全体を破棄する理由）。
    先に座標を読む実装だと、たとえば版2が座標を入れ子で持つ形式に変わったとき、
    ログは毎回「lat が無い」と言い続ける。**直すべき場所を指していない手がかりは、
    無いより悪い**——形式が変わったという事実に辿り着けなくなる。

    故意破壊（版の検査より先に座標を読む）で見つかった穴。分類表の各行を
    1件ずつ壊すテストでは、順序が問われる場面を作れなかった。
    """
    stored = stored_json(schema_version=SCHEMA_VERSION + 1, lat="こわれている")

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        load = decode(stored, now=NOW)

    assert load.state is LoadState.EMPTY
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert CorruptionKind.UNKNOWN_VERSION.value in message
    assert CorruptionKind.WRONG_TYPE.value not in message


@pytest.mark.parametrize("case", CORRUPT_CASES, ids=CORRUPT_IDS)
def test_corrupt_value_never_raises(case: Corrupt) -> None:
    """壊れた値で例外を上げないこと（design.md 8.6）。

    画面にエラーを出さない方針なので、例外で上まで抜けると
    「地図をクリックする」という次の行動そのものが取れなくなる。
    """
    load = decode(case.stored, now=NOW)

    assert load.state in {LoadState.EMPTY, LoadState.LOADED}


@pytest.mark.parametrize("case", CORRUPT_CASES, ids=CORRUPT_IDS)
def test_corrupt_value_discards_the_snap_distance(case: Corrupt) -> None:
    """どの分類でもスナップ距離は使わないこと（design.md 8.5 条件5 / 8.6）。

    **対照を同じテストに置く。** 距離を常に捨てる実装でもこの主張は通るので、
    壊れていない値では距離が残ることを併せて見る（残らないと経路 A が消える）。
    """
    load = decode(case.stored, now=NOW)

    assert load.snapped_distance_m is None
    assert decode(stored_json(), now=NOW).snapped_distance_m == SNAPPED_M


@pytest.mark.parametrize("case", CORRUPT_CASES, ids=CORRUPT_IDS)
def test_corrupt_value_keeps_the_origin_only_when_it_is_sound(case: Corrupt) -> None:
    """座標が妥当なら起点は復元され、そうでなければ全体を捨てること（8.6）。

    **これが 8.6 の部分復旧そのものである。** スナップ距離が壊れているだけで
    起点まで失うと、ユーザーは毎回地図をクリックし直すことになり AC-07-2 が壊れる。
    逆に座標が壊れているのに `LOADED` を返すと、地図に別の場所が出る。
    """
    load = decode(case.stored, now=NOW)

    if case.keeps_origin:
        assert load.state is LoadState.LOADED
        assert load.origin == ORIGIN
    else:
        assert load.state is LoadState.EMPTY
        assert load.origin is None


@pytest.mark.parametrize(
    ("label", "stored"),
    [
        ("距離が 0m（道路の真上）", stored_json(snapped_distance_m=0.0)),
        ("距離がちょうど 300m", stored_json(snapped_distance_m=300.0)),
        ("lat がちょうど北極", stored_json(lat=90.0)),
        ("lat がちょうど南極", stored_json(lat=-90.0)),
        ("lon がちょうど東端", stored_json(lon=180.0)),
        ("lon がちょうど西端", stored_json(lon=-180.0)),
    ],
)
def test_boundary_values_are_not_treated_as_corrupt(label: str, stored: str) -> None:
    """境界の値を壊れていると判定しないこと。

    ちょうど 300.0m は接近ゲートで WARN（拒否ではない。design.md 5.1）なので
    **保存されうる値**である。ここを「300 以上は不変則違反」にすると、
    保存できた値を読み出し側が捨てることになり、書いた側と読んだ側で
    境界の解釈が食い違う。0m は「起点が道路の真上」という正常な実測値。
    """
    load = decode(stored, now=NOW)

    assert load.state is LoadState.LOADED, label
    assert load.snapped_distance_m is not None, label


# --- 5. 失効（design.md 8.5 条件3） -----------------------------------------


def test_snap_distance_expires_after_the_ttl_but_the_origin_remains() -> None:
    """30日を超えたらスナップ距離だけ失効し、起点は残ること（8.5 条件3）。

    起点の保持は AC-07 の要求で、スナップ距離は速度の最適化にすぎない。
    距離を失った実行は二段投入（経路 B）に落ちるだけで、結果は変わらない。
    """
    stored = stored_json(probed_at=(NOW - TTL - timedelta(seconds=1)).isoformat())

    load = decode(stored, now=NOW)

    assert load.state is LoadState.LOADED
    assert load.origin == ORIGIN
    assert load.snapped_distance_m is None


def test_exactly_the_ttl_is_still_fresh() -> None:
    """ちょうど30日は失効しないこと（「30日**超**」の境界）。

    境界の向きを決めておく。逆にすると、失効の判定が
    「経過時間 >= TTL」と「> TTL」のどちらだったかを後から思い出せない。
    """
    stored = stored_json(probed_at=(NOW - TTL).isoformat())

    load = decode(stored, now=NOW)

    assert load.snapped_distance_m == SNAPPED_M


def test_a_fresh_value_survives() -> None:
    """期限内のスナップ距離はそのまま使えること（経路 A の前提。design.md 4.6.2）。"""
    stored = stored_json(probed_at=(NOW - timedelta(days=1)).isoformat())

    load = decode(stored, now=NOW)

    assert load.snapped_distance_m == SNAPPED_M


def test_freshness_is_judged_against_the_given_time() -> None:
    """鮮度が**渡された時刻**で判定されること（design.md 8.5）。

    同じ保存値が、時計を進めた呼び出しでは失効する。現在時刻を自分で読む実装だと
    この対照が作れず、失効の経路をテストから動かせない（T09 / T13 と同じ判断で、
    時刻を読む場所は `ui/` の1か所に寄せる）。
    """
    stored = stored_json(probed_at=PROBED_AT.isoformat())

    assert decode(stored, now=NOW).snapped_distance_m == SNAPPED_M
    assert decode(stored, now=NOW + TTL + timedelta(seconds=1)).snapped_distance_m is None


# --- 6. ログ（design.md 8.6 / 8.7） -----------------------------------------


@pytest.mark.parametrize("case", CORRUPT_CASES, ids=CORRUPT_IDS)
def test_corruption_is_logged_with_its_classification(
    case: Corrupt, caplog: pytest.LogCaptureFixture
) -> None:
    """壊れ方の分類がログに出ること（design.md 8.6）。

    黙って捨てたうえにログもなければ、形式が壊れていること自体に永久に気づけない。
    **分類を取り違えると手がかりの意味が変わる**ので、行ごとに固定する。
    """
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        decode(case.stored, now=NOW)

    messages = [record.getMessage() for record in caplog.records]

    assert len(messages) == 1, f"{case.label}: ログが1件ではない: {messages}"
    assert case.kind.value in messages[0], f"{case.label}: 分類が出ていない: {messages[0]}"


@pytest.mark.parametrize("case", CORRUPT_CASES, ids=CORRUPT_IDS)
def test_logs_do_not_contain_the_saved_values(
    case: Corrupt, caplog: pytest.LogCaptureFixture
) -> None:
    """ログに座標も保存値も出さないこと（design.md 8.7）。

    保存する座標は自宅である。`probed_at` は「その座標に居た日時」を意味しうる
    ので、こちらも出さない。壊れ方の分類とキー名だけで十分に手がかりになる。

    **1件は出ていることを先に確かめる。** 何も出さない実装では「漏れていない」が
    自動的に通り、検査したつもりで通り続ける。
    """
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        decode(case.stored, now=NOW)

    assert caplog.records, f"{case.label}: ログが出ていない"
    text = " ".join(record.getMessage() for record in caplog.records)

    assert "31.596" not in text, case.label
    assert "130.557" not in text, case.label
    assert "2026-08-06" not in text, case.label
    # 空文字はあらゆる文字列の部分文字列なので、保存値そのものの検査から外す
    if case.stored:
        assert case.stored not in text, case.label


def test_a_sound_value_is_not_logged_as_corrupt(caplog: pytest.LogCaptureFixture) -> None:
    """壊れていない値でログを出さないこと。

    正常な読み出しで警告が出ると、本当に見たいログ（壊れ方）が埋もれる
    （`ors/mapper.py` が `"-"` を1件ずつ記録しないのと同じ理由）。

    **対照を同じテストに置く。** 一切ログを出さない実装でもこの主張は通るので、
    壊れた値では出ることを併せて見る。
    """
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        decode(stored_json(), now=NOW)

    assert caplog.records == []

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        decode("not json at all", now=NOW)

    assert caplog.records, "壊れた値でもログが出ていない"


def test_expiry_is_not_reported_as_corruption(caplog: pytest.LogCaptureFixture) -> None:
    """失効を「壊れている」として記録しないこと（design.md 8.5 条件3 / 8.6）。

    失効は**通常の経路**である（起点だけ復元された状態は例外処理ではない。
    design.md 4.6.2）。壊れ方の分類に混ぜると、30日ごとに警告が出て
    本物の破損が埋もれる。

    **失効の経路を通ったことを先に確かめる。** 何も起きない実装では
    「警告が出ない」が自動的に通る。
    """
    stored = stored_json(probed_at=(NOW - TTL - timedelta(days=1)).isoformat())

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        load = decode(stored, now=NOW)

    assert load.origin == ORIGIN
    assert load.snapped_distance_m is None
    assert caplog.records == []


# --- 7. 実装の規律（design.md 8.4 / 8.7、AC-05-1） ---------------------------


def test_persistence_does_not_sleep() -> None:
    """読み出しを `time.sleep()` で待っていないこと（design.md 8.4）。

    待ち時間は環境で変わるので、待てば十分という保証がどこにもない。
    3状態（8.4）で表して、`PENDING` の間は結論を出さない構造にする。
    """
    assert "sleep" not in _called_names(_module_ast(PERSISTENCE))


def test_persistence_does_not_read_the_clock() -> None:
    """現在時刻を自分で読まないこと（design.md 8.5、T09 / T13 と同じ判断）。

    時刻を読む場所を `ui/` の1か所に寄せる。ここで読むと失効の経路を
    テストから動かせず、`now` を渡す引数が飾りになる。
    """
    called = _called_names(_module_ast(PERSISTENCE))

    assert "now" not in called
    assert "utcnow" not in called
    assert "today" not in called
    assert "time" not in called


def test_persistence_does_not_round() -> None:
    """座標を丸める呼び出しが無いこと（AC-05-1、design.md 8.7）。

    往復のテストと二重にする。往復だけだと、書き出し側と読み出し側で
    同じ丸めをしたときに通ってしまう。
    """
    assert "round" not in _called_names(_module_ast(PERSISTENCE))
