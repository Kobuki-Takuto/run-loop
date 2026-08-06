"""起点とスナップ距離の永続化（design.md 8.2〜8.7、ADR-0004）。

このモジュールが持つのは2つ。**読み書きの境界**（`OriginStore`）と、
**読めた値をどう信じるか**（`decode`）である。

**保存先の事情を持たない。** localStorage も `streamlit-js-eval` も出てこない
（実装クラスは T15 で足す）。ここにあるのは design.md 8.6 の分類表と 8.5 条件3の
失効で、どちらも**保存先が変わっても変わらない**判断である。

**壊れていたら黙って捨て、初回起動と同じ状態にする**（design.md 8.6、AC-07-4）。
画面にエラーを出さない。ユーザーに取れる行動が「地図をクリックする」しかなく、
それは初回起動の案内とまったく同じであるため、「保存データが壊れています」は
状態の報告であって行動を変えない（design.md 9.2）。
**ただしログには出す。** 黙って捨てたうえにログもなければ、形式が壊れていること
自体に永久に気づけない。**座標は出さない**（design.md 8.7）。

**現在時刻を読まない。** 失効の判定（design.md 8.5 条件3）に使う「いま」は
引数で受ける。時刻を読む場所を `ui/` の1か所に寄せる（`generation.py` が
キャッシュの有効性を判定しないのと同じ判断。design.md 4.6.2）。

**待たない。** 読み出しの遅れを `time.sleep()` で吸収しない（design.md 8.4）。
待ち時間は環境で変わるので、待てば十分という保証がどこにもない。
「まだ読めていない」を `PENDING` という状態として表し、結論を出さない側に倒す。
"""

import enum
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from runloop.config import SNAP_CACHE_TTL_DAYS
from runloop.models import ApproachVerdict, LatLon, classify_approach

_LOG = logging.getLogger(__name__)

# 保存形式の版（design.md 8.2）。**未知の版は全体を破棄する**（8.5 条件2）。
# プロバイダを替えるとスナップ距離の意味が変わるため、形式の変更と同じ扱いにする
SCHEMA_VERSION: Final = 1

# スナップ距離の寿命（design.md 8.5 条件3）。**30日を超えたら**失効する。
# 「超えたら」なのでちょうど30日は生きている（境界の向きを1か所で決める）
_TTL: Final = timedelta(days=SNAP_CACHE_TTL_DAYS)

# 緯度経度の値域。**要件由来の閾値ではない**ので config.py には置かない
# （±300m や 50m とは種類が違い、変えることが要件の変更にならない）
_LAT_LIMIT: Final = 90.0
_LON_LIMIT: Final = 180.0

# 保存された JSON のキー（design.md 8.2 の表）
_VERSION_KEY: Final = "schema_version"
_LAT_KEY: Final = "lat"
_LON_KEY: Final = "lon"
_DISTANCE_KEY: Final = "snapped_distance_m"
_PROBED_AT_KEY: Final = "probed_at"


class LoadState(enum.Enum):
    """読み出しの3状態（design.md 8.4）。

    `PENDING` と `EMPTY` を分けるのがこの型の目的である。カスタム
    コンポーネントは初回のスクリプト実行で `None` を返し、`getItem()` は
    キーが無いときも `null` を返すため、**「まだ読めていない」と「保存が無い」が
    同じ値になる。** 区別せずに「保存が無い」と解釈すると、毎回の起動で
    AC-07-4 の案内が一瞬出てから復元表示に切り替わる（AC-07-2 と混ざる）。
    """

    # 読み出し未完了。地図は出すが、起点の案内も復元表示もしない
    PENDING = "pending"
    # 読み出し完了・保存なし。起点の指定を促す（AC-07-4）
    EMPTY = "empty"
    # 読み出し完了・値あり。地図に復元表示する（AC-07-2）
    LOADED = "loaded"


class CorruptionKind(enum.Enum):
    """壊れ方の分類（design.md 8.6 の表）。**ログに出るのはこれだけ。**

    黙って捨てる設計なので、ログが唯一の手がかりになる。値（座標・日時）を
    出さずに手がかりを残すには、**どの行に当たったか**を名前で言うしかない。
    """

    # JSON としてパースできない、あるいは辞書ではない
    UNREADABLE = "unreadable"
    # `schema_version` が未知（8.5 条件2）。**全体を破棄する**
    UNKNOWN_VERSION = "unknown_version"
    MISSING_KEY = "missing_key"
    WRONG_TYPE = "wrong_type"
    # `lat` が ±90 の外、`lon` が ±180 の外、スナップ距離が負
    OUT_OF_RANGE = "out_of_range"
    # スナップ距離が 300m 超。8.2 のとおり保存されえない値である
    INVARIANT_VIOLATION = "invariant_violation"
    # `probed_at` がパースできない、時差がない、または未来
    BAD_TIMESTAMP = "bad_timestamp"


