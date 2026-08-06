"""points=3 の directions と /v2/snap を実データで1件ずつ確認する使い捨てスクリプト。

docs/design.md 11節の要検証 #4 / #12 / #13 を埋めるために使う。

- #4  points=3・``instructions: true`` の steps（step 数・``"-"`` の割合・type の分布）
- #12 ``/v2/snap`` のスナップ先が directions の始点と一致するか
- #13 ``/v2/snap`` が directions と**同じ無料枠**を消費するか

**投げるのはちょうど3回。** 順番に意味がある。

1. directions（seed=1）… fixture にする1件。ここで残数 A を読む
2. ``/v2/snap``        … スナップ先と残数 B を読む
3. directions（seed=2）… 残数 C を読む

#13 は残数 A と C の差で判定する。**C = A - 1 なら snap は directions の枠を
消費していない。C = A - 2 なら消費している。** snap 側の残数 B だけを見ても、
別建ての枠なのか同じ枠なのかが区別できないため、この3回目が必要になる。
3回目のシードを変えるのは、同じ本文だと途中で結果が再利用されて
実際の消費が観測できない可能性を避けるため。

**起点は暫定座標を使い、``HOME_LAT`` / ``HOME_LON`` を読まない。**
fixture は tests/fixtures/ に入れてコミットするため、自宅座標が混ざってはいけない
（design.md 10.3、T05 の完了条件）。spike の他の4本は未設定時に暫定座標へ
落ちる形だが、このスクリプトは**設定されていても暫定座標を使う**。

使い方（``-m`` で実行する。``runloop.geo`` を import するため）::

    uv run python -m spike.ors_points3_snap             # 3回投げる
    uv run python -m spike.ors_points3_snap --dry-run   # 投げずに本文だけ表示

結果は spike/out/ に保存する。``*_response.json`` がレスポンス本体、
``*_meta.json`` がステータスとヘッダと投げた本文。

**距離の算出に ``runloop.geo.haversine`` を使う。** #12 は「本体が算出する接近距離と
snap の値が一致するか」の検証なので、同じ関数で測らないと比較の意味が薄れる。
依存の向きは spike → runloop であり、逆（本体が spike を参照する）ではない。
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import requests
from dotenv import load_dotenv

from runloop.geo import haversine
from runloop.models import LatLon

DIRECTIONS_URL: Final = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"

# snap は GeoJSON を受け付けない（実測。/geojson を付けると 406 code 8007
# "This response format is not supported"）。素の JSON で受ける。
# **この 406 は無料枠を消費しなかった**（残数ヘッダが変わらなかった）
SNAP_URL: Final = "https://api.openrouteservice.org/v2/snap/foot-walking"

# 検証用の暫定座標（鹿児島市役所あたり）。実際の起点ではない。
# **HOME_LAT / HOME_LON は読まない**（この値は fixture としてコミットされる）
PROBE: Final = LatLon(lat=31.5966, lon=130.5571)

# 本体と同じ値を使う（runloop/config.py の SNAP_RADIUS_M / design.md 4.6.1）
SNAP_RADIUS: Final = 350

TARGET_M: Final = 5_000
POINTS: Final = 3

OUT_DIR: Final = Path(__file__).parent / "out"


def directions_body(*, seed: int) -> dict[str, Any]:
    """directions のリクエスト本体。本体（T07）が投げる形に合わせる。

    ORS の座標は [経度, 緯度] の順（緯度・経度ではない）。
    """
    return {
        "coordinates": [[PROBE.lon, PROBE.lat]],
        "options": {
            "round_trip": {"length": TARGET_M, "points": POINTS, "seed": seed},
            "avoid_features": ["steps"],  # AC-03-1
        },
        "instructions": True,  # steps を得る（要検証 #4）
        "instructions_format": "text",
        "elevation": True,  # ascent / descent（AC-03-3）
        "units": "m",
    }


def snap_body() -> dict[str, Any]:
    """/v2/snap のリクエスト本体（design.md 4.6.1）。"""
    return {"locations": [[PROBE.lon, PROBE.lat]], "radius": SNAP_RADIUS}


def ratelimit_headers(headers: dict[str, str]) -> dict[str, str]:
    """残数系のヘッダだけ抜き出す（名前の揺れに備えて前方一致で拾う）。"""
    return {k: v for k, v in headers.items() if "ratelimit" in k.lower() or "quota" in k.lower()}


def post(
    *, label: str, url: str, body: dict[str, Any], api_key: str, stamp: str, accept: str
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """1回だけ投げて、本文とヘッダを保存する。リトライはしない。"""
    print(f"\n=== {label} ===")
    print(f"POST {url}")
    print(f"body {json.dumps(body, ensure_ascii=False)}")

    response = requests.post(
        url,
        json=body,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": accept,
        },
        timeout=60,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None

    headers = dict(response.headers)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = OUT_DIR / f"{stamp}_{label}"
    if payload is not None:
        base.with_name(base.name + "_response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    # meta にリクエストヘッダ（= API キー）を入れない
    base.with_name(base.name + "_meta.json").write_text(
        json.dumps(
            {
                "requested_at": stamp,
                "url": url,
                "request_body": body,
                "status_code": response.status_code,
                "response_headers": headers,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"status {response.status_code}")
    print(f"残数系ヘッダ {ratelimit_headers(headers)}")
    if response.status_code != 200:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:1500] if payload else response.text[:1500])
    return payload, headers


def steps_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """レスポンスから steps を集める（キーの存在を仮定しない）。"""
    features = payload.get("features") or []
    if not features:
        return []
    steps: list[dict[str, Any]] = []
    for segment in features[0].get("properties", {}).get("segments", []):
        steps.extend(segment.get("steps", []))
    return steps


def report_steps(payload: dict[str, Any]) -> None:
    """要検証 #4: step 数・name の割合・maneuver の分布。"""
    print("\n--- 要検証 #4: points=3・instructions=true の steps ---")
    features = payload.get("features") or []
    if not features:
        print("  features が空")
        return
    props = features[0].get("properties", {})
    summary = props.get("summary", {})
    coords = features[0].get("geometry", {}).get("coordinates", [])
    print(f"  ループ距離   : {summary.get('distance')} m")
    print(f"  ascent/descent: {props.get('ascent')} / {props.get('descent')}")
    print(f"  座標数       : {len(coords)}（次元 {len(coords[0]) if coords else 0}）")

    steps = steps_of(payload)
    print(f"  step 数      : {len(steps)}")
    if not steps:
        return

    names = Counter("-" if step.get("name") == "-" else "名前あり" for step in steps)
    dash = names.get("-", 0)
    print(f"  name が \"-\"  : {dash} / {len(steps)} 件（{dash / len(steps):.1%}）")

    print("  maneuver の分布（type: 件数）:")
    turn_types = {0, 1, 2, 3, 4, 5}  # design.md 7.1 のホワイトリスト
    counts = Counter(step.get("type") for step in steps)
    for step_type, count in sorted(counts.items(), key=lambda item: str(item[0])):
        mark = "方向転換" if step_type in turn_types else "（対象外）"
        print(f"    type={step_type!r:>5} : {count:3d} 件  {mark}")
    turns = sum(count for t, count in counts.items() if t in turn_types)
    print(f"  方向転換の件数: {turns} 件（5件への間引きが{'必要' if turns > 5 else '不要'}）")


