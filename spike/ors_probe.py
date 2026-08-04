"""OpenRouteService の仕様を1回のリクエストで確認する使い捨てスクリプト。

docs/requirements.md の設計を確定させる前に、以下の5点を実データで確かめる。

1. round_trip と avoid_features=[steps] を併用できるか
2. round_trip と elevation=true を併用できるか
3. ascent / descent が返るか（foot-walking プロファイル）
4. maneuver の種別（step の type）にどんな値が来るか
5. 名前のない道の name フィールドが実際に何になるか

使い方::

    uv run python spike/ors_probe.py                      # 全部入りで1回投げる
    uv run python spike/ors_probe.py --variant no-avoid   # 切り分けたいときだけ

1回の実行で投げるリクエストはちょうど1回。リトライはしない（無料枠を無駄に
消費しないため）。全部入りが失敗したときだけ、--variant で原因を切り分ける。

結果は spike/out/ に2ファイル保存される。

- ``*_response.json`` … レスポンス本体そのまま。これを tests/fixtures/ に移す
- ``*_meta.json``     … ステータス、レスポンスヘッダ、投げたリクエスト本体

このスクリプトは検証が終わったら消してよい。本体のパッケージ（runloop/）には
何も依存させない。
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

# ORS のルート検索エンドポイント（GeoJSON 形式で受け取る）
ENDPOINT: Final = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"

# .env に HOME_LAT / HOME_LON が無い場合に使う暫定座標（鹿児島市役所あたり）。
# 検証用の仮の値であり、実際の起点ではない。
FALLBACK_LAT: Final = 31.5966
FALLBACK_LON: Final = 130.5571

OUT_DIR: Final = Path(__file__).parent / "out"

# 投げる組み合わせ。all が失敗したときの切り分け用に3種を用意する。
VARIANTS: Final[dict[str, str]] = {
    "all": "round_trip + avoid_features[steps] + elevation",
    "no-avoid": "round_trip + elevation（avoid_features を外す）",
    "no-elevation": "round_trip + avoid_features[steps]（elevation を外す）",
    "plain": "round_trip のみ",
}


def build_body(
    *,
    variant: str,
    lat: float,
    lon: float,
    distance_m: int,
    seed: int,
    points: int,
) -> dict[str, Any]:
    """リクエスト本体を組み立てる。

    ORS の座標は [経度, 緯度] の順であることに注意（緯度・経度の順ではない）。
    round_trip は始点1つだけを渡す。
    """
    options: dict[str, Any] = {
        "round_trip": {"length": distance_m, "points": points, "seed": seed}
    }
    if variant in ("all", "no-elevation"):
        options["avoid_features"] = ["steps"]

    body: dict[str, Any] = {
        "coordinates": [[lon, lat]],
        "options": options,
        "instructions": True,
        # 曲がり方の種別（type）を確実に得るため、テキストではなく構造化された
        # 指示を要求する
        "instructions_format": "text",
        "units": "m",
    }
    if variant in ("all", "no-avoid"):
        body["elevation"] = True
    return body


def summarize(payload: dict[str, Any]) -> None:
    """レスポンスから、確認したかった5点に対応する情報を抜き出して表示する。

    レスポンスの形が想定と違う可能性そのものを検証しているので、
    キーの存在を仮定せずに取り出す。
    """
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        print("  features が空。ルートが返っていない")
        return

    props = features[0].get("properties", {})
    geometry = features[0].get("geometry", {})
    coords = geometry.get("coordinates", [])

    # --- 2, 3: 標高 ---
    summary = props.get("summary", {})
    print(f"  summary            : {summary}")
    print(f"  ascent             : {props.get('ascent', '(キーなし)')}")
    print(f"  descent            : {props.get('descent', '(キーなし)')}")
    if coords:
        dim = len(coords[0]) if isinstance(coords[0], list) else 0
        print(f"  座標の次元         : {dim}（3 なら標高が入っている）")
        print(f"  座標数             : {len(coords)}")
        print(f"  最初の座標         : {coords[0]}")

    # --- properties の一覧。想定外のキーを見落とさないため ---
    print(f"  properties のキー  : {sorted(props.keys())}")

    # --- 4, 5: maneuver の種別と道路名 ---
    steps: list[dict[str, Any]] = []
    for segment in props.get("segments", []):
        steps.extend(segment.get("steps", []))

    if not steps:
        print("  steps が空。instructions が返っていない")
        return

    print(f"  step 数            : {len(steps)}")
    print(f"  step のキー        : {sorted(steps[0].keys())}")

    print("\n  --- 4. maneuver の種別（type: 件数 / 指示文の例）---")
    type_counts = Counter(step.get("type") for step in steps)
    examples: dict[Any, str] = {}
    for step in steps:
        examples.setdefault(step.get("type"), str(step.get("instruction", "")))
    for step_type, count in sorted(type_counts.items(), key=lambda x: str(x[0])):
        print(f"    type={step_type!r:>6} : {count:3d} 件  例: {examples[step_type]!r}")

    print("\n  --- 5. name フィールドの実際の値 ---")
    name_counts = Counter(repr(step.get("name")) for step in steps)
    for name, count in name_counts.most_common(15):
        print(f"    {count:3d} 件  {name}")
    if len(name_counts) > 15:
        print(f"    ...ほか {len(name_counts) - 15} 種")
    unnamed = [n for n in name_counts if n in ("'-'", "''", "None")]
    print(f"  名前なしと思われる値: {unnamed or '見つからない'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        choices=sorted(VARIANTS),
        default="all",
        help="投げる組み合わせ。既定は all（全部入り）",
    )
    parser.add_argument("--distance", type=int, default=5000, help="目標距離（m）")
    parser.add_argument("--seed", type=int, default=1, help="round_trip のシード")
    parser.add_argument("--points", type=int, default=5, help="round_trip の経由点数")
    parser.add_argument("--profile", default="foot-walking", help="ORS のプロファイル")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("ORS_API_KEY")
    if not api_key:
        print("ORS_API_KEY が設定されていない。")
        print("プロジェクト直下に .env を作り、次の行を書いてから再実行する:")
        print("    ORS_API_KEY=<取得したキー>")
        print("（.env は .gitignore 済みなのでコミットされない）")
        return 2

    lat = float(os.getenv("HOME_LAT") or FALLBACK_LAT)
    lon = float(os.getenv("HOME_LON") or FALLBACK_LON)
    if not os.getenv("HOME_LAT"):
        print(f"HOME_LAT/HOME_LON が未設定のため暫定座標を使う: {lat}, {lon}")

    body = build_body(
        variant=args.variant,
        lat=lat,
        lon=lon,
        distance_m=args.distance,
        seed=args.seed,
        points=args.points,
    )
    url = ENDPOINT.format(profile=args.profile)

    print(f"variant : {args.variant}（{VARIANTS[args.variant]}）")
    print(f"POST    : {url}")
    print(f"body    : {json.dumps(body, ensure_ascii=False)}")
    print("リクエストは1回だけ投げる（リトライしない）\n")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = OUT_DIR / f"{stamp}_{args.variant}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "requested_at": stamp,
        "variant": args.variant,
        "url": url,
        "request_body": body,
    }

    try:
        response = requests.post(
            url,
            json=body,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/geo+json",
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        # 通信そのものが失敗した場合も記録する（あとで原因を追えるように）
        meta["transport_error"] = f"{type(exc).__name__}: {exc}"
        base.with_name(base.name + "_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"通信に失敗した: {type(exc).__name__}: {exc}")
        return 1

    # ヘッダ名が環境で揺れる可能性があるため、レート制限系を選別せず全部残す。
    # API キーはヘッダに含まれない（リクエスト側なので）が、念のため body だけを保存対象にする。
    meta["status_code"] = response.status_code
    meta["response_headers"] = dict(response.headers)

    try:
        payload = response.json()
    except ValueError:
        payload = None
        meta["raw_text"] = response.text[:5000]

    response_path = base.with_name(base.name + "_response.json")
    meta_path = base.with_name(base.name + "_meta.json")
    if payload is not None:
        response_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"status  : {response.status_code}")
    print(f"保存     : {response_path if payload is not None else '(JSONでない)'}")
    print(f"保存     : {meta_path}")

    if response.status_code != 200:
        # レート制限（429）や権限エラー（403）もここに来る。本文を必ず表示する。
        print("\n--- エラー応答の本文 ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2) if payload else response.text[:2000])
        print("\n判定: この組み合わせは通らなかった。")
        print("      429 ならレート制限。時間を置いて再実行する。")
        print("      400 番台ならオプションの組み合わせが原因の可能性があるので、")
        print("      --variant no-avoid / no-elevation で切り分ける。")
        return 1

    print("\n--- 応答の要約 ---")
    if isinstance(payload, dict):
        summarize(payload)

    print("\n判定: この組み合わせは通った。")
    print(f"次: {response_path.name} を tests/fixtures/ に移してモックの元データにする。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
