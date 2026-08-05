"""ORS round_trip の実用性を3つの観点で確かめる使い捨てスクリプト。

2本目のスパイクで、全シード共通の補正係数は効かないと分かった（ズレはシード
ごとに独立で、標準偏差が許容 ±300m の3.5倍ある）。残った望みは「シードごとの
補正」で、それが成立するかを A で判定する。

段の構成（この順に実行する）::

    A. 線形性（最優先）… seed を固定して length を変え、実距離が単調かつ
       比例的に動くか。比例するなら「1本目で実測 → 比から補正長 → 2本目」の
       2段方式が成立する。形ごと変わるなら、この方向は閉じる。
    C. 並列化の可否 … 同時 3/5/10 本で 429 や接続エラーが出るか。
       B の50回を並列で回せるかがここで決まる。
    B. 命中率の再推定 … points=3 で seed を50個振り、±300m 命中率の
       信頼区間を狭める。異常値と404の発生率も測る。

D（所要時間）は独立した段ではなく、全呼び出しで計測して各段の表に出す。

段ごとに独立して起動できる::

    uv run python spike/ors_feasibility.py --stages a          # A だけ
    uv run python spike/ors_feasibility.py --stages c          # C だけ
    uv run python spike/ors_feasibility.py --stages b          # B だけ
    uv run python spike/ors_feasibility.py --dry-run           # 計画のみ

既定は a,c,b の一括実行。ただし C で並列が危険と判定されたら B は自動で
スキップする（直列50回は時間がかかるため、続行は明示的な指示を要求する）。

レスポンス本体は、目標の3倍を超える異常ルートが出たケースだけ保存する
（2本目で 415km の中身を確認できなかったため）。

検証が終わったら消してよい。本体のパッケージ（runloop/）には依存させない。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import requests
from dotenv import load_dotenv

ENDPOINT: Final = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"
FALLBACK_LAT: Final = 31.5966
FALLBACK_LON: Final = 130.5571
OUT_DIR: Final = Path(__file__).parent / "out"

TOLERANCE_M: Final = 300.0  # AC-01-2
ORIGIN_TOLERANCE_M: Final = 50.0  # AC-01-3
DEGENERATE_FACTOR: Final = 3.0  # 目標の何倍を超えたら異常ルートとみなすか


@dataclass
class Row:
    """1回の呼び出しの記録。"""

    stage: str
    seed: int
    points: int
    requested_m: int
    concurrency: int = 1
    elapsed_s: float | None = None
    actual_m: float | None = None
    error_m: float | None = None
    error_pct: float | None = None
    # 合計距離方式の評価用。合計 = ループ距離 + 接近距離(スナップ) × 2。
    # 接近区間は道路を通るので直線距離は下限であり、この値は過小評価になる。
    total_m: float | None = None
    total_error_m: float | None = None
    # AC-01-3 の実測用。ORS がスナップした始点・終点が起点から何m離れるか
    snap_start_m: float | None = None
    snap_end_m: float | None = None
    closure_m: float | None = None
    status: int | None = None
    ratelimit_remaining: int | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.actual_m is not None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離（m）。地球を半径 6371km の球とみなす。

    50m 前後の判定に使うので、球近似で十分（楕円体との差は 0.5% 未満）。
    """
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二項比率の Wilson 信頼区間（既定 95%）。

    単純な ±1.96·√(p(1-p)/n) は、p が 0 に近いと下限が負になって使えない。
    Wilson なら少数の当たりでも妥当な区間が出る。
    """
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def build_body(
    *, lat: float, lon: float, length_m: int, points: int, seed: int
) -> dict[str, Any]:
    """本番と同じルーティング条件で組み立てる。座標は [経度, 緯度] の順。"""
    return {
        "coordinates": [[lon, lat]],
        "options": {
            "round_trip": {"length": length_m, "points": points, "seed": seed},
            "avoid_features": ["steps"],
        },
        "elevation": True,
        "instructions": False,
        "units": "m",
    }


def call(
    *,
    url: str,
    api_key: str,
    lat: float,
    lon: float,
    stage: str,
    length_m: int,
    points: int,
    seed: int,
    target_m: int,
    concurrency: int,
    stamp: str,
) -> Row:
    """1回だけ呼び出す。リトライしない。所要時間を必ず記録する。"""
    row = Row(
        stage=stage,
        seed=seed,
        points=points,
        requested_m=length_m,
        concurrency=concurrency,
    )
    body = build_body(lat=lat, lon=lon, length_m=length_m, points=points, seed=seed)
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
        row.note = f"{type(exc).__name__}: {exc}"[:200]
        return row
    row.elapsed_s = round(time.perf_counter() - started, 2)
    row.status = response.status_code

    remaining = response.headers.get("X-Ratelimit-Remaining")
    if remaining is not None and remaining.isdigit():
        row.ratelimit_remaining = int(remaining)

    if response.status_code != 200:
        # 429 や接続エラーは C の判定材料なので、本体とヘッダを必ず残す
        row.note = response.text[:200].replace("\n", " ")
        if response.status_code == 429:
            path = OUT_DIR / f"{stamp}_ratelimited_{stage}_seed{seed}.json"
            path.write_text(
                json.dumps(
                    {
                        "status": response.status_code,
                        "headers": dict(response.headers),
                        "body": response.text[:4000],
                        "concurrency": concurrency,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return row

    try:
        payload = response.json()
        props = payload["features"][0]["properties"]
        coords = payload["features"][0]["geometry"]["coordinates"]
        row.actual_m = float(props["summary"]["distance"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        row.note = f"距離が取り出せない: {type(exc).__name__}"
        return row

    row.error_m = row.actual_m - target_m
    row.error_pct = row.error_m / target_m * 100.0

    # AC-01-3 の実測。coords は [経度, 緯度, 標高] の順
    if coords:
        first, last = coords[0], coords[-1]
        row.snap_start_m = round(haversine_m(lat, lon, first[1], first[0]), 1)
        row.snap_end_m = round(haversine_m(lat, lon, last[1], last[0]), 1)
        row.closure_m = round(haversine_m(first[1], first[0], last[1], last[0]), 1)
        row.total_m = round(row.actual_m + row.snap_start_m * 2, 1)
        row.total_error_m = round(row.total_m - target_m, 1)

    # 異常ルートだけ本体を保存する（前回 415km の中身を確認できなかったため）
    if row.actual_m > target_m * DEGENERATE_FACTOR:
        path = OUT_DIR / f"{stamp}_degenerate_{stage}_seed{seed}_len{length_m}.json"
        path.write_text(
            json.dumps(redact_geometry(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        row.note = f"異常ルート。本体を保存（座標は相対化）: {path.name}"
    return row


def redact_geometry(payload: dict[str, Any]) -> dict[str, Any]:
    """絶対座標を落として保存できる形にする。

    異常ルートの原因を見るのに必要なのは「形と規模」であって絶対位置ではない。
    先頭点を原点とする相対座標に変換すれば、起点（＝自宅の近傍）を書き出さずに
    診断できる。bbox も絶対座標なので削る。
    """
    redacted = json.loads(json.dumps(payload))
    redacted.pop("bbox", None)
    for feature in redacted.get("features", []):
        feature.pop("bbox", None)
        coords = feature.get("geometry", {}).get("coordinates")
        if not coords:
            continue
        base_lon, base_lat = coords[0][0], coords[0][1]
        feature["geometry"]["coordinates"] = [
            [round(c[0] - base_lon, 6), round(c[1] - base_lat, 6), *c[2:]]
            for c in coords
        ]
    redacted["_note"] = (
        "座標は先頭点を原点とする相対値（度）。絶対位置は意図的に除去している。"
        "bbox も削除済み。形と規模の診断にのみ使う。"
    )
    return redacted


def print_table(rows: list[Row], *, show_seed_length: bool = True) -> None:
    """段ごとの結果を表で出す。"""
    header = (
        f"{'seed':>5} {'len':>6} {'並列':>4} {'秒':>6} {'ループ':>9} "
        f"{'誤差':>9} {'誤差率':>8} {'接近':>6} {'合計':>9} {'合計誤差':>9} {'±300':>5}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        if not r.ok:
            print(
                f"{r.seed:>5} {r.requested_m:>6} {r.concurrency:>4} "
                f"{r.elapsed_s or 0:>6.2f} {'---':>9} {'---':>9} {'---':>8} "
                f"{'---':>7} {'---':>7} {'---':>6}  失敗 {r.status} {r.note[:30]}"
            )
            continue
        assert r.actual_m is not None and r.error_m is not None
        assert r.error_pct is not None
        # 判定は合計距離基準（新方式）。ループ距離基準の誤差も並べて出す
        hit = "○" if abs(r.total_error_m or r.error_m) <= TOLERANCE_M else "×"
        print(
            f"{r.seed:>5} {r.requested_m:>6} {r.concurrency:>4} "
            f"{r.elapsed_s or 0:>6.2f} {r.actual_m:>9.1f} {r.error_m:>+9.1f} "
            f"{r.error_pct:>+7.1f}% {r.snap_start_m or 0:>6.1f} "
            f"{r.total_m or 0:>9.1f} {r.total_error_m or 0:>+9.1f} {hit:>5}"
        )
    if show_seed_length:
        print("（接近=起点からルート始点までの直線距離m、合計=ループ+接近×2）")
        print("（±300 の判定は合計距離基準。接近区間は直線近似のため過小評価）")


def run_serial(plan: list[dict[str, Any]], *, sleep_s: float, **shared: Any) -> list[Row]:
    """直列に実行する。所要時間の基準値になる。"""
    rows: list[Row] = []
    for index, item in enumerate(plan, start=1):
        row = call(concurrency=1, **item, **shared)
        rows.append(row)
        state = f"{row.actual_m:.0f}m" if row.ok else f"失敗{row.status}"
        print(f"  [{index}/{len(plan)}] {item} -> {state} ({row.elapsed_s}s)")
        if index < len(plan):
            time.sleep(sleep_s)
    return rows


def run_parallel(
    plan: list[dict[str, Any]], *, workers: int, **shared: Any
) -> tuple[list[Row], float]:
    """同時に投げる。戻り値は (結果, 全体の壁時計秒)。"""
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(call, concurrency=workers, **item, **shared) for item in plan
        ]
        rows = [f.result() for f in futures]
    return rows, round(time.perf_counter() - started, 2)


def interp(x: float, xs: list[float], ys: list[float]) -> float | None:
    """測定点の間を線形補間する。範囲外は None（外挿はしない）。"""
    if len(xs) < 2 or x < xs[0] or x > xs[-1]:
        return None
    for left, right, y_left, y_right in zip(xs, xs[1:], ys, ys[1:], strict=False):
        if left <= x <= right:
            if right == left:
                return y_left
            return y_left + (y_right - y_left) * (x - left) / (right - left)
    return None


def analyse_stage_a(rows: list[Row], target_m: int, lengths: list[int]) -> None:
    """A の判定: 単調性、比の安定性、2段方式が成立するかの推定。"""
    print("\n--- A の判定 ---")
    seeds = sorted({r.seed for r in rows})
    verdicts: list[bool] = []
    for seed in seeds:
        subset = [r for r in rows if r.seed == seed and r.ok]
        subset.sort(key=lambda r: r.requested_m)
        if len(subset) < 3:
            print(f"  seed={seed}: 有効データ {len(subset)} 件。判定不能")
            continue
        xs = [float(r.requested_m) for r in subset]
        ys = [float(r.actual_m or 0) for r in subset]
        ratios = [y / x for x, y in zip(xs, ys, strict=False)]
        monotonic = all(b >= a for a, b in pairwise(ys))
        ratio_sd = statistics.stdev(ratios) * 100 if len(ratios) > 1 else 0.0
        print(
            f"  seed={seed}: 単調={'はい' if monotonic else 'いいえ'}  "
            f"比={[round(r, 3) for r in ratios]}  比の標準偏差={ratio_sd:.1f}pt"
        )
        # 2段方式の retrospective 検証:
        # 目標長で測った比から補正長を求め、その長さでの実距離を補間で予測する
        at_target = next((r for r in subset if r.requested_m == target_m), None)
        if at_target is None or not at_target.actual_m:
            continue
        ratio_at_target = at_target.actual_m / target_m
        corrected = target_m / ratio_at_target
        predicted = interp(corrected, xs, ys)
        if predicted is None:
            print(
                f"      補正長 {corrected:.0f}m は測定範囲外のため予測不能"
                f"（範囲 {lengths[0]}〜{lengths[-1]}m）"
            )
            continue
        err = predicted - target_m
        verdicts.append(abs(err) <= TOLERANCE_M)
        print(
            f"      2段方式の予測: 補正長 {corrected:.0f}m → 予測実距離 "
            f"{predicted:.0f}m（誤差 {err:+.0f}m）"
            f" {'○ ±300m 以内' if abs(err) <= TOLERANCE_M else '× 外れ'}"
        )
    if verdicts:
        print(
            f"\n  2段方式が ±300m に入ると予測されたシード: "
            f"{sum(verdicts)}/{len(verdicts)}"
        )
        print("  （補間による予測なので、実測での確認は別途必要）")


def analyse_hit_rate(rows: list[Row], target_m: int, label: str) -> None:
    """B の判定: 命中率と信頼区間、異常値・404 の発生率。"""
    n = len(rows)
    ok = [r for r in rows if r.ok]
    degenerate = [r for r in ok if (r.actual_m or 0) > target_m * DEGENERATE_FACTOR]
    failed = [r for r in rows if not r.ok]
    hits = [r for r in ok if abs(r.error_m or 1e9) <= TOLERANCE_M]
    lo, hi = wilson_interval(len(hits), n)
    print(f"\n--- {label} ---")
    print(f"  呼び出し {n} 回: 応答成功 {len(ok)} / 失敗 {len(failed)} / 異常ルート {len(degenerate)}")
    print(f"  ±300m 命中（ループ距離基準）: {len(hits)} 件 = {len(hits) / n * 100:.1f}%")
    print(f"  95% 信頼区間: {lo * 100:.1f}% 〜 {hi * 100:.1f}%")

    # 合計距離方式（ループ + 接近×2）で判定した場合の命中率。
    # スナップは起点だけで決まる定数なので、目標を 2×スナップ ぶん手前に
    # 置いたのと等価になる。方式の比較のために両方出す。
    total_hits = [r for r in ok if abs(r.total_error_m or 1e9) <= TOLERANCE_M]
    if any(r.total_error_m is not None for r in ok):
        tlo, thi = wilson_interval(len(total_hits), n)
        print(
            f"  ±300m 命中（合計距離基準）: {len(total_hits)} 件 = "
            f"{len(total_hits) / n * 100:.1f}%  95%CI {tlo * 100:.1f}%〜{thi * 100:.1f}%"
        )
    sane = [r for r in ok if r not in degenerate]
    if len(sane) > 1:
        pcts = [r.error_pct or 0 for r in sane]
        print(
            f"  誤差率（異常値除く n={len(pcts)}）: 平均 {statistics.mean(pcts):+.1f}% "
            f"標準偏差 {statistics.stdev(pcts):.1f}pt "
            f"最小 {min(pcts):+.1f}% 最大 {max(pcts):+.1f}%"
        )
    print(f"  失敗の内訳: {sorted({(r.status, r.note[:40]) for r in failed}) or 'なし'}")
    if lo > 0:
        print("\n  この命中率で「1本以上当たる」確率（下限〜点推定で計算）:")
        for calls in (5, 8, 10, 15):
            print(
                f"    {calls:>2}回: {1 - (1 - lo) ** calls:.1%} 〜 "
                f"{1 - (1 - len(hits) / n) ** calls:.1%}"
            )


def analyse_snap(rows: list[Row]) -> None:
    """AC-01-3 の実測結果。全段のデータをまとめて見る。"""
    starts = [r.snap_start_m for r in rows if r.snap_start_m is not None]
    closures = [r.closure_m for r in rows if r.closure_m is not None]
    print("\n--- AC-01-3 の実測（起点からのスナップ距離）---")
    if not starts:
        print("  データなし")
        return
    over = sum(1 for s in starts if s > ORIGIN_TOLERANCE_M)
    print(
        f"  始点までの距離 n={len(starts)}: 最小 {min(starts):.1f}m "
        f"中央 {statistics.median(starts):.1f}m 最大 {max(starts):.1f}m"
    )
    print(f"  50m を超えた件数: {over} / {len(starts)}")
    if closures:
        print(
            f"  始点と終点の距離: 最小 {min(closures):.1f}m "
            f"最大 {max(closures):.1f}m（0 なら完全な周回）"
        )
    print("  ※ スナップ先は起点座標だけで決まるため、シードを変えても同じになるはず")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=5000)
    parser.add_argument("--points", type=int, default=3)
    parser.add_argument("--stages", default="a,c,b", help="実行する段（a/c/b）")
    parser.add_argument("--a-seeds", default="1,2,3")
    parser.add_argument("--a-lengths", default="4000,4500,5000,5500,6000")
    parser.add_argument("--c-batches", default="3,5,10")
    parser.add_argument("--b-seeds", type=int, default=50)
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=1.0, help="直列時の待機秒")
    parser.add_argument(
        "--force-b-serial",
        action="store_true",
        help="C で並列が危険と判定されても B を直列で実行する",
    )
    parser.add_argument("--profile", default="foot-walking")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stages = [s.strip().lower() for s in args.stages.split(",") if s.strip()]
    a_seeds = [int(s) for s in args.a_seeds.split(",") if s.strip()]
    a_lengths = sorted(int(s) for s in args.a_lengths.split(",") if s.strip())
    c_batches = [int(s) for s in args.c_batches.split(",") if s.strip()]

    calls_a = len(a_seeds) * len(a_lengths) if "a" in stages else 0
    calls_c = sum(c_batches) if "c" in stages else 0
    calls_b = args.b_seeds if "b" in stages else 0
    total = calls_a + calls_c + calls_b

    print(f"目標距離 : {args.target}m / points={args.points}")
    print(f"A 線形性 : {len(a_seeds)} seeds × {len(a_lengths)} lengths = {calls_a} 回（直列）")
    print(f"C 並列   : バッチ {c_batches} = {calls_c} 回")
    print(f"B 命中率 : {calls_b} 回")
    print(f"合計     : {total} 回（上限 {args.max_calls}）")
    if total > args.max_calls:
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
        print(f"HOME_LAT/HOME_LON が未設定のため暫定座標を使う: {lat}, {lon}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    shared = {
        "url": ENDPOINT.format(profile=args.profile),
        "api_key": api_key,
        "lat": lat,
        "lon": lon,
        "target_m": args.target,
        "stamp": stamp,
    }

    all_rows: list[Row] = []
    parallel_ok = 0  # エラーが出なかった最大の同時数

    # ===== A: 線形性 =====
    if "a" in stages:
        print("\n=== A: シード固定・length 可変（直列）===")
        plan = [
            {"stage": "A", "seed": seed, "points": args.points, "length_m": length}
            for seed in a_seeds
            for length in a_lengths
        ]
        rows_a = run_serial(plan, sleep_s=args.sleep, **shared)
        all_rows += rows_a
        print("\n--- A の結果 ---")
        print_table(rows_a)
        analyse_stage_a(rows_a, args.target, a_lengths)
        serial_times = [r.elapsed_s for r in rows_a if r.elapsed_s]
        if serial_times:
            print(
                f"\n  D 直列の所要時間: 平均 {statistics.mean(serial_times):.2f}s "
                f"最小 {min(serial_times):.2f}s 最大 {max(serial_times):.2f}s"
            )

    # ===== C: 並列化の可否 =====
    if "c" in stages:
        print("\n=== C: 並列化の可否 ===")
        seed_base = 200
        for batch in c_batches:
            plan = [
                {
                    "stage": f"C{batch}",
                    "seed": seed_base + i,
                    "points": args.points,
                    "length_m": args.target,
                }
                for i in range(batch)
            ]
            seed_base += batch
            print(f"\n  同時 {batch} 本を投げる...")
            rows_c, wall = run_parallel(plan, workers=batch, **shared)
            all_rows += rows_c
            errors = [r for r in rows_c if r.status != 200]
            times = [r.elapsed_s for r in rows_c if r.elapsed_s]
            print(
                f"  壁時計 {wall}s / 個別 平均 {statistics.mean(times):.2f}s "
                f"最大 {max(times):.2f}s / エラー {len(errors)} 件"
            )
            if errors:
                for r in errors:
                    print(f"    status={r.status} {r.note[:120]}")
            has_429 = any(r.status == 429 for r in rows_c)
            has_transport = any(r.status is None for r in rows_c)
            if has_429 or has_transport:
                print(f"  → 同時 {batch} 本は危険（429 または接続エラー）。ここで打ち切る")
                break
            parallel_ok = batch
            time.sleep(args.sleep * 2)
        print(f"\n  C の判定: エラーなしで通った最大同時数 = {parallel_ok}")

    # ===== B: 命中率の再推定 =====
    if "b" in stages:
        run_b = True
        workers = parallel_ok
        if "c" in stages and parallel_ok == 0:
            if args.force_b_serial:
                print("\nC で並列が通らなかったが --force-b-serial のため直列で実行する。")
                workers = 1
            else:
                print(
                    f"\n=== B をスキップする ===\n"
                    f"C で並列が通らなかったため、B の {args.b_seeds} 回は直列になり"
                    f"時間がかかる（1回 3s + 待機 {args.sleep}s なら約"
                    f"{args.b_seeds * (3 + args.sleep) / 60:.0f}分）。\n"
                    f"続けるなら --stages b --force-b-serial で明示的に実行する。"
                )
                run_b = False
        elif "c" not in stages:
            workers = 1  # C を回していないなら安全側に直列

        if run_b:
            print(f"\n=== B: 命中率の再推定（{args.b_seeds} 回, 同時 {max(workers, 1)} 本）===")
            plan = [
                {
                    "stage": "B",
                    "seed": 1000 + i,
                    "points": args.points,
                    "length_m": args.target,
                }
                for i in range(args.b_seeds)
            ]
            if workers > 1:
                rows_b: list[Row] = []
                # C で確認できた同時数を超えないよう、バッチに区切って投げる
                for start in range(0, len(plan), workers):
                    chunk = plan[start : start + workers]
                    part, wall = run_parallel(chunk, workers=len(chunk), **shared)
                    rows_b += part
                    done = min(start + workers, len(plan))
                    print(f"  {done}/{len(plan)} 完了（このバッチ {wall}s）")
                    if any(r.status == 429 for r in part):
                        print("  429 が出たので中止する")
                        break
                    time.sleep(args.sleep)
            else:
                rows_b = run_serial(plan, sleep_s=args.sleep, **shared)
            all_rows += rows_b
            print("\n--- B の結果 ---")
            print_table(rows_b)
            analyse_hit_rate(rows_b, args.target, "B 単独")
            pooled = [r for r in all_rows if r.stage.startswith(("B", "C"))]
            analyse_hit_rate(pooled, args.target, "B + C 合算（同条件のため）")

    if not all_rows:
        print("\n実行した呼び出しがない。")
        return 0

    analyse_snap(all_rows)

    times = [r.elapsed_s for r in all_rows if r.elapsed_s]
    print("\n--- D 所要時間の全体 ---")
    for label, group in (
        ("直列(A)", [r for r in all_rows if r.concurrency == 1 and r.elapsed_s]),
        ("並列", [r for r in all_rows if r.concurrency > 1 and r.elapsed_s]),
    ):
        vals = [r.elapsed_s or 0 for r in group]
        if vals:
            print(
                f"  {label}: n={len(vals)} 平均 {statistics.mean(vals):.2f}s "
                f"最小 {min(vals):.2f}s 最大 {max(vals):.2f}s"
            )
    if times:
        print(f"  全体: n={len(times)} 平均 {statistics.mean(times):.2f}s")

    remainings = [r.ratelimit_remaining for r in all_rows if r.ratelimit_remaining]
    if remainings:
        print(f"\n  Ratelimit: {max(remainings)} → {min(remainings)}（呼び出し {len(all_rows)} 回）")

    csv_path = OUT_DIR / f"{stamp}_feasibility.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[f.name for f in fields(Row)])
        writer.writeheader()
        for row in all_rows:
            writer.writerow(asdict(row))
    print(f"\n保存: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
