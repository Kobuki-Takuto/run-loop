"""候補の生成（design.md 4.1 / 4.2 / 4.4）。

**二段投入（経路B）。** プローブを1本投げ、その応答から接近距離を算出して
ゲート（AC-01-3）にかけ、通ったら残り14本を並列で投げる。
キャッシュを使う一括並列（経路A）と乖離検出は T09 の担当で、ここには無い。

**なぜ「15本同時」ではなく「1本 → 14本」なのか。** 15本を同時に投げてから
判定すると**打ち切りが成立しない。** 送信済みのリクエストは取り消せず、
404 と同様に枠を消費する（design.md 4.6.1）。道路から離れた地点をクリックした
1回の操作で15回消費してしまう。未投入なら確実に投げずに済む。

**このモジュールが決めないこと。** どの候補を見せるか（AC-01-2 の ±300m、
AC-01-5 の異常値除外、AC-03-2 の獲得標高）は `selection.py` の責務である。
ここは「集める」だけで、絞りも並べ替えもしない（design.md 5.2）。

**外部 API の都合を知らない。** HTTP も `requests` も出てこない。知っているのは
`ports.RouteProvider` と6例外だけで、リトライは client の内側にある。
そのため**送信回数はここから見えない**（消費した呼び出し回数だけを数える。
design.md 4.3）。
"""

import logging
import random
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from runloop.config import CANDIDATE_COUNT
from runloop.geo import haversine
from runloop.models import (
    ApproachVerdict,
    Candidate,
    GenerationOutcome,
    ProviderRoute,
    RouteQuery,
    classify_approach,
)
from runloop.ports import RouteProvider, RouteProviderError

_LOG: Final = logging.getLogger(__name__)

# シードの範囲。実測は 1〜30・106〜113・1001〜1006 のみで、大きな値での挙動は
# 未確認（design.md 4.2「シードの範囲は要検証」）。重複しない15個が取れれば足りる
_SEED_MIN: Final = 1
_SEED_MAX: Final = 1_000_000


def generate(
    provider: RouteProvider,
    query: RouteQuery,
    *,
    rng: random.Random | None = None,
) -> GenerationOutcome:
    """候補を集める（design.md 4.1 の経路B）。

    **例外を上げない。** 失敗は種類別に数えてログに出し、成功した候補で続行する
    （AC-06-4「いずれの異常時も、アプリが停止せず再実行できる状態を保つ」）。
    全滅（成功0本）も候補0本という**通常の結果**として返し、AC-06-3 の判定は
    `selection.py` に任せる。

    `rng` は乱数源。テストと再現のために受けるだけで、本体は渡さない。
    省くと実行ごとに別の15個が選ばれる（design.md 4.2。固定すると「もう一度
    探す」で同じ15本が返り、US-08 に応えられない）。
    """
    seeds = list(_pick_seeds(rng))
    failures: Counter[str] = Counter()
    candidates: list[Candidate] = []
    calls = 0

    # --- 1段目: プローブ（design.md 4.1 手順3〜5） ---------------------------
    probe: Candidate | None = None
    while seeds and probe is None:
        calls += 1
        probe = _record(_attempt(provider, query, seeds.pop(0)), failures)

    if probe is None:
        # 1本も応答が得られず、接近距離を測れなかった。**0m や REJECT で埋めない**
        _log_failures(failures)
        return GenerationOutcome(
            candidates=(),
            approach_m=None,
            verdict=None,
            failures=dict(failures),
            calls_consumed=calls,
            aborted_early=False,
        )

    candidates.append(probe)  # プローブも候補として使う（総数を15回に保つ）
    approach_m = probe.approach_m
    verdict = classify_approach(approach_m)

    if verdict is ApproachVerdict.REJECT:
        # AC-01-3。**残りを投げない。** ここが枠を守る唯一の場所である
        _LOG.info("接近ゲートで打ち切った。残り %d 本は投げない", len(seeds))
        _log_failures(failures)
        return GenerationOutcome(
            candidates=tuple(candidates),
            approach_m=approach_m,
            verdict=verdict,
            failures=dict(failures),
            calls_consumed=calls,
            aborted_early=True,
        )

    # --- 2段目: 残りを並列（design.md 4.1 手順6） ----------------------------
    calls += len(seeds)
    candidates += _fetch_in_parallel(provider, query, seeds, failures)

    _LOG.debug("候補 %d 本を %d 回の呼び出しで集めた", len(candidates), calls)
    _log_failures(failures)
    return GenerationOutcome(
        candidates=tuple(candidates),
        approach_m=approach_m,
        verdict=verdict,
        failures=dict(failures),
        calls_consumed=calls,
        aborted_early=False,
    )


