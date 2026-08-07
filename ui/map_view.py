"""folium での地図描画（design.md 1.2 / 7.2、requirements.md AC-01-1 / AC-04 / AC-05-1〜3）。

地図の描画をこの1ファイルに閉じる。**接近区間の道は描かない。** ORS のレスポンスに
接近区間のジオメトリは存在しない（design.md 7.2）ため、起点とループの始点を
直線でつないで埋め合わせることもしない。それは実在しない道を描くことになる。

地図の見た目そのものは手動確認とする（design.md 10.4）。ここで固定するのは
座標が正しく渡っていることと、ポリラインが1本であることだけである。
"""

from typing import Final

import folium

from runloop.models import Candidate, Checkpoint, LatLon, TurnDirection

# 起点未指定のときの初期表示。ユーザーの実際の位置とは無関係な広域表示にする
# （AC-05-1「地図をクリックして起点を指定する」の前段階。実在の地点を焼き付けない）
_DEFAULT_CENTER: Final = LatLon(lat=36.2048, lon=138.2529)  # 日本の大まかな中心
_DEFAULT_ZOOM: Final = 5
_ORIGIN_ZOOM: Final = 16

# チェックポイントの丸印の半径（px）。指で操作できる大きさは手動確認の対象
# （design.md 10.4）だが、既定のマーカーより小さいので大きめに取る
_CHECKPOINT_RADIUS: Final = 12

# 方向転換の向きの日本語表記（design.md 7.1 のホワイトリストと1対1。AC-04-2）。
# ここに無い `TurnDirection` があれば KeyError で気づける（黙って無表記にしない）
_DIRECTION_LABELS: Final[dict[TurnDirection, str]] = {
    TurnDirection.TURN_LEFT: "左折",
    TurnDirection.TURN_RIGHT: "右折",
    TurnDirection.SHARP_LEFT: "鋭角左折",
    TurnDirection.SHARP_RIGHT: "鋭角右折",
    TurnDirection.SLIGHT_LEFT: "緩い左折",
    TurnDirection.SLIGHT_RIGHT: "緩い右折",
}


def build_map(
    origin: LatLon | None = None,
    candidate: Candidate | None = None,
    checkpoints: tuple[Checkpoint, ...] = (),
) -> folium.Map:
    """起点・コース・チェックポイントを乗せた地図を組み立てる。

    起点が未指定なら広域表示のみ（クリックして起点を指定する前の状態）。
    起点があればマーカーを置く（AC-05-2）。`candidate` があればループの
    ポリラインを重ねる（AC-01-1 の描画部分）。`checkpoints` があれば
    その分だけ丸印を置く（AC-04-1〜2 / AC-04-4）。
    """
    center = origin if origin is not None else _DEFAULT_CENTER
    zoom = _ORIGIN_ZOOM if origin is not None else _DEFAULT_ZOOM
    folium_map = folium.Map(location=(center.lat, center.lon), zoom_start=zoom)

    if origin is not None:
        folium.Marker(
            location=(origin.lat, origin.lon),
            tooltip="起点",
            icon=folium.Icon(color="red"),
        ).add_to(folium_map)

    if candidate is not None:
        # folium.PolyLine.__init__ には型注釈が無い（Marker / CircleMarker /
        # Map / Icon にはある。folium 自身の型定義の抜け）
        folium.PolyLine(  # type: ignore[no-untyped-call]
            locations=[(point.lat, point.lon) for point in candidate.geometry],
        ).add_to(folium_map)

    for checkpoint in checkpoints:
        folium.CircleMarker(
            location=(checkpoint.position.lat, checkpoint.position.lon),
            radius=_CHECKPOINT_RADIUS,
            tooltip=_checkpoint_tooltip(checkpoint),
            fill=True,
        ).add_to(folium_map)

    return folium_map


def _checkpoint_tooltip(checkpoint: Checkpoint) -> str:
    """チェックポイントの吹き出し文言（AC-04-2 / AC-04-4）。

    地点名は取得できた場合のみ併記し、無ければ何も足さない
    （`None` を文字列として出さない）。
    """
    label = _DIRECTION_LABELS[checkpoint.direction]
    text = f"{checkpoint.order}. 起点から{checkpoint.distance_from_origin_m:.0f}m {label}"
    if checkpoint.name is not None:
        text += f"（{checkpoint.name}）"
    return text
