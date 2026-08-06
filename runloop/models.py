"""データ構造と、要件由来の閾値。

このモジュールは**何にも依存しない**（design.md 1.3）。
そのため閾値の定義元もここに置く。`Candidate.is_within_tolerance` から
`config.py` を import すると依存の向きが逆流するためである。
T04 の `config.py` はここの定数を参照して公開する。

丸めは一切行わない。表示の丸め（AC-02-1 の小数第2位）は `messages.py` の責務で、
判定は生の float で行う。表示のために丸めた値で判定すると、
±300m の境界付近で表示と判定が食い違う（design.md 2.2）。
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

# AC-01-2「合計距離が目標距離の ±300m 以内」。境界を含む（design.md 5.1）
TOLERANCE_M: Final = 300.0

# AC-01-5「合計距離が目標距離の3倍を超える候補は異常値」。ちょうど3倍は超えていない
DEGENERATE_FACTOR: Final = 3

# AC-01-3 の接近距離の区分。「〜50m」が OK、「300m 超」が拒否（design.md 5.1）
APPROACH_OK_M: Final = 50.0
APPROACH_REJECT_M: Final = 300.0


@dataclass(frozen=True)
class LatLon:
    """緯度経度。**丸めない**（AC-05-1「自宅の玄関を正確に起点にしたい」）。"""

    lat: float
    lon: float


@dataclass(frozen=True)
class RouteQuery:
    """1回の実行の条件。在庫の有効性の鍵に使う（design.md 6.2）。"""

    origin: LatLon
    target_m: int
    # ORS に渡す周回の頂点数。本体は 3 で動かす（design.md 7.3）
    points: int = 3
    # AC-03-1「生成されるコースに階段が含まれない」
    avoid_steps: bool = True


class ApproachVerdict(enum.Enum):
    """接近距離の区分（AC-01-3）。REJECT なら結果を表示しない。"""

    OK = "ok"
    WARN = "warn"
    REJECT = "reject"


def classify_approach(approach_m: float) -> ApproachVerdict:
    """接近距離を3区分に分類する（design.md 5.1 の境界値表）。

    境界の等号の入れ方は要件の文面から読み取った判断である。
    ちょうど 50.0m は WARN ではなく OK、ちょうど 300.0m は REJECT ではなく WARN。
    丸めてから比べない（300.4m を 300 と見なすと拒否すべき起点を通してしまう）。
    """
    if approach_m > APPROACH_REJECT_M:
        return ApproachVerdict.REJECT
    if approach_m > APPROACH_OK_M:
        return ApproachVerdict.WARN
    return ApproachVerdict.OK


@dataclass(frozen=True)
class Candidate:
    """1本の候補コース。

    合計距離や距離誤差は**フィールドとして持たず計算プロパティにする**
    （design.md 2.2）。合計距離は判定（AC-01-2）にも表示（AC-02-1）にも使うため、
    別々に組み立てると画面の値と選択が使った値が食い違う。

    `target_m` を候補に焼き付けるのは、`error_m` の算出に目標距離が必要であり、
    外から渡す形にすると呼び出し側が別の目標距離を渡す余地が残るためである。
    1回の実行では目標距離は1つに決まる。

    `approach_m` を候補ごとに持つのは、実測では起点だけで決まる値だが、
    それは ORS の実測事実であってドメインの不変則ではないため
    （プロバイダを替えて前提が崩れても構造が壊れない側に倒す。design.md 2.2）。

    `turns`（方向転換の列）は T11 で追加する。
    """

    seed: int
    # ORS が返した周回そのものの距離。接近区間は含まない
    loop_m: float
    # 起点からスナップ先までの距離。geo.haversine で generation が算出する
    approach_m: float
    # ループ区間のみの獲得標高／下り（AC-03-3 の注記の根拠）
    ascent_m: float
    descent_m: float
    target_m: int
    geometry: tuple[LatLon, ...]

    @property
    def total_m(self) -> float:
        """合計距離（AC-01-2 / AC-02-1）。接近区間は往復するので2回足す。"""
        return self.loop_m + self.approach_m * 2

    @property
    def error_m(self) -> float:
        """距離誤差（AC-02-2）。符号付き。負なら不足、正なら超過。"""
        return self.total_m - self.target_m

    @property
    def abs_error_m(self) -> float:
        """距離誤差の絶対値（AC-01-4 の「誤差最小」の比較に使う）。"""
        return abs(self.error_m)

    @property
    def is_within_tolerance(self) -> bool:
        """在庫に入れてよいか（AC-01-2）。「±300m 以内」なので境界を含む。"""
        return self.abs_error_m <= TOLERANCE_M

    @property
    def is_degenerate(self) -> bool:
        """異常値か（AC-01-5）。「3倍を超える」なのでちょうど3倍は含まない。"""
        return self.total_m > self.target_m * DEGENERATE_FACTOR


# --- プロバイダの境界の値（ports.py の Protocol の戻り値。design.md 3.1） ------


@dataclass(frozen=True)
class SnapResult:
    """`RouteProvider.snap()` の結果（design.md 3.1）。

    起点確定時のプローブ（design.md 4.6.1）で使う。**半径内に道路がない場合は
    この型を返さず `None` を返す**（`snapped_distance_m = 0.0` と区別する。
    圏外の文言には距離を含めないため。design.md 4.6.1 / 10.1）。
    """

    snapped_distance_m: float
    # 道の名前。ORS の `"-"` は `ors/mapper.py` が None に正規化する（AC-04-4）
    name: str | None = None


class Maneuver(enum.Enum):
    """道なりの案内の種別（design.md 7.1）。

    **番号との対応はここに書かない。** ORS は 0〜13 の整数で表すが、それは
    ORS 固有の知識であり `ors/mapper.py` の対応表に閉じる（design.md 1.2）。
    値を `TURN_LEFT = 0` にすると番号がドメインの型に焼き付き、番号体系の違う
    サービスを足すときに `models.py` を触ることになる。

    **どれが方向転換かはここでも `ors/` でも決めない。** AC-04-4 の
    ホワイトリスト（0〜5 に相当する6種のみ）は `checkpoints.py` の責務である
    （T11）。要件由来の規則を、プロバイダの都合を閉じる層に置かないため。

    `KEEP_LEFT` / `KEEP_RIGHT` は分岐でどちら側に留まるかの案内で、
    進行方向は変わらない。`UNKNOWN` は「対応表にない種別が来た」ことを表し、
    **異常ではない**（未観測の種別が来る前提で設計している。design.md 7.1）。
    """

    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    SHARP_LEFT = "sharp_left"
    SHARP_RIGHT = "sharp_right"
    SLIGHT_LEFT = "slight_left"
    SLIGHT_RIGHT = "slight_right"
    STRAIGHT = "straight"
    KEEP_LEFT = "keep_left"
    KEEP_RIGHT = "keep_right"
    DEPART = "depart"
    ARRIVE = "arrive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RawStep:
    """プロバイダが返した案内1件。**そのまま画面に出す形ではない。**

    `distance_m` は**その step 単体の距離**であって累積ではない。累積を積み、
    接近距離のオフセットを足して `Checkpoint` にするのは `checkpoints.py` の
    仕事である（design.md 7.1 / 7.2）。ここで累積に変えると、オフセットを
    足す場所が1か所に定まらなくなる（design.md 2.3）。

    `position` は案内が始まる地点。プロバイダの応答では geometry への添字で
    表されるが、その解決は `ors/mapper.py` が済ませる（添字の扱いは
    ORS 固有の知識であり、上位に出さない）。

    `name` は道の名前。取得できないのが通常で（実測 71/71 = 100% が名前なし。
    design.md 7.1）、`None` は異常ではない（AC-04-4）。
    """

    distance_m: float
    maneuver: Maneuver
    position: LatLon
    name: str | None = None


@dataclass(frozen=True)
class ProviderRoute:
    """プロバイダが返した1本のルート。**ドメインの候補ではなく生の成果である。**

    `Candidate` との違いは2つある。

    1. 接近距離を持たない。接近距離は「起点」というアプリ側の概念との差であり、
       プロバイダの成果物ではない。`generation.py` が
       `geo.haversine(起点, snapped_start)` で算出する（design.md 3.1 / 4.4）
    2. 目標距離を持たない。したがって距離の判定（AC-01-2）もできない。
       判定はドメイン規則であり、プロバイダを替えても変わらない（design.md 3.1）

    `steps` の既定を空にしているのは、**方向転換が0件のルートが異常ではない**
    ため（design.md 7.3「0件のときは何も出さない」）。ただし応答に steps の
    キー自体が無い場合は変換できないので `MalformedRoute` にする（T06）。
    「案内が無いルート」と「案内を読み落とした変換」を区別する。
    """

    seed: int
    # 周回そのものの距離。接近区間は含まない
    loop_m: float
    ascent_m: float
    descent_m: float
    # API がルート始点として返した座標。接近距離の算出元（design.md 2.2）
    snapped_start: LatLon
    geometry: tuple[LatLon, ...]
    # 案内の列。方向転換の抽出と間引きは checkpoints.py の責務（design.md 7.1）
    steps: tuple[RawStep, ...] = ()
    # レスポンスヘッダの残数。画面には出さずログに出す（design.md 3.2）。
    # 取得できなかった場合は None で、代わりの数を埋めない
    ratelimit_remaining: int | None = None


# --- 生成の結果（generation.py の成果。design.md 4.1 / 4.4） -----------------


@dataclass(frozen=True)
class GenerationOutcome:
    """1回の実行で集めた候補と、その過程で観測した事実（design.md 4.1 / 4.4）。

    **候補を絞らない。** 異常値の除外（AC-01-5）も並べ替え（AC-03-2）も
    `selection.py` の責務である。生成側で落とすと、除外件数を数えて AC-06-3 の
    判定に使う経路が作れない（`ports.py` が異常な長さを `MalformedRoute` に
    含めないのと同じ理由）。

    **拒否（`REJECT`）でも候補を空にしない。** 「300m 超なら結果を表示しない」は
    選択の結論であって生成の都合ではなく、候補を0本にして返すと「起点が悪い」と
    「ルートが見つからない」（AC-06-3）が画面上で区別できなくなる（design.md 5.2）。

    `approach_m` と `verdict` が `None` なのは「**1本も測れなかった**」ことを表す。
    0m や `REJECT` で埋めない。0m は「起点が道路の上にある」、`REJECT` は
    「起点が遠い」という別の事実であり、観測できていないことと区別する。
    """

    candidates: tuple[Candidate, ...]
    # 接近距離（AC-01-3 の文言に使う）。1本も応答が得られなければ None
    approach_m: float | None
    verdict: ApproachVerdict | None
    # 失敗の内訳（例外の型の名前 → 件数）。ログに出し、画面には出さない
    failures: Mapping[str, int]
    # directions を呼んだ回数（= 無料枠の消費。非機能要件「15 回以内」）。
    # 429 の投げ直しは枠を消費しないので、ここには現れない（design.md 4.3）
    calls_consumed: int
    # 接近ゲートで2段目を投げずに終えたか（design.md 4.1 の打ち切り）
    aborted_early: bool
