"""ORS の round_trip が返す距離のズレの性質を測る使い捨てスクリプト。

1本目のスパイク（ors_probe.py）で、目標 5,000m に対し実距離 6,177m（+23.5%）と
いう大きなズレが1件見つかった。AC-01-2 の ±300m に対して4倍外れている。
1サンプルでは「系統的に長い」のか「シードごとにばらつく」のか判断できないため、
複数回測って性質を掴む。

調べること::

    1. 同じ length・points でシードだけ変えたときの距離のばらつき
    2. ズレは系統的に長くなるのか、上下にばらつくのか
    3. points（3/5/8）を変えると精度は変わるか
    4. length を小さく指定して補正すれば ±300m に入るか
    5. 1リクエストで X-Ratelimit-Remaining が実際いくつ減るか

処理は2段:

- 第1段（測定）… points × seed の格子で実距離を測り、実距離/指定長 の比を出す
- 第2段（検証）… 第1段で最もばらつきが小さかった points の比から補正係数を作り、
  ``length = 目標 / 比`` を指定して**第1段とは別のシード**で投げ、
  ±300m に何件入るかを数える

第1段と同じシードで検証すると、係数を当てはめた対象そのものを測ることになり
結果が良く出過ぎる。だからシードを分ける。

使い方::

    uv run python spike/ors_distance_calibration.py --dry-run   # 計画だけ表示
    uv run python spike/ors_distance_calibration.py             # 既定 23 回

レスポンス本体は保存しない（1件 50KB 超で、距離しか使わないため）。
各呼び出しの X-Ratelimit-Remaining は CSV に記録する。

検証が終わったら消してよい。本体のパッケージ（runloop/）には依存させない。
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import requests
from dotenv import load_dotenv

ENDPOINT: Final = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"

# .env に HOME_LAT / HOME_LON が無い場合の暫定座標（鹿児島市役所あたり）
FALLBACK_LAT: Final = 31.5966
FALLBACK_LON: Final = 130.5571

OUT_DIR: Final = Path(__file__).parent / "out"

# 受け入基準 AC-01-2 の許容誤差
TOLERANCE_M: Final = 300.0


@dataclass
class Row:
    """1回の呼び出しの測定結果。"""

    stage: str
    seed: int
    points: int
    requested_m: int
    actual_m: float | None = None
    error_m: float | None = None
    error_pct: float | None = None
    status: int | None = None
    ratelimit_remaining: int | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.actual_m is not None


@dataclass
class Stats:
    """誤差率の要約。"""

    n: int
    mean: float
    stdev: float
    minimum: float
    maximum: float
    mean_ratio: float = field(default=0.0)


def build_body(
    *, lat: float, lon: float, length_m: int, points: int, seed: int
) -> dict[str, Any]:
    """リクエスト本体。座標は [経度, 緯度] の順。

    avoid_features と elevation は本番と同じ条件で残す（avoid_features は
    ルート自体を変えるため、外すと測定結果が本番とずれる）。
    instructions は距離の測定に不要なので切って、レスポンスを軽くする。
    """
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


class RateLimited(Exception):
    """レート制限に当たったので以降の呼び出しを止める。"""


def measure(
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
) -> Row:
    """1回だけ呼び出して距離を測る。リトライはしない。"""
    row = Row(stage=stage, seed=seed, points=points, requested_m=length_m)
    try:
        response = requests.post(
            url,
            json=build_body(
                lat=lat, lon=lon, length_m=length_m, points=points, seed=seed
            ),
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/geo+json",
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        row.note = f"{type(exc).__name__}: {exc}"
        return row

    row.status = response.status_code
    remaining = response.headers.get("X-Ratelimit-Remaining")
    if remaining is not None and remaining.isdigit():
        row.ratelimit_remaining = int(remaining)

    if response.status_code == 429:
        row.note = "rate limited"
        raise RateLimited(response.text[:300])
    if response.status_code != 200:
        row.note = response.text[:200].replace("\n", " ")
        return row

    try:
        payload = response.json()
        distance = payload["features"][0]["properties"]["summary"]["distance"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        row.note = f"距離が取り出せない: {type(exc).__name__}"
        return row

    row.actual_m = float(distance)
    # 誤差は「目標距離」に対して測る（指定した length ではない）。
    # 第2段では length を意図的にずらすため、この区別が重要になる。
    row.error_m = row.actual_m - target_m
    row.error_pct = row.error_m / target_m * 100.0
    return row


def summarize(rows: list[Row]) -> Stats | None:
    """誤差率の平均・標準偏差・最小・最大を出す。"""
    values = [r.error_pct for r in rows if r.error_pct is not None]
    if not values:
        return None
    ratios = [
        r.actual_m / r.requested_m for r in rows if r.actual_m and r.requested_m
    ]
    return Stats(
        n=len(values),
        mean=statistics.mean(values),
        # 標本が1件だと標準偏差は定義できないので 0 とする
        stdev=statistics.stdev(values) if len(values) > 1 else 0.0,
        minimum=min(values),
        maximum=max(values),
        mean_ratio=statistics.mean(ratios) if ratios else 0.0,
    )


def print_table(rows: list[Row], target_m: int) -> None:
    """測定結果を表形式で出す。"""
    print(
        f"{'seed':>5} {'points':>6} {'指定length':>10} {'実距離':>9} "
        f"{'誤差':>9} {'誤差率':>8}  {'±300m':>5}"
    )
    print("-" * 62)
    for r in rows:
        if not r.ok:
            print(
                f"{r.seed:>5} {r.points:>6} {r.requested_m:>10} "
                f"{'---':>9} {'---':>9} {'---':>8}  失敗: {r.note[:40]}"
            )
            continue
        assert r.actual_m is not None and r.error_m is not None
        assert r.error_pct is not None
        hit = "○" if abs(r.error_m) <= TOLERANCE_M else "×"
        print(
            f"{r.seed:>5} {r.points:>6} {r.requested_m:>10} "
            f"{r.actual_m:>9.1f} {r.error_m:>+9.1f} {r.error_pct:>+7.1f}%  {hit:>5}"
        )
    print(f"（誤差はいずれも目標 {target_m}m に対する値）")


def print_stats(label: str, stats: Stats | None) -> None:
    if stats is None:
        print(f"  {label}: 有効な結果なし")
        return
    print(
        f"  {label}: n={stats.n}  平均={stats.mean:+.1f}%  "
        f"標準偏差={stats.stdev:.1f}pt  最小={stats.minimum:+.1f}%  "
        f"最大={stats.maximum:+.1f}%  平均比={stats.mean_ratio:.3f}"
    )


def run_stage(
    *,
    url: str,
    api_key: str,
    lat: float,
    lon: float,
    stage: str,
    plan: list[tuple[int, int, int]],
    target_m: int,
    sleep_s: float,
) -> list[Row]:
    """(length, points, seed) の計画を順に実行する。呼び出しの間に待つ。"""
    rows: list[Row] = []
    for index, (length_m, points, seed) in enumerate(plan, start=1):
        print(
            f"  [{index}/{len(plan)}] length={length_m} points={points} seed={seed} ...",
            end="",
            flush=True,
        )
        try:
            row = measure(
                url=url,
                api_key=api_key,
                lat=lat,
                lon=lon,
                stage=stage,
                length_m=length_m,
                points=points,
                seed=seed,
                target_m=target_m,
            )
        except RateLimited as exc:
            print(f" レート制限に当たった: {exc}")
            print("  以降の呼び出しを中止する。")
            break
        rows.append(row)
        if row.ok:
            print(f" {row.actual_m:.0f}m（{row.error_pct:+.1f}%）")
        else:
            print(f" 失敗 status={row.status} {row.note[:60]}")
        if index < len(plan):
            time.sleep(sleep_s)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=5000, help="目標距離（m）")
    parser.add_argument(
        "--points", default="3,5,8", help="比較する points の値（カンマ区切り）"
    )
    parser.add_argument(
        "--seeds", type=int, default=5, help="第1段で points ごとに試すシード数"
    )
    parser.add_argument(
        "--verify-seeds", type=int, default=8, help="第2段（補正の検証）のシード数"
    )
    parser.add_argument(
        "--max-calls", type=int, default=30, help="呼び出し回数の上限。超える計画は中止"
    )
    parser.add_argument("--sleep", type=float, default=1.0, help="呼び出し間の待機秒")
    parser.add_argument("--profile", default="foot-walking")
    parser.add_argument(
        "--no-verify", action="store_true", help="第2段（補正の検証）を行わない"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="計画を表示するだけで呼び出さない"
    )
    args = parser.parse_args()

    points_list = [int(p) for p in args.points.split(",") if p.strip()]
    stage1_calls = len(points_list) * args.seeds
    stage2_calls = 0 if args.no_verify else args.verify_seeds
    total = stage1_calls + stage2_calls

    print(f"目標距離   : {args.target}m")
    print(f"第1段（測定）: points={points_list} × seed 1..{args.seeds} = {stage1_calls} 回")
    print(f"第2段（検証）: {stage2_calls} 回（別シード）")
    print(f"合計       : {total} 回（上限 {args.max_calls}）")

    if total > args.max_calls:
        print(f"\n計画が上限 {args.max_calls} 回を超えている。1回も呼び出さずに中止する。")
        print("--seeds / --verify-seeds を減らすか、--max-calls を上げる。")
        return 2
    if args.dry_run:
        print("\n--dry-run なので呼び出さずに終了する。")
        return 0

    load_dotenv()
    api_key = os.getenv("ORS_API_KEY")
    if not api_key:
        print("\nORS_API_KEY が設定されていない。.env か環境変数に設定して再実行する。")
        return 2

    lat = float(os.getenv("HOME_LAT") or FALLBACK_LAT)
    lon = float(os.getenv("HOME_LON") or FALLBACK_LON)
    if not os.getenv("HOME_LAT"):
        print(f"HOME_LAT/HOME_LON が未設定のため暫定座標を使う: {lat}, {lon}")

    url = ENDPOINT.format(profile=args.profile)

    # --- 第1段: points × seed の格子 ---
    print("\n=== 第1段: 素の指定で測る ===")
    plan1 = [
        (args.target, points, seed)
        for points in points_list
        for seed in range(1, args.seeds + 1)
    ]
    rows1 = run_stage(
        url=url,
        api_key=api_key,
        lat=lat,
        lon=lon,
        stage="measure",
        plan=plan1,
        target_m=args.target,
        sleep_s=args.sleep,
    )

    print("\n--- 第1段の結果 ---")
    print_table(rows1, args.target)

    print("\n--- 誤差率の要約 ---")
    per_points: dict[int, Stats] = {}
    for points in points_list:
        subset = [r for r in rows1 if r.points == points]
        stats = summarize(subset)
        print_stats(f"points={points}", stats)
        if stats is not None:
            per_points[points] = stats
    overall1 = summarize(rows1)
    print_stats("全体      ", overall1)

    if overall1 is not None:
        positives = sum(1 for r in rows1 if r.error_m is not None and r.error_m > 0)
        print(
            f"\n  2. ズレの向き: 長い側 {positives} 件 / 短い側 "
            f"{overall1.n - positives} 件"
            "  → 全部が長い側なら系統的、混ざっていればばらつき"
        )
        hits = sum(
            1 for r in rows1 if r.error_m is not None and abs(r.error_m) <= TOLERANCE_M
        )
        print(f"  補正なしで ±300m に入った件数: {hits} / {overall1.n}")

    rows2: list[Row] = []
    if not args.no_verify and per_points:
        # 補正が効くのは「ズレが系統的」なときだけ。ばらつきが大きいと係数では
        # 直らないので、平均が小さい points ではなく標準偏差が最小の points を選ぶ。
        best_points = min(per_points, key=lambda p: per_points[p].stdev)
        ratio = per_points[best_points].mean_ratio
        if ratio <= 0:
            print("\n比が計算できないため第2段を行わない。")
        else:
            corrected = round(args.target / ratio)
            print("\n=== 第2段: 補正して測る ===")
            print(
                f"ばらつき最小の points={best_points}"
                f"（標準偏差 {per_points[best_points].stdev:.1f}pt、"
                f"平均比 {ratio:.3f}）を採用"
            )
            print(f"指定 length を {args.target} → {corrected}m に補正する")
            print(f"シードは第1段と重ならない {args.seeds + 101}.. を使う")
            plan2 = [
                (corrected, best_points, seed)
                for seed in range(args.seeds + 101, args.seeds + 101 + stage2_calls)
            ]
            rows2 = run_stage(
                url=url,
                api_key=api_key,
                lat=lat,
                lon=lon,
                stage="verify",
                plan=plan2,
                target_m=args.target,
                sleep_s=args.sleep,
            )
            print("\n--- 第2段の結果 ---")
            print_table(rows2, args.target)
            stats2 = summarize(rows2)
            print("\n--- 誤差率の要約 ---")
            print_stats("補正後    ", stats2)
            if stats2 is not None:
                hits2 = sum(
                    1
                    for r in rows2
                    if r.error_m is not None and abs(r.error_m) <= TOLERANCE_M
                )
                print(
                    f"\n  4. 補正後に ±300m に入った件数: {hits2} / {stats2.n}"
                    f"  → 1件でも入れば「シードを振れば当たりが出る」と言える"
                )

    # --- 5. Ratelimit の消費 ---
    all_rows = rows1 + rows2
    remainings = [r.ratelimit_remaining for r in all_rows if r.ratelimit_remaining]
    print("\n--- 5. X-Ratelimit-Remaining の推移 ---")
    if len(remainings) >= 2:
        # 1回目の呼び出しが何消費したかは、その前の値を知らないので分からない。
        # 分かるのは「2回目以降」の減り方だけなので、そこだけを根拠にする。
        deltas = [before - after for before, after in pairwise(remainings)]
        print(f"  最初: {remainings[0]}  最後: {remainings[-1]}")
        print(
            f"  ヘッダが取れた呼び出し: {len(remainings)} 回  "
            f"うち2回目以降 {len(deltas)} 回で減った量: {remainings[0] - remainings[-1]}"
        )
        print(f"  1回あたりの減少量（観測値）: {sorted(set(deltas))}")
    else:
        print("  ヘッダが取れなかった")

    # --- CSV に保存（レスポンス本体は保存しない）---
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    csv_path = OUT_DIR / f"{stamp}_distance_calibration.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[f.name for f in fields(Row)])
        writer.writeheader()
        for row in all_rows:
            writer.writerow(asdict(row))
    print(f"\n保存: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