def report_snap(directions: dict[str, Any], snap: dict[str, Any]) -> None:
    """要検証 #12: snap のスナップ先が directions の始点と一致するか。"""
    print("\n--- 要検証 #12: snap と directions のスナップ先 ---")
    features = directions.get("features") or []
    coords = features[0].get("geometry", {}).get("coordinates", []) if features else []
    if not coords:
        print("  directions の座標が取れない")
        return
    d_start = LatLon(lat=coords[0][1], lon=coords[0][0])
    d_distance = haversine(PROBE, d_start)
    print(f"  directions の始点  : {d_start.lat:.7f}, {d_start.lon:.7f}")
    print(f"  起点からの距離     : {d_distance:.3f} m（haversine）")

    # snap は素の JSON を返す（locations の配列。半径内に道路がなければ null が入る）
    locations = snap.get("locations")
    if not isinstance(locations, list) or not locations:
        print("  snap が locations を返していない")
        print(f"  snap の生の中身: {json.dumps(snap, ensure_ascii=False)[:500]}")
        return
    entry = locations[0]
    if entry is None:
        print("  locations[0] が null（半径内に道路なし）。距離を言えないケース（4.6.1）")
        return
    snap_coords = entry.get("location", [])
    s_point = LatLon(lat=snap_coords[1], lon=snap_coords[0])
    print(f"  snap のスナップ先  : {s_point.lat:.7f}, {s_point.lon:.7f}")
    print(f"  snapped_distance   : {entry.get('snapped_distance')}")
    print(f"  name               : {entry.get('name')!r}")
    print(f"  起点からの距離     : {haversine(PROBE, s_point):.3f} m（haversine）")
    print(f"  2つのスナップ先の差: {haversine(d_start, s_point):.3f} m")
    print(f"  → 10m（design.md 8.5.1 の乖離の許容）と比べる: "
          f"{'一致とみなせる' if haversine(d_start, s_point) <= 10.0 else '一致しない'}")


