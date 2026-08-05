"""2段方式を採用すべきかを決着させる使い捨てスクリプト。

3本目のスパイクで、同一シード内では実距離が length に対して単調で、比の
標準偏差が 3.5〜5.7pt に収まることが分かった。ここから「1本目で比を測り、
補正長で2本目を投げる」2段方式が成立しそうだと見えたが、n=3 の補間による
予測でしかない。実測で決着させる。

決定ルール（データを見る前に固定する）::

    単純方式（N本投げて最良を選ぶ）の命中率 p の Wilson 信頼区間下限で評価し、
    15本投げたときの成功確率が 90% を超えるなら 2段方式は採用しない。
    p = 0.15 のとき 15本で 91.3% なので、下限が 15% を超えれば単純方式で決定。

このルールは実行開始時と結果表示時の両方に出力する。後から基準を変えられない
ようにするため。

測定::

    30シード × 2回 = 60回
    1本目: 目標長そのまま        → 単純方式のブラインド標本（n=30）
    2本目: 1本目の比から補正長   → 2段方式の標本（n=30）

減衰係数は使わない。代わりに「1本目と2本目のうち目標に近い方を採る」方式も
評価する（補正した標本を*追加する*という考え方。減衰係数が不要になる）。

毎分制限（約40回）を避けるため、10本並列を1バッチとし、バッチ間に30秒待つ。
429 が出たバッチは待って再試行する（失敗は無料枠を消費しないと実測済み）。

座標は一切記録しない。距離だけで足りる。

検証が終わったら消してよい。本体のパッケージ（runloop/）には依存させない。
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import requests
from dotenv import load_dotenv

ENDPOINT: Final = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"
FALLBACK_LAT: Final = 31.5966
FALLBACK_LON: Final = 130.5571
OUT_DIR: Final = Path(__file__).parent / "out"

TOLERANCE_M: Final = 300.0  # AC-01-2
DEGENERATE_FACTOR: Final = 3.0  # 目標の何倍を超えたら異常ルートとみなすか

# 決定ルールの定数。ここを実行時に変更する手段は用意しない
DECISION_TRIALS: Final = 15
DECISION_THRESHOLD: Final = 0.90

DECISION_RULE_TEXT: Final = f"""決定ルール（データを見る前に固定済み）:
  単純方式の命中率 p の Wilson 信頼区間下限を取り、
  {DECISION_TRIALS} 本投げたときの成功確率 1-(1-p_lo)^{DECISION_TRIALS} が
  {DECISION_THRESHOLD:.0%} を超えるなら 2段方式は採用しない（単純方式で決定）。"""


@dataclass
class Row:
    """1回の呼び出しの記録。座標は持たない。"""

    stage: str  # probe（1本目）または corrected（2本目）
    seed: int
    requested_m: int
    points: int
    elapsed_s: float | None = None
    loop_m: float | None = None
    snap_m: float | None = None
    total_m: float | None = None
    total_error_m: float | None = None
    ratio: float | None = None  # loop_m / requested_m
    status: int | None = None
    ratelimit_remaining: int | None = None
    retries: int = 0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.total_error_m is not None

    @property
    def degenerate(self) -> bool:
        return self.loop_m is not None and self.loop_m > 0 and self.ratio is not None and (
            self.loop_m > self.requested_m * DEGENERATE_FACTOR
        )

    @property
    def hit(self) -> bool:
        return self.ok and abs(self.total_error_m or 1e9) <= TOLERANCE_M


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離（m）。球近似。スナップ距離の算出にのみ使う。"""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二項比率の Wilson 信頼区間（既定 95%）。"""
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def call(
    *,
    url: str,
    api_key: str,
    lat: float,
    lon: float,
    stage: str,
    seed: int,
    length_m: int,
    points: int,
    target_m: int,
) -> Row:
    """1回だけ呼び出す。所要時間を必ず記録する。"""
    row = Row(stage=stage, seed=seed, requested_m=length_m, points=points)
    body: dict[str, Any] = {
        "coordinates": [[lon, lat]],
        "options": {
            "round_trip": {"length": length_m, "points": points, "seed": seed},
            "avoid_features": ["steps"],
        },
        "elevation": True,
        "instructions": False,
        "units": "m",
    }
    started = time.perf_counter()
    try:
        response = requests.post(
            url,
            json=body,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/geo+json",
            },
            timeout=90,
        )
    except requests.RequestException as exc:
        row.elapsed_s = round(time.perf_counter() - started, 2)
        row.note = f"{type(exc).__name__}"
        return row
    row.elapsed_s = round(time.perf_counter() - started, 2)
    row.status = response.status_code

    remaining = response.headers.get("X-Ratelimit-Remaining")
    if remaining is not None and remaining.isdigit():
        row.ratelimit_remaining = int(remaining)

    if response.status_code != 200:
        row.note = response.text[:120].replace("\n", " ").strip()
        return row

    try:
        payload = response.json()
        feature = payload["features"][0]
        row.loop_m = float(feature["properties"]["summary"]["distance"])
        coords = feature["geometry"]["coordinates"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        row.note = f"距離が取り出せない: {type(exc).__name__}"
        return row

    row.ratio = round(row.loop_m / length_m, 4)
    # 合計距離方式（設計で合意した基準）で判定する。
    # 座標そのものは記録せず、距離に変換した値だけを残す。
    row.snap_m = round(haversine_m(lat, lon, coords[0][1], coords[0][0]), 1)
    row.total_m = round(row.loop_m + row.snap_m * 2, 1)
    row.total_error_m = round(row.total_m - target_m, 1)
    return row


def run_batch(
    items: list[dict[str, Any]],
    *,
    workers: int,
    retry_wait: float,
    max_retries: int,
    label: str,
    **shared: Any,
) -> tuple[list[Row], int]:
    """1バッチを並列で投げる。429 は待って再試行する。

    戻り値は (結果, 追加で投げた再試行の回数)。再試行は無料枠を消費しない
    （3本目のスパイクで実測済み）ため、計画回数の上限には数えない。
    """
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = [f.result() for f in [pool.submit(call, **item, **shared) for item in items]]
    wall = round(time.perf_counter() - started, 2)

    extra = 0
    for attempt in range(1, max_retries + 1):
        stuck = [i for i, r in enumerate(rows) if r.status == 429]
        if not stuck:
            break
        print(
            f"    429 が {len(stuck)} 件。{retry_wait:.0f} 秒待って再試行"
            f"（{attempt}/{max_retries}）"
        )
        time.sleep(retry_wait)
        retry_items = [items[i] for i in stuck]
        with ThreadPoolExecutor(max_workers=len(retry_items)) as pool:
            retried = [
                f.result()
                for f in [pool.submit(call, **item, **shared) for item in retry_items]
            ]
        extra += len(retried)
        for slot, row in zip(stuck, retried, strict=True):
            row.retries = attempt
            rows[slot] = row

    ok = sum(1 for r in rows if r.ok)
    hits = sum(1 for r in rows if r.hit)
    print(f"    {label}: 壁時計 {wall}s / 成功 {ok}/{len(rows)} / 命中 {hits}")
    return rows, extra


def print_table(rows: list[Row]) -> None:
    header = (
        f"{'seed':>5} {'段':>10} {'指定len':>8} {'秒':>5} {'ループ':>9} "
        f"{'合計':>9} {'合計誤差':>9} {'比':>7} {'±300':>5}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        if not r.ok:
            print(
                f"{r.seed:>5} {r.stage:>10} {r.requested_m:>8} "
                f"{r.elapsed_s or 0:>5.2f} {'---':>9} {'---':>9} {'---':>9} "
                f"{'---':>7}  失敗 {r.status} {r.note[:28]}"
            )
            continue
        print(
            f"{r.seed:>5} {r.stage:>10} {r.requested_m:>8} {r.elapsed_s or 0:>5.2f} "
            f"{r.loop_m or 0:>9.1f} {r.total_m or 0:>9.1f} "
            f"{r.total_error_m or 0:>+9.1f} {r.ratio or 0:>7.4f} "
            f"{'○' if r.hit else '×':>5}"
        )


def describe(label: str, rows: list[Row]) -> tuple[int, int, float, float]:
    """命中率と Wilson 信頼区間を出す。戻り値は (命中数, n, 下限, 上限)。"""
    n = len(rows)
    hits = sum(1 for r in rows if r.hit)
    lo, hi = wilson_interval(hits, n) if n else (0.0, 0.0)
    rate = hits / n * 100 if n else 0.0
    print(
        f"  {label:<28} n={n:>3}  命中 {hits:>2} = {rate:>5.1f}%  "
        f"95%CI {lo * 100:>5.1f}% 〜 {hi * 100:>5.1f}%"
    )
    return hits, n, lo, hi


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=5000)
    parser.add_argument("--points", type=int, default=3)
    parser.add_argument("--seeds", type=int, default=30, help="シード数（呼び出しは2倍）")
    parser.add_argument("--batch", type=int, default=10, help="1バッチの並列数")
    parser.add_argument("--batch-wait", type=float, default=30.0, help="バッチ間の待機秒")
    parser.add_argument("--retry-wait", type=float, default=30.0, help="429 後の待機秒")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-calls", type=int, default=60, help="計画回数の上限")
    parser.add_argument("--profile", default="foot-walking")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    planned = args.seeds * 2
    batches = math.ceil(args.seeds / args.batch) * 2
    est_min = (batches * 2.5 + (batches - 1) * args.batch_wait) / 60

    print(DECISION_RULE_TEXT)
    print()
    print(f"目標距離 : {args.target}m / points={args.points}")
    print(f"シード   : 1..{args.seeds}（1本目=目標長、2本目=補正長）")
    print(f"計画回数 : {planned} 回（上限 {args.max_calls}）")
    print(f"バッチ   : {args.batch} 並列 × {batches} 回、間隔 {args.batch_wait:.0f}s")
    print(f"所要見込 : 約 {est_min:.1f} 分")
    if planned > args.max_calls:
        print("\n上限を超えている。1回も呼び出さずに中止する。")
        return 2
    if args.dry_run:
        print("\n--dry-run なので呼び出さずに終了する。")
        return 0

    load_dotenv()
    api_key = os.getenv("ORS_API_KEY")
    if not api_key:
        print("\nORS_API_KEY が未設定。")
        return 2
    lat = float(os.getenv("HOME_LAT") or FALLBACK_LAT)
    lon = float(os.getenv("HOME_LON") or FALLBACK_LON)
    if not os.getenv("HOME_LAT"):
        print("HOME_LAT/HOME_LON が未設定のため暫定座標を使う（結果の解釈に注意）")

    shared = {
        "url": ENDPOINT.format(profile=args.profile),
        "api_key": api_key,
        "lat": lat,
        "lon": lon,
        "points": args.points,
        "target_m": args.target,
    }
    batch_opts = {
        "workers": args.batch,
        "retry_wait": args.retry_wait,
        "max_retries": args.max_retries,
    }
    seeds = list(range(1, args.seeds + 1))
    extra_calls = 0

    # ===== 1本目: 目標長そのまま =====
    print("\n=== 1本目: 目標長そのまま（単純方式のブラインド標本）===")
    probes: list[Row] = []
    chunks = [seeds[i : i + args.batch] for i in range(0, len(seeds), args.batch)]
    for index, chunk in enumerate(chunks, start=1):
        items = [
            {"stage": "probe", "seed": s, "length_m": args.target} for s in chunk
        ]
        rows, extra = run_batch(
            items, label=f"バッチ {index}/{len(chunks)}", **batch_opts, **shared
        )
        probes += rows
        extra_calls += extra
        if index < len(chunks):
            time.sleep(args.batch_wait)

    # ===== 2本目: 1本目の比から補正長 =====
    print("\n=== 2本目: 補正長（2段方式の標本）===")
    plan2: list[dict[str, Any]] = []
    skipped: list[str] = []
    for row in probes:
        if not row.ok or not row.ratio:
            skipped.append(f"seed={row.seed}(1本目失敗)")
            continue
        if row.degenerate:
            # 比が壊れているので補正しても意味がない。アプリも同様に捨てる
            skipped.append(f"seed={row.seed}(異常ルート)")
            continue
        corrected = round(args.target / row.ratio)
        plan2.append({"stage": "corrected", "seed": row.seed, "length_m": corrected})
    if skipped:
        print(f"  2本目を省くシード: {', '.join(skipped)}")

    corrected_rows: list[Row] = []
    chunks2 = [plan2[i : i + args.batch] for i in range(0, len(plan2), args.batch)]
    for index, chunk2 in enumerate(chunks2, start=1):
        time.sleep(args.batch_wait)
        rows, extra = run_batch(
            chunk2, label=f"バッチ {index}/{len(chunks2)}", **batch_opts, **shared
        )
        corrected_rows += rows
        extra_calls += extra

    all_rows = probes + corrected_rows

    print("\n--- 1本目の結果 ---")
    print_table(probes)
    print("\n--- 2本目の結果 ---")
    print_table(corrected_rows)

    # ===== 1. 単純方式 / 3. 2段方式 / 4. 良い方を採る =====
    print("\n=== 命中率の比較（判定は合計距離基準）===")
    h_simple, n_simple, lo_simple, hi_simple = describe("1. 単純方式（1本目）", probes)
    describe("3. 2段方式（2本目のみ）", corrected_rows)

    by_seed: dict[int, list[Row]] = {}
    for row in all_rows:
        by_seed.setdefault(row.seed, []).append(row)
    best_rows: list[Row] = []
    for rows in by_seed.values():
        usable = [r for r in rows if r.ok]
        if usable:
            best_rows.append(min(usable, key=lambda r: abs(r.total_error_m or 1e9)))
    h_best, n_best, lo_best, hi_best = describe("4. 良い方を採る（1本+2本）", best_rows)

    # ===== 2. 決定ルールの判定 =====
    print("\n=== 2. 決定ルールの判定 ===")
    print(DECISION_RULE_TEXT)
    success = 1 - (1 - lo_simple) ** DECISION_TRIALS
    print(
        f"\n  単純方式の下限 p_lo = {lo_simple * 100:.1f}%（n={n_simple}）\n"
        f"  {DECISION_TRIALS} 本投げたときの成功確率 = {success:.1%}"
    )
    if success > DECISION_THRESHOLD:
        print(
            f"\n  判定: {success:.1%} > {DECISION_THRESHOLD:.0%} なので"
            "【単純方式で決定】。2段方式は採用しない。"
        )
    else:
        print(
            f"\n  判定: {success:.1%} <= {DECISION_THRESHOLD:.0%} なので"
            "【単純方式では不足】。2段方式を採用する。"
        )

    # ===== 在庫数の比較 =====
    # 引き直し（別のコースを見たい操作）は、取得済みの候補から2番目に良いものを
    # 出せば済むので API を叩かない。したがって方式の価値は「1本を精度よく
    # 当てること」ではなく「±300m を満たす候補を何本在庫できるか」になる。
    print("\n=== 在庫数の比較（±300m を満たす候補が平均何本得られるか）===")
    pairs = DECISION_TRIALS * 2 // 3  # 20回相当のペア数（=10）
    print(f"  {'方式':<26} {'呼出':>4} {'在庫(平均)':>12} {'95%CI':>16} {'≥1本':>7} {'≥2本':>7}")
    print("  " + "-" * 78)
    for label, units, calls, point, plo, phi in (
        (
            f"単純方式 {DECISION_TRIALS} 本",
            DECISION_TRIALS,
            DECISION_TRIALS,
            h_simple / n_simple if n_simple else 0.0,
            lo_simple,
            hi_simple,
        ),
        (
            f"単純方式 {pairs * 2} 本（同予算）",
            pairs * 2,
            pairs * 2,
            h_simple / n_simple if n_simple else 0.0,
            lo_simple,
            hi_simple,
        ),
        (
            f"2段方式 {pairs} ペア",
            pairs,
            pairs * 2,
            h_best / n_best if n_best else 0.0,
            lo_best,
            hi_best,
        ),
    ):
        at_least_1 = 1 - (1 - point) ** units
        at_least_2 = at_least_1 - units * point * (1 - point) ** (units - 1)
        print(
            f"  {label:<26} {calls:>4} {units * point:>10.1f} 本  "
            f"{units * plo:>6.1f}〜{units * phi:<7.1f} "
            f"{at_least_1:>6.1%} {max(at_least_2, 0.0):>7.1%}"
        )
    print("  ≥2本 = 引き直し（2番目の候補を出す）が API なしで成立する確率")

    print("\n  呼び出し数と毎分制限（約40回/分）への耐性:")
    print(f"    単純方式 {DECISION_TRIALS} 本 = {DECISION_TRIALS} 回/実行")
    print(f"    2段方式 {pairs} ペア = {pairs * 2} 回/実行")
    print("    引き直しを在庫から出すなら API を叩かないので、毎分制限は")
    print("    「新しい距離で引き直す頻度」だけで評価すればよい。回数の少ない")
    print("    方式ほど有利だが、在庫が減るなら引き直し自体ができなくなる")

    # ===== 5. 補正後の誤差の符号 =====
    print("\n=== 5. 補正後の誤差の符号 ===")
    errs = [r.total_error_m for r in corrected_rows if r.total_error_m is not None]
    if errs:
        pos = sum(1 for e in errs if e > 0)
        print(
            f"  正 {pos} 件 / 負 {len(errs) - pos} 件  平均 {statistics.mean(errs):+.1f}m  "
            f"中央 {statistics.median(errs):+.1f}m"
        )
        print("  正に偏るなら、比が length とともに上がるドリフトの裏付けになる")

    # ===== 6. 同一シード内の比のドリフト =====
    print("\n=== 6. 同一シード内の比のドリフト ===")
    drifts: list[tuple[int, float, float, int, float]] = []
    probe_by_seed = {r.seed: r for r in probes}
    for row in corrected_rows:
        probe = probe_by_seed.get(row.seed)
        if not probe or not probe.ratio or not row.ratio:
            continue
        d_len = row.requested_m - probe.requested_m
        drifts.append((row.seed, probe.ratio, row.ratio, d_len, row.ratio - probe.ratio))
    if drifts:
        print(f"  {'seed':>5} {'比(1本目)':>10} {'比(2本目)':>10} {'Δlen':>7} {'Δ比':>8}")
        for seed, r1, r2, d_len, d_ratio in drifts:
            print(f"  {seed:>5} {r1:>10.4f} {r2:>10.4f} {d_len:>+7} {d_ratio:>+8.4f}")
        d_ratios = [d[4] for d in drifts]
        per_km = [d[4] / (d[3] / 1000) for d in drifts if d[3] != 0]
        print(
            f"\n  Δ比: 平均 {statistics.mean(d_ratios):+.4f} "
            f"（正 {sum(1 for d in d_ratios if d > 0)} / 負 "
            f"{sum(1 for d in d_ratios if d < 0)} 件）"
        )
        if per_km:
            print(f"  length 1000m あたりの比の変化: 平均 {statistics.mean(per_km):+.4f}")
        print("  3本目の A で length=4000 だけ比が約1.0だった件の確認材料")

    # ===== 付帯情報 =====
    times = [r.elapsed_s for r in all_rows if r.elapsed_s]
    if times:
        print(f"\n  所要時間: 平均 {statistics.mean(times):.2f}s 最大 {max(times):.2f}s")
    snaps = {r.snap_m for r in all_rows if r.snap_m is not None}
    print(f"  スナップ距離（観測値）: {sorted(snaps)} m")
    remainings = [r.ratelimit_remaining for r in all_rows if r.ratelimit_remaining]
    if remainings:
        print(
            f"  Ratelimit: {max(remainings)} → {min(remainings)}"
            f"（計画 {planned} 回 + 再試行 {extra_calls} 回）"
        )
    failures = sorted({(r.status, r.note[:40]) for r in all_rows if not r.ok})
    print(f"  失敗の内訳: {failures or 'なし'}")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"{stamp}_two_step.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[f.name for f in fields(Row)])
        writer.writeheader()
        for row in all_rows:
            writer.writerow(asdict(row))
    print(f"\n保存: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
