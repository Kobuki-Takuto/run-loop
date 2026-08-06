"""候補の選択（design.md 5.1 / 5.2）。requirements.md 6節の順序をそのまま実装する。

**距離を先に、標高を後に。** 本アプリの中核価値は距離の正確さであり、
逆順（獲得標高が最小の候補を選んでから距離を見る）にすると
「坂がないだけの外れた距離」が選ばれる。requirements.md が「順序が仕様」と
明記している点で、実装で最も外しやすい（CLAUDE.md）。

**判定そのものはここに書かない。** ±300m（`is_within_tolerance`）も3倍
（`is_degenerate`）も定義元は `models.Candidate` のプロパティである。ここで
不等号を書き直すと境界の等号が2か所に分かれ、片方だけ直したときに静かに食い違う。
このモジュールが持つのは**順序**だけで、閾値は1つも持たない。

**外部依存ゼロ**（design.md 1.2）。`models` 以外は import しない。
接近ゲートの判定（`classify_approach`）は `generation.py` が打ち切りのために
済ませており、ここが受け取るのはその結論（`verdict`）である。**表示するか
どうかの判断だけがここにある**（design.md 5.2）。
"""

from runloop.models import (
    ApproachVerdict,
    Candidate,
    GenerationOutcome,
    SelectionOutcome,
    SelectionResult,
)


def select(outcome: GenerationOutcome) -> SelectionResult:
    """design.md 5.1 の5手順で1本を選ぶ。

    目標距離を引数で受け取らないのは、`target_m` が `Candidate` に焼き付いて
    いるためである（design.md 2.2 / 5.1）。ここでも受け取ると、呼び出し側が
    候補と別の目標距離を渡す余地が戻る。
    """
    # 手順1: 接近ゲート。候補があっても1本も表示しない（AC-01-3）。
    # 除外より先に置く——起点が悪いときに伝えるべきは「最寄りの道路上を
    # クリックしてください」であって「候補が0件」ではない（design.md 5.2）
    if outcome.verdict is ApproachVerdict.REJECT:
        return SelectionResult(
            chosen=None,
            stock=(),
            outcome=SelectionOutcome.ORIGIN_REJECTED,
            degenerate_count=0,
        )

    # 手順2: 異常値除外（AC-01-5）。妥協パスより先に置く。
    # 実測の異常値は 416,451.3m と 414,672.8m で、妥協して出すコースが
    # 415km であってはならない
    usable = tuple(c for c in outcome.candidates if not c.is_degenerate)
    degenerate_count = len(outcome.candidates) - len(usable)

    # 手順3: 除外後が0件（AC-06-3）。妥協パスに落とさない
    if not usable:
        return SelectionResult(
            chosen=None,
            stock=(),
            outcome=SelectionOutcome.NO_CANDIDATE,
            degenerate_count=degenerate_count,
        )

    # 手順4: 在庫（±300m を満たす候補。AC-01-2）を並べる。
    # 並び順を確定させるのはここだけで、引き直し側では並べ替えない（AC-08-1）
    stock = tuple(sorted((c for c in usable if c.is_within_tolerance), key=_stock_order))

    # 手順5: 在庫があれば先頭（獲得標高が最小。AC-03-2）。
    # 別々に選ばず先頭を指すのは、引き直し1回目で初回と同じコースが出る経路を
    # 作らないため（design.md 2.4）
    if stock:
        return SelectionResult(
            chosen=stock[0],
            stock=stock,
            outcome=SelectionOutcome.IN_TOLERANCE,
            degenerate_count=degenerate_count,
        )

    # 手順6: 妥協パス（AC-01-4）。在庫は空のまま——在庫は定義上 ±300m を
    # 通過した候補だけで、AC-08-4 が「±300m 未満の候補で埋めない」と定めている
    return SelectionResult(
        chosen=min(usable, key=_compromise_order),
        stock=(),
        outcome=SelectionOutcome.COMPROMISED,
        degenerate_count=degenerate_count,
    )


def _stock_order(candidate: Candidate) -> tuple[float, float, int]:
    """在庫の並び順: 獲得標高 昇順 → |距離誤差| 昇順 → seed 昇順（design.md 5.1）。

    第2・第3キーを置くのは、獲得標高が同値の候補があると並び順が実行ごとに
    変わりうるためである。候補は並列に集めるので到着順は一定しない。
    引き直し（AC-08-1）は「次に良い1本」を出す操作なので、順序が決定的でないと
    同じ操作で違う結果になる。第2キーが距離誤差なのは、距離が中核価値だから。
    """
    return (candidate.ascent_m, candidate.abs_error_m, candidate.seed)


def _compromise_order(candidate: Candidate) -> tuple[float, float, int]:
    """妥協パスの順序: |距離誤差| 昇順 → 獲得標高 昇順 → seed 昇順（design.md 5.1）。

    第1キーが在庫と違うのは、妥協パスの基準が距離だからである（AC-01-4）。
    第2キー以降を置く理由は在庫と同じで、`min()` が先に見た方を返す以上、
    同値のときに到着順が結果に出る。
    """
    return (candidate.abs_error_m, candidate.ascent_m, candidate.seed)
