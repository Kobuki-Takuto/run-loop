"""在庫と引き直しカーソルの状態（design.md 6.1 / 6.2）。US-08 の遷移を持つ。

**純データである。** Streamlit も、プロバイダも、選択規準も持たない。
AC-08-2「引き直しでは外部 API を呼び出さない」は、呼ばない手続きを書くのではなく
**呼ぶ手段を持たない**ことで守る（`models` 以外を import しない。design.md 1.3）。

**並べ替えない。** 在庫の順序を決めるのは `selection.py` だけで、ここは受け取った
並びのまま先頭から1本ずつ出す。AC-08-1 が「選択規準は初回表示と同一であり、
ランダムではない」と定めており、順序の定義元が2か所に分かれると守れない。

**遷移は書き換えではなく差し替えである**（design.md 6.1）。Streamlit は操作ごとに
スクリプトを再実行するため、古いセッションを参照している経路が残る。書き換えると
そこから見える状態が静かに変わる。`RunSession` は不変で、`reroll()` は新しい
`RunSession` を返す。`ui/` は `st.session_state["run"]` を差し替えるだけにする。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from runloop.models import (
    Candidate,
    GenerationOutcome,
    RouteQuery,
    SelectionOutcome,
    SelectionResult,
)


@dataclass(frozen=True)
class RunSession:
    """1回の実行の状態。**これが1つあれば画面が描ける**（design.md 6.1）。

    状態を複数のキーに分けない（在庫・カーソル・起点を別々に置くと、片方だけ
    更新される事故が起きる）。Streamlit 層はこれを `st.session_state` に
    1つ置くだけにする。

    **在庫と結論は `selection` から読み出す**（フィールドとして持たない）。
    妥協パス（AC-01-4）は在庫を空にしたまま1本を表示するので、`stock` と
    表示する1本を別々に持つと同じ事実が2か所に分かれる。
    """

    query: RouteQuery
    selection: SelectionResult
    # 現在表示している在庫の位置（0 始まり）。前進のみ（design.md 6.2）
    cursor: int
    # この実行の接近距離（全候補共通）。1本も測れなかった実行では None
    approach_m: float | None
    # 生成時刻（ログとデバッグ用）。**現在時刻はここでは読まない**——
    # 呼び出し側から受け取る（generation.py が失効の判定を持たないのと同じ）
    generated_at: datetime
    # 失敗の内訳（例外の型の名前 → 件数）。**全滅の原因を言い分けるために運ぶ**
    # （2026-08-07 に追加、AC-06-1）。これが無いと、15本すべてが接続不能で
    # 失敗しても画面には AC-06-3 の「候補が得られませんでした。起点を道路の
    # 近くに指定し直すか」しか出せず、**起点は悪くないのに起点を疑わせる**。
    # 部分的な失敗（15本中1〜2本の 404）は画面に出さない（design.md 4.4）——
    # 出すかどうかを決めるのは表示側で、ここは事実を運ぶだけである
    failures: Mapping[str, int] = field(default_factory=dict)

    @property
    def stock(self) -> tuple[Candidate, ...]:
        """在庫（±300m を満たす候補。獲得標高の昇順）。生成後は不変。"""
        return self.selection.stock

    @property
    def outcome(self) -> SelectionOutcome:
        """この実行の結論（design.md 5.2）。次にすべき操作がこれで決まる。"""
        return self.selection.outcome

    @property
    def current(self) -> Candidate | None:
        """いま表示している1本。

        在庫が空でも `chosen` を返すのは、妥協パス（AC-01-4）が
        「条件を満たすコースがなかった旨」と併せて1本を表示するためである。
        `NO_CANDIDATE` / `ORIGIN_REJECTED` では `chosen` が `None` なので
        そのまま `None` になる（表示する1本がない）。
        """
        if not self.stock:
            return self.selection.chosen
        return self.stock[self.cursor]


@dataclass(frozen=True)
class RerollResult:
    """引き直しの結果（design.md 6.2）。

    **進めたかどうかを一緒に返す。** 新しいセッションだけを返すと、在庫の末尾では
    同じ状態が返り、画面上は何も起きなかったように見える。AC-08-3 は
    「尽きたことを黙って同じコースを再表示しない」と定めているので、
    呼び出し側が見落とせない形にする。
    """

    session: RunSession
    # False なら在庫の末尾だった（AC-08-3 の文言を出す）
    advanced: bool


def start(
    query: RouteQuery,
    generation: GenerationOutcome,
    selection: SelectionResult,
    *,
    generated_at: datetime,
) -> RunSession:
    """実行（探す）の結果からセッションを作る（design.md 6.2）。カーソルは先頭。

    接近距離を `selection` ではなく `generation` から取るのは、それが生成の過程で
    観測した事実だからである（design.md 4.4）。引数で別に受け取る形にすると、
    候補と無関係な値を渡す余地が残る（T10 が `target_m` を引数から外したのと
    同じ理由）。
    """
    return RunSession(
        query=query,
        selection=selection,
        cursor=0,
        approach_m=generation.approach_m,
        generated_at=generated_at,
        failures=generation.failures,
    )


def reroll(session: RunSession) -> RerollResult:
    """引き直し（AC-08-1 / AC-08-2）。在庫の次の1本へ進める。

    **API は呼ばない。** 進めるのはカーソルだけで、出てくるのは初回実行で得た
    在庫のその候補そのものである。

    在庫の末尾（および在庫が空のとき）はカーソルを動かさず、`advanced=False` で
    尽きたことを伝える（AC-08-3）。**前の候補に戻る操作は持たない**——
    要件になく、あると「どちら向きに尽きたか」で判定が分かれる（design.md 6.2）。
    """
    if session.cursor + 1 >= len(session.stock):
        return RerollResult(session=session, advanced=False)
    return RerollResult(session=replace(session, cursor=session.cursor + 1), advanced=True)


def is_stale(session: RunSession, query: RouteQuery) -> bool:
    """在庫を破棄すべきか（design.md 6.2）。条件が変わっていれば True。

    鍵は `RouteQuery` 全体の一致である。目標 5km の在庫は 3km の ±300m を
    満たさず、条件が変わった在庫から出すと AC-08-4 が壊れる。`avoid_steps` を
    外して集めた在庫は AC-03-1（階段を含まない）の前提が違い、距離を満たしていても
    出してよい在庫ではない。**破棄すべき条件を数え上げるより、条件そのものの
    一致で判定するほうが、条件が増えたときに取りこぼさない。**
    """
    return session.query != query