@dataclass(frozen=True)
class OriginRecord:
    """**保存するもの**（design.md 8.2 / 8.3）。3項目が同時に書かれる。

    既定値を置かない。置くと「座標だけ保存する」呼び出しが書けてしまい、
    座標だけ新しくスナップ距離が古い組み合わせが作れる。その状態では
    一括並列（design.md 4.6）が誤った起点に対して走る。

    `probed_at` は時差を持つ日時に限る（`encode` が検査する）。
    """

    origin: LatLon
    snapped_distance_m: float
    probed_at: datetime


@dataclass(frozen=True)
class OriginLoad:
    """**読み出した結果**（design.md 8.3 / 8.4）。`OriginRecord` とは制約が違う。

    スナップ距離が欠けうるのが `OriginRecord` との違いである。失効しても
    （8.5 条件3）値が壊れていても（8.6）、**起点だけは復元して**二段投入に
    落ちる。起点の保持は AC-07 の要求で、スナップ距離は速度の最適化にすぎない。

    `snapped_distance_m` が `None` なのは「**使えるキャッシュが無い**」という
    1つの事実である。無い・失効した・壊れていた、のどれであっても呼び出し側の
    することは同じ（経路 B に落ちる）なので、区別を型に持たせない。
    区別が必要な唯一の相手はログで、そちらには分類が出る。
    """

    state: LoadState
    origin: LatLon | None = None
    snapped_distance_m: float | None = None


@runtime_checkable
class OriginStore(Protocol):
    """起点の読み書きの境界（design.md 8.3）。`ui/app.py` はこれにしか触らない。

    `runtime_checkable` にしているのは、テストでフェイクの適合を実行時にも
    確かめるため（T03 と同じ）。**メソッドの有無しか見ない**ので、
    静的な適合は mypy が担保する。
    """

    def load(self) -> OriginLoad:
        """保存された起点を読む。**`None` を返さない**（design.md 8.4）。

        戻り値を `OriginLoad | None` にすると「まだ読めていない」を `None` で
        表す形に戻り、「保存が無い」と区別できなくなる。3状態は型で表す。
        """
        ...

    def save(self, record: OriginRecord) -> None:
        """起点とスナップ距離を**同時に**書く（design.md 8.3）。

        引数が `LatLon` ではなく `OriginRecord` なのは、別々に保存できる形を
        作らないためである。書き込みの直後に `st.rerun()` を呼ばないこと
        （`setItem` が取り消される報告がある。design.md 8.4）。
        """
        ...

    def clear(self) -> None:
        """保存を破棄する。版が不明なとき（8.5 条件2）と壊れているとき（8.6）に使う。"""
        ...


def pending() -> OriginLoad:
    """まだ読めていない状態（design.md 8.4）。

    **`decode` はこれを返せない。** 読み出しが完了したかどうかを知っているのは
    保存先の層だけである（localStorage では「コンポーネントの戻り値が `None`」が
    その事実に当たる）。判定の材料を持たない場所で状態を作らないため、
    ここは呼び出し側（T15）が使う入口として分けている。
    """
    return OriginLoad(state=LoadState.PENDING)


def empty() -> OriginLoad:
    """読み出し完了・保存なし（AC-07-4）。壊れた値を捨てた後もこの状態になる。"""
    return OriginLoad(state=LoadState.EMPTY)


