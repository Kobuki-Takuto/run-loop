"""候補の生成（design.md 4.1 / 4.2 / 4.4 / 4.6）。

**経路は2つある**（design.md 4.6.2）。分けるのはスナップ距離のキャッシュの有無で、
どちらも directions は15回。経路 A が節約するのは呼び出し数ではなく**時間**である。

| 経路 | 条件 | 手順 |
|---|---|---|
| A: 一括並列 | キャッシュがある | ゲートは判定済み。15本を同時に投げる |
| B: 二段投入 | キャッシュが無い | プローブ1本 → ゲート → 残り14本を並列 |

**なぜ経路B が「15本同時」ではなく「1本 → 14本」なのか。** 15本を同時に投げてから
判定すると**打ち切りが成立しない。** 送信済みのリクエストは取り消せず、
404 と同様に枠を消費する（design.md 4.6.1）。道路から離れた地点をクリックした
1回の操作で15回消費してしまう。未投入なら確実に投げずに済む。

**経路B を消さない。** 起点だけ復元されてスナップ距離が無い状態（design.md 8.5
条件3〜5、8.6 の部分復旧）は通常の経路であり、例外処理ではない。

**このモジュールが決めないこと。** どの候補を見せるか（AC-01-2 の ±300m、
AC-01-5 の異常値除外、AC-03-2 の獲得標高）は `selection.py` の責務である。
ここは「集める」だけで、絞りも並べ替えもしない（design.md 5.2）。

**キャッシュの有効性も決めない。** 失効（30日）・不変則違反・壊れた値の判定は
`persistence.py` の責務で（design.md 8.5 / 8.6）、ここが受け取るのは「使える値か
`None` か」だけである。**このモジュールは現在時刻を知らない。** 同じ規則を2か所に
置くと、片方だけ直したときに静かに食い違う。破棄も呼び出し側が行う——
生成側は保存先を知らないので、通知（`cache_diverged`）までが責務である。

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

from runloop.checkpoints import extract_turns
from runloop.config import CACHE_DRIFT_TOLERANCE_M, CANDIDATE_COUNT
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
    cached_approach_m: float | None = None,
    rng: random.Random | None = None,
) -> GenerationOutcome:
    """候補を集める（design.md 4.6.2 の2経路）。

    **例外を上げない。** 失敗は種類別に数えてログに出し、成功した候補で続行する
    （AC-06-4「いずれの異常時も、アプリが停止せず再実行できる状態を保つ」）。
    全滅（成功0本）も候補0本という**通常の結果**として返し、AC-06-3 の判定は
    `selection.py` に任せる。

    `cached_approach_m` は起点確定時に測って保存してあったスナップ距離
    （design.md 8.2）。**あれば経路 A、無ければ経路 B。** `None` を「0m の
    キャッシュ」と解釈しないこと——道路から離れた起点でゲートを飛ばし、
    打ち切りが効かなくなる。

    `rng` は乱数源。テストと再現のために受けるだけで、本体は渡さない。
    省くと実行ごとに別の15個が選ばれる（design.md 4.2。固定すると「もう一度
    探す」で同じ15本が返り、US-08 に応えられない）。
    """
    seeds = list(_pick_seeds(rng))
    failures: Counter[str] = Counter()

    if cached_approach_m is not None:
        return _generate_in_one_batch(provider, query, seeds, failures, cached_approach_m)
    return _generate_in_two_stages(provider, query, seeds, failures)


def _generate_in_one_batch(
    provider: RouteProvider,
    query: RouteQuery,
    seeds: Sequence[int],
    failures: Counter[str],
    cached_approach_m: float,
) -> GenerationOutcome:
    """経路 A: 15本を一括で並列に投げる（design.md 4.6.2）。

    ゲートの答え（AC-01-3）は起点確定時のプローブで既に得ているので、
    1本目の応答を待つ理由が無い。**節約するのは呼び出し数ではなく時間**である
    （経路 B の約4〜5秒に対して約2秒の見込み）。

    **キャッシュを信じてゲートを飛ばす以上、誤っていたことに気づく手段を
    同時に持つ**（design.md 8.5.1）。「スナップ距離は起点だけで決まる」は ORS の
    実測事実であってドメインの不変則ではなく、キャッシュはこの事実を構造の
    前提に格上げする操作だからである。

    **打ち切りはここには無い。** 15本は送信済みで取り消せない。キャッシュが
    誤っていた場合の損害は1回の実行（15回）に限られ、以後は破棄されて
    経路 B に落ちる（design.md 4.6.3。この1回ぶんの露出は受け入れる）。
    """
    candidates = _fetch_in_parallel(provider, query, seeds, failures)
    approach_m, verdict = _measure_approach(candidates)

    _LOG.debug("候補 %d 本を %d 回の呼び出しで集めた（経路A）", len(candidates), len(seeds))
    _log_failures(failures)
    return GenerationOutcome(
        candidates=tuple(candidates),
        approach_m=approach_m,
        verdict=verdict,
        failures=dict(failures),
        calls_consumed=len(seeds),
        aborted_early=False,
        cache_diverged=_cache_has_diverged(candidates, cached_approach_m),
    )


def _generate_in_two_stages(
    provider: RouteProvider,
    query: RouteQuery,
    seeds: list[int],
    failures: Counter[str],
) -> GenerationOutcome:
    """経路 B: プローブ1本 → 接近ゲート → 残り14本を並列（design.md 4.1）。"""
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
            # キャッシュを使っていないので、破棄を促す理由も無い
            cache_diverged=False,
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
            cache_diverged=False,
        )

    # --- 2段目: 残りを並列（design.md 4.1 手順6） ----------------------------
    calls += len(seeds)
    candidates += _fetch_in_parallel(provider, query, seeds, failures)

    _LOG.debug("候補 %d 本を %d 回の呼び出しで集めた（経路B）", len(candidates), calls)
    _log_failures(failures)
    return GenerationOutcome(
        candidates=tuple(candidates),
        approach_m=approach_m,
        verdict=verdict,
        failures=dict(failures),
        calls_consumed=calls,
        aborted_early=False,
        cache_diverged=False,
    )


def _measure_approach(
    candidates: Sequence[Candidate],
) -> tuple[float | None, ApproachVerdict | None]:
    """集めた候補から接近距離とゲートの結論を出す（design.md 4.6.3）。

    **キャッシュ値を流さない。** キャッシュ（例 20m = OK）を `verdict` に使うと、
    実際には 300m 超の起点でもコースが画面に出て、AC-01-3「300m 超なら結果を
    表示せず拒否する」を破る。キャッシュはゲートを**飛ばす判断**にだけ使い、
    ゲートの**結論**は実測から出す。

    先頭の候補（シードの順で最初に成功したもの）から取る。実測では接近距離は
    起点だけで決まり全候補で同値になるので（99回すべて同一値）、どれを取っても
    同じはずである。**同じでなくなったこと自体は乖離検査が捕まえる。**
    1本も測れなければ `None`（0m や `REJECT` で埋めない）。
    """
    if not candidates:
        return None, None
    approach_m = candidates[0].approach_m
    return approach_m, classify_approach(approach_m)


def _cache_has_diverged(
    candidates: Sequence[Candidate],
    cached_approach_m: float,
) -> bool:
    """実測がキャッシュから離れていないかを検査する（design.md 8.5.1）。

    **各候補と比べる。** 14本が一致していても1本ずれていれば「起点だけで決まる」
    という前提はもう成り立っていない。平均や代表値1本で比べると、その1本が埋もれる。

    許容 10m は、`/v2/snap` の `snapped_distance` と本アプリの `haversine` が
    別々の計算経路を通るための幅であって、ずれを許す幅ではない（実測の分散は
    ゼロ）。境界は「**超えて**離れていたら」なので、ちょうど 10m は乖離としない。

    1本も測れなかったときは `False`。全滅は API 側の事情であってキャッシュの
    誤りではなく、破棄すると通信が不安定な間ずっと再プローブを繰り返す。
    **観測できていないことと「ずれていた」を区別する。**
    """
    drifts = [abs(candidate.approach_m - cached_approach_m) for candidate in candidates]
    if not drifts:
        return False

    worst = max(drifts)
    if worst <= CACHE_DRIFT_TOLERANCE_M:
        return False

    # 画面には出さない（ユーザーに取れる行動が無い。design.md 9.2）。
    # **座標は出さない**（design.md 8.7）。差の大きさは起点を明かさない
    _LOG.warning(
        "スナップ距離のキャッシュが実測と乖離した（最大 %.1fm、許容 %.1fm）。破棄を促す",
        worst,
        CACHE_DRIFT_TOLERANCE_M,
    )
    return True


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

    **`steps` から `Turn` を取り出すのもここである**（2026-08-07、T18a）。
    `Candidate` は `steps` を持たない（生の案内はプロバイダの語彙である）ので、
    ここで変換しないと案内が候補に変換した時点で失われ、`ui/` から
    チェックポイント（AC-04-1）を組み立てる手段が無くなる。どれが方向転換かの
    判断（AC-04-4 のホワイトリスト）は `checkpoints.py` に置いたままで、
    ここはその結果を運ぶだけである。
    """
    return Candidate(
        seed=route.seed,
        loop_m=route.loop_m,
        approach_m=haversine(query.origin, route.snapped_start),
        ascent_m=route.ascent_m,
        descent_m=route.descent_m,
        target_m=query.target_m,
        geometry=route.geometry,
        turns=extract_turns(route.steps),
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