def report_quota(headers: list[tuple[str, dict[str, str]]]) -> None:
    """要検証 #13: snap が directions と同じ枠を消費するか。"""
    print("\n--- 要検証 #13: 無料枠の消費 ---")
    for label, header in headers:
        print(f"  {label:20} {ratelimit_headers(header)}")


def load_saved(stamp: str, label: str) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """spike/out/ に保存した応答とヘッダを読む（**API を消費しない**）。

    分析の書き直しで投げ直さないために用意した。無料枠を使う検証では、
    保存した生データから何度でも読み直せることが節約になる。
    """
    response_path = OUT_DIR / f"{stamp}_{label}_response.json"
    meta_path = OUT_DIR / f"{stamp}_{label}_meta.json"
    payload = (
        json.loads(response_path.read_text(encoding="utf-8")) if response_path.exists() else None
    )
    headers: dict[str, str] = {}
    if meta_path.exists():
        headers = json.loads(meta_path.read_text(encoding="utf-8")).get("response_headers", {})
    return payload, headers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="投げずに本文だけ表示する")
    parser.add_argument(
        "--from-saved",
        metavar="STAMP",
        help="spike/out/<STAMP>_*.json を読んで分析だけやり直す（送信しない）",
    )
    parser.add_argument(
        "--only",
        choices=["all", "snap"],
        default="all",
        help="snap だけ投げ直す（directions を再送しないため）",
    )
    args = parser.parse_args()

    print(f"起点（暫定座標。HOME_LAT は読まない）: {PROBE.lat}, {PROBE.lon}")

    if args.from_saved:
        first, headers_a = load_saved(args.from_saved, "directions-seed1")
        snap, headers_b = load_saved(args.from_saved, "snap")
        _, headers_c = load_saved(args.from_saved, "directions-seed2")
        if first is not None:
            report_steps(first)
            if snap is not None:
                report_snap(first, snap)
        report_quota(
            [
                ("1. directions seed=1", headers_a),
                ("2. snap", headers_b),
                ("3. directions seed=2", headers_c),
            ]
        )
        print("\n保存済みの応答から分析した（送信ゼロ）。")
        return 0

    if args.dry_run:
        print(f"\n1. directions seed=1: {json.dumps(directions_body(seed=1), ensure_ascii=False)}")
        print(f"2. snap             : {json.dumps(snap_body(), ensure_ascii=False)}")
        print(f"3. directions seed=2: {json.dumps(directions_body(seed=2), ensure_ascii=False)}")
        print("\n--dry-run のため送信しない。")
        return 0

    load_dotenv()
    api_key = os.getenv("ORS_API_KEY")
    if not api_key:
        print("ORS_API_KEY が設定されていない。.env に ORS_API_KEY=<キー> を書いて再実行する。")
        return 2

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    collected: list[tuple[str, dict[str, str]]] = []

    if args.only == "snap":
        # directions を再送しない。#13 の判定は snap の残数ヘッダを
        # 直前の directions の残数と比べて行う
        snap, headers_b = post(
            label="snap", url=SNAP_URL, body=snap_body(), api_key=api_key, stamp=stamp,
            accept="application/json",
        )
        report_quota([("snap のみ", headers_b)])
        if snap is not None:
            print(f"\nsnap の応答: {json.dumps(snap, ensure_ascii=False)[:800]}")
        print("\n直前の directions の残数と比べて #13 を判定する。")
        return 0

    first, headers_a = post(
        label="directions-seed1", url=DIRECTIONS_URL, body=directions_body(seed=1),
        api_key=api_key, stamp=stamp, accept="application/geo+json",
    )
    collected.append(("1. directions seed=1", headers_a))
    if first is None:
        return 1

    snap, headers_b = post(
        label="snap", url=SNAP_URL, body=snap_body(), api_key=api_key, stamp=stamp,
        accept="application/json",
    )
    collected.append(("2. snap", headers_b))

    _, headers_c = post(
        label="directions-seed2", url=DIRECTIONS_URL, body=directions_body(seed=2),
        api_key=api_key, stamp=stamp, accept="application/geo+json",
    )
    collected.append(("3. directions seed=2", headers_c))

    report_steps(first)
    if snap is not None:
        report_snap(first, snap)
    report_quota(collected)

    print("\n次: spike/out/ の directions-seed1_response.json を")
    print("    tests/fixtures/ors_round_trip_5km_points3.json に置く。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
