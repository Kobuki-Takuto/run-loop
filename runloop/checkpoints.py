"""方向転換の抽出と5件への間引き（design.md 7.1 / 7.2 / 7.3、requirements.md AC-04-1〜4）。

AC-04 の4基準をここに集める。**接近距離のオフセットを足す場所は
`select_checkpoints` が `Checkpoint` を作る箇所の1か所に限定する**
（design.md 7.2）。`extract_turns` は `approach_m` を引数に取らない
（`Turn.cumulative_loop_m` はルート始点からの生の累積距離のまま）。
二重加算と加算漏れは、どちらも実測ではオフセットが小さい起点（0.7m）では
気づけないため、型（関数の引数）で場所を固定する。
"""

from typing import Final

from runloop.models import Checkpoint, Maneuver, RawStep, Turn, TurnDirection

# 方向転換として扱う maneuver のホワイトリスト（design.md 7.1 の表。type 0〜5 相当）。
# ここに無い種別（STRAIGHT・KEEP_LEFT・KEEP_RIGHT・DEPART・ARRIVE・UNKNOWN）は
# 方向転換として扱わない側に倒す（AC-04-4）。ブラックリストにしないのは、
# 未観測の種別が来たときに「不明」の案内を出さないため
_TURN_DIRECTIONS: Final[dict[Maneuver, TurnDirection]] = {
    Maneuver.TURN_LEFT: TurnDirection.TURN_LEFT,
    Maneuver.TURN_RIGHT: TurnDirection.TURN_RIGHT,
    Maneuver.SHARP_LEFT: TurnDirection.SHARP_LEFT,
    Maneuver.SHARP_RIGHT: TurnDirection.SHARP_RIGHT,
    Maneuver.SLIGHT_LEFT: TurnDirection.SLIGHT_LEFT,
    Maneuver.SLIGHT_RIGHT: TurnDirection.SLIGHT_RIGHT,
}


def extract_turns(steps: tuple[RawStep, ...]) -> tuple[Turn, ...]:
    """方向転換だけを `Turn` として抜き出す（design.md 7.1、AC-04-4）。

    `cumulative_loop_m` は各 step の**開始点**（それ以前の step の distance の
    合計）。方向転換でない step も、距離の積み上げには効かせる
    （ホワイトリストで除外するのは Turn への変換だけである）。
    """
    turns: list[Turn] = []
    cumulative = 0.0
    for step in steps:
        direction = _TURN_DIRECTIONS.get(step.maneuver)
        if direction is not None:
            turns.append(
                Turn(
                    direction=direction,
                    cumulative_loop_m=cumulative,
                    position=step.position,
                    name=step.name,
                )
            )
        cumulative += step.distance_m
    return tuple(turns)


def select_checkpoints(
    turns: tuple[Turn, ...],
    *,
    approach_m: float,
    loop_m: float,
    max_count: int = 5,
) -> tuple[Checkpoint, ...]:
    """表示する `Checkpoint` を選ぶ（AC-04-1〜3）。

    `max_count` 件以下ならすべてを、超えるなら `_thin` で間引く。
    起点からの距離（`approach_m + cumulative_loop_m`）を足すのはここだけ
    （design.md 7.2）。距離の昇順に並べ、`order` を 1 から振る（design.md 7.3 手順4）。
    """
    chosen = turns if len(turns) <= max_count else _thin(
        turns, approach_m=approach_m, loop_m=loop_m, max_count=max_count
    )
    ordered = sorted(chosen, key=lambda turn: approach_m + turn.cumulative_loop_m)
    return tuple(
        Checkpoint(
            order=order,
            distance_from_origin_m=approach_m + turn.cumulative_loop_m,
            direction=turn.direction,
            name=turn.name,
            position=turn.position,
        )
        for order, turn in enumerate(ordered, start=1)
    )


def _thin(
    turns: tuple[Turn, ...],
    *,
    approach_m: float,
    loop_m: float,
    max_count: int,
) -> tuple[Turn, ...]:
    """ループを `max_count + 1` 等分した目標距離への最近傍で `max_count` 件選ぶ（design.md 7.3）。

    目標距離の昇順に処理し、選んだ `Turn` は候補から取り除く。**取り除かないと、
    近い場所に密集した Turn が複数の目標に選ばれ、同じ Turn が2回選ばれて
    最終的な件数が `max_count` を割り込む。**
    """
    remaining = list(turns)
    picked: list[Turn] = []
    divisions = max_count + 1
    for i in range(1, divisions):
        target = approach_m + loop_m * i / divisions

        def distance_to_target(turn: Turn, target: float = target) -> float:
            return abs((approach_m + turn.cumulative_loop_m) - target)

        nearest = min(remaining, key=distance_to_target)
        picked.append(nearest)
        remaining.remove(nearest)
    return tuple(picked)