def encode(record: OriginRecord) -> str:
    """保存する JSON 文字列を作る（design.md 8.2）。**座標を丸めない**（AC-05-1）。

    プライバシーのための丸めは要件と衝突する（design.md 8.7）。守るのは
    保存場所を選ぶ側であって、値を鈍らせる側ではない。

    `probed_at` は UTC に直して書く。時差のない日時は**受け付けない**——
    「たぶん UTC」「たぶん現地」のどちらに解釈しても、JST では9時間ずれた瞬間が
    保存される。ずれたまま保存すると読み出し側は毎回「未来の日時」（8.6）として
    スナップ距離を捨て、症状は「キャッシュが毎回効かない」だけになる。
    **原因に辿り着く手がかりが画面にもログにも残らないので、止める側に倒す**
    （design.md 3.3 の判断基準。2026-08-06）。
    """
    if record.probed_at.tzinfo is None or record.probed_at.utcoffset() is None:
        raise ValueError(f"{_PROBED_AT_KEY} に時差がない（UTC の日時を渡すこと）")

    payload: dict[str, object] = {
        _VERSION_KEY: SCHEMA_VERSION,
        _LAT_KEY: record.origin.lat,
        _LON_KEY: record.origin.lon,
        _DISTANCE_KEY: record.snapped_distance_m,
        _PROBED_AT_KEY: record.probed_at.astimezone(UTC).isoformat(),
    }
    return json.dumps(payload)


def decode(stored: str | None, *, now: datetime) -> OriginLoad:
    """保存された文字列を読み出しの結果にする（design.md 8.5 / 8.6）。

    `stored` が `None` なら「読み出しは終わったが値が無い」（`EMPTY`）。
    **未読（`PENDING`）はここでは作れない**（`pending()` の説明を参照）。

    `now` は失効の判定に使う現在時刻。呼び出し側から受け取るのは、時刻を読む
    場所を `ui/` に寄せるためと、失効の経路をテストから動かせるようにするため。

    **段が2つある。** まず座標を読み、読めなければ全体を捨てる。座標が読めたら
    スナップ距離と鮮度を見て、こちらが駄目なら**起点だけを返す**（8.6 の部分復旧）。
    軽い方の失敗（最適化の材料）で重い方（AC-07 の要求）を巻き添えにしない。
    """
    if stored is None:
        return empty()

    try:
        document = _read_document(stored)
        origin = _read_origin(document)
    except _Corrupt as corrupt:
        _log(corrupt)
        return empty()

    try:
        snapped_distance_m = _read_snapped_distance(document)
        probed_at = _read_probed_at(document, now=now)
    except _Corrupt as corrupt:
        _log(corrupt)
        return OriginLoad(state=LoadState.LOADED, origin=origin)

    if now - probed_at > _TTL:
        # 失効（8.5 条件3）。**壊れてはいないのでログに出さない。**
        # 30日ごとに警告が出ると、本物の破損（8.6）が埋もれる
        return OriginLoad(state=LoadState.LOADED, origin=origin)

    return OriginLoad(
        state=LoadState.LOADED,
        origin=origin,
        snapped_distance_m=snapped_distance_m,
    )


# --- 読み取りの中身（design.md 8.6 の分類表がそのまま現れる） -----------------


class _Corrupt(Exception):
    """壊れた値を見つけたことを `decode` に伝える内部の合図。

    **このモジュールの外に出ない。** 上位に例外を投げると、画面にエラーを
    出さない方針（8.6）が破れる。`decode` が捕まえて、破棄の範囲を決める。

    `where` はキー名など**場所だけ**を持つ。値は入れない（design.md 8.7）。
    """

    def __init__(self, kind: CorruptionKind, where: str) -> None:
        super().__init__(f"{kind.value}: {where}")
        self.kind = kind
        self.where = where


def _log(corrupt: _Corrupt) -> None:
    """壊れ方の分類だけをログに出す（design.md 8.6 / 8.7）。

    座標は自宅であり、`probed_at` は「その座標に居た日時」を意味しうる。
    どちらも出さない。分類とキー名だけでも、形式が壊れていることには気づける。
    """
    _LOG.warning(
        "保存された起点の値を破棄した: 分類=%s 場所=%s", corrupt.kind.value, corrupt.where
    )


def _read_document(stored: str) -> dict[str, object]:
    """JSON を辞書として読む。読めなければ `UNREADABLE`。

    版の検査もここで行う（8.5 条件2）。**未知の版は全体を破棄する**ので、
    座標を読むより前に弾く。形式が変わっていれば `lat` の意味も保証されない。
    """
    try:
        document: object = json.loads(stored)
    except (json.JSONDecodeError, ValueError) as error:
        raise _Corrupt(CorruptionKind.UNREADABLE, "保存された JSON") from error
    if not isinstance(document, dict):
        raise _Corrupt(CorruptionKind.UNREADABLE, "保存された JSON の最上位")

    # **版のキーが無い場合も `UNKNOWN_VERSION` にする**（`MISSING_KEY` にしない）。
    # 版が付く前の形式で書かれた値がこの形になるので、伝えたい事実は
    # 「形式が分からない」である。加えて `MISSING_KEY` は部分復旧する失敗
    # （距離や日時の欠落）にも使う分類なので、これに混ぜるとログから
    # **全体を捨てたのか距離だけ捨てたのか**が読み取れなくなる
    if _VERSION_KEY not in document:
        raise _Corrupt(CorruptionKind.UNKNOWN_VERSION, _VERSION_KEY)
    version: object = document[_VERSION_KEY]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise _Corrupt(CorruptionKind.UNKNOWN_VERSION, _VERSION_KEY)
    return document