def _pick_seeds(rng: random.Random | None) -> tuple[int, ...]:
    """重複しないシードを15個選ぶ（design.md 4.2）。

    重複すると同じ経路が2本返り、在庫（US-08）が見かけより少なくなる。
    しかも画面上は「コースが出る」ので正常に見え、引き直しで同じコースが
    出続けることでしか気づけない。`sample` は重複しないことを保証する。
    """
    source = rng if rng is not None else random.Random()
    return tuple(source.sample(range(_SEED_MIN, _SEED_MAX + 1), CANDIDATE_COUNT))


def _fetch_in_parallel(
    provider: RouteProvider,
    query: RouteQuery,
    seeds: Sequence[int],
    failures: Counter[str],
) -> list[Candidate]:
    """残りのシードを**同時に**投げる（design.md 4.1「並列の実現方法」）。

    `requests` は同期なので `ThreadPoolExecutor` を使う（スパイクで実測した構成と
    同じ。同時10本でエラー0件）。`asyncio` + `httpx` に替えない——依存を増やす
    利益がなく、実測済みの構成から離れる不利益がある。

    **ワーカー数を投げる本数と同じにする。** 少なくすると波に分かれ、
    429 の待機（client の内側の `time.sleep`）が重ならずに積み上がる。

    数えるのは呼び出し側（この関数を抜けた後）である。`Counter` を
    ワーカーから触ると加算が競合するので、**結果を持ち帰ってから数える。**
    """
    if not seeds:
        return []

    def attempt(seed: int) -> Candidate | RouteProviderError:
        return _attempt(provider, query, seed)

    with ThreadPoolExecutor(max_workers=len(seeds)) as pool:
        results = list(pool.map(attempt, seeds))

    return [candidate for result in results if (candidate := _record(result, failures))]


def _attempt(
    provider: RouteProvider,
    query: RouteQuery,
    seed: int,
) -> Candidate | RouteProviderError:
    """1本取りにいく。**失敗を例外として持ち帰る**（その場で数えない）。

    並列のワーカーから呼ばれるので、ここで共有の集計に触れないようにする。
    捕まえるのは `RouteProviderError` だけで、それ以外の例外は上へ抜ける
    （プロバイダ由来でない不具合を欠測として黙らせない。AC-06-4 の捕捉は
    `ui/` の最外殻が行う。T18b）。
    """
    try:
        route = provider.round_trip(
            query.origin,
            # 目標距離をそのまま要求する。**補正しない**（ADR-0003）
            length_m=query.target_m,
            seed=seed,
            points=query.points,
            avoid_steps=query.avoid_steps,
        )
    except RouteProviderError as exc:
        return exc
    return _to_candidate(route, query)


def _record(
    result: Candidate | RouteProviderError,
    failures: Counter[str],
) -> Candidate | None:
    """成功なら候補、失敗なら種類別に数えて `None`（design.md 4.4）。

    種類をまとめると、404 が続いているのか 5xx なのかがログから読めない。
    どちらも「候補が減る」だが、原因も次の行動も違う。
    """
    if isinstance(result, RouteProviderError):
        failures[type(result).__name__] += 1
        return None
    return result


def _to_candidate(route: ProviderRoute, query: RouteQuery) -> Candidate:
    """プロバイダの成果をドメインの候補にする（design.md 2.2 / 4.4）。

    **接近距離はここで算出する。** 起点はアプリ側の概念でプロバイダの成果では
    ないので、`ProviderRoute` は持っていない（design.md 3.1）。

    候補ごとに、**その応答の** `snapped_start` から測る。実測では接近距離は
    起点だけで決まりシードに依存しないが、それは ORS の実測事実であって
    ドメインの不変則ではない。プローブの値を全候補に配ると、前提が崩れたときに
    合計距離（AC-01-2）が静かに間違う。
    """
    return Candidate(
        seed=route.seed,
        loop_m=route.loop_m,
        approach_m=haversine(query.origin, route.snapped_start),
        ascent_m=route.ascent_m,
        descent_m=route.descent_m,
        target_m=query.target_m,
        geometry=route.geometry,
    )


def _log_failures(failures: Counter[str]) -> None:
    """失敗の内訳をログに出す。**画面には出さない**（design.md 4.4 / 9.2）。

    15本のうち1〜2本の 404 は実測される正常な範囲であり（自宅 1/103、
    暫定座標 2/23）、ユーザーの行動を変えない情報である。在庫が減る影響は
    AC-08-3 のメッセージで結果として伝わる。

    **座標を出さない**（design.md 8.7）。起点は自宅である。
    """
    if not failures:
        return
    breakdown = "、".join(f"{kind} {count}件" for kind, count in sorted(failures.items()))
    _LOG.info("失敗の内訳: %s", breakdown)