def _read_origin(document: dict[str, object]) -> LatLon:
    """座標を読む。**ここが読めなければ全体を捨てる**（8.6 の最後の1文）。"""
    lat = _number(_entry(document, _LAT_KEY), _LAT_KEY)
    lon = _number(_entry(document, _LON_KEY), _LON_KEY)
    if abs(lat) > _LAT_LIMIT:
        raise _Corrupt(CorruptionKind.OUT_OF_RANGE, _LAT_KEY)
    if abs(lon) > _LON_LIMIT:
        raise _Corrupt(CorruptionKind.OUT_OF_RANGE, _LON_KEY)
    # **丸めない**（AC-05-1「自宅の玄関を正確に起点にしたい」）
    return LatLon(lat=lat, lon=lon)


def _read_snapped_distance(document: dict[str, object]) -> float:
    """スナップ距離を読む。読めなければ距離だけを捨てる（8.6 の部分復旧）。

    不変則の検査に `classify_approach` を使う（自分で 300 と比べない）。
    保存するのは接近ゲートを通った起点だけなので（8.2）、`REJECT` に分類される
    値は保存されえない。**境界の等号を書き直さない**ことに意味がある——
    ちょうど 300.0m は `WARN`（拒否ではない。design.md 5.1）なので保存されうる。
    ここで「300 以上は違反」と書くと、書いた側と読んだ側で解釈が食い違う。
    """
    distance = _number(_entry(document, _DISTANCE_KEY), _DISTANCE_KEY)
    if distance < 0:
        raise _Corrupt(CorruptionKind.OUT_OF_RANGE, _DISTANCE_KEY)
    if classify_approach(distance) is ApproachVerdict.REJECT:
        raise _Corrupt(CorruptionKind.INVARIANT_VIOLATION, _DISTANCE_KEY)
    return distance


def _read_probed_at(document: dict[str, object], *, now: datetime) -> datetime:
    """プローブ日時を読む。読めない・時差がない・未来なら `BAD_TIMESTAMP`。

    未来を弾くのは、そのままでは失効の判定（8.5 条件3）が永久に成立せず、
    古い値が使われ続けるためである。時差がない日時を弾くのは `encode` と同じ
    理由で、時差を補って解釈すると9時間ずれた瞬間を黙って受け入れることになる。
    """
    value = _entry(document, _PROBED_AT_KEY)
    if not isinstance(value, str):
        raise _Corrupt(CorruptionKind.BAD_TIMESTAMP, _PROBED_AT_KEY)
    try:
        probed_at = datetime.fromisoformat(value)
    except ValueError as error:
        raise _Corrupt(CorruptionKind.BAD_TIMESTAMP, _PROBED_AT_KEY) from error
    if probed_at.tzinfo is None or probed_at.utcoffset() is None:
        raise _Corrupt(CorruptionKind.BAD_TIMESTAMP, _PROBED_AT_KEY)
    if probed_at > now:
        raise _Corrupt(CorruptionKind.BAD_TIMESTAMP, _PROBED_AT_KEY)
    return probed_at


def _entry(document: dict[str, object], key: str) -> object:
    """キーを取り出す。**既定値で埋めない**（design.md 3.3、T04）。

    欠けていることは「壊れた値が保存されている」という事実であり、それらしい
    値を置いて先に進むと、間違いに気づく手がかりが消える。
    """
    if key not in document:
        raise _Corrupt(CorruptionKind.MISSING_KEY, key)
    value: object = document[key]
    return value


def _number(value: object, where: str) -> float:
    """数値として読む。`bool` は `int` の派生なので除く（`True` を 1.0 にしない）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Corrupt(CorruptionKind.WRONG_TYPE, where)
    return float(value)
