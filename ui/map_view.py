"""folium での地図描画（design.md 1.2 / 7.2、requirements.md AC-01-1 / AC-04 / AC-05-1〜3）。

地図の描画をこの1ファイルに閉じる。**接近区間の道は描かない。** ORS のレスポンスに
接近区間のジオメトリは存在しない（design.md 7.2）ため、起点とループの始点を
直線でつないで埋め合わせることもしない。それは実在しない道を描くことになる。

地図の見た目そのものは手動確認とする（design.md 10.4）。ここで固定するのは
座標が正しく渡っていることと、ポリラインが1本であることだけである。
"""

import html
from typing import Final

import folium

from runloop import messages
from runloop.models import Candidate, Checkpoint, LatLon

# 起点未指定のときの初期表示。ユーザーの実際の位置とは無関係な広域表示にする
# （AC-05-1「地図をクリックして起点を指定する」の前段階。実在の地点を焼き付けない）
_DEFAULT_CENTER: Final = LatLon(lat=36.2048, lon=138.2529)  # 日本の大まかな中心
_DEFAULT_ZOOM: Final = 5
_ORIGIN_ZOOM: Final = 16

# チェックポイントの丸印の半径（px）。指で操作できる大きさは手動確認の対象
# （design.md 10.4）だが、既定のマーカーより小さいので大きめに取る
_CHECKPOINT_RADIUS: Final = 12

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
        _fit_to_course(folium_map, candidate=candidate, origin=origin)

    for checkpoint in checkpoints:
        folium.CircleMarker(
            location=(checkpoint.position.lat, checkpoint.position.lon),
            # 吹き出しの文言は `messages.py` から取る（画面に文字列リテラルを
            # 書かない。AC-04-2 / AC-04-4 の標準形の定義元は1か所）。
            # **描画層でエスケープする**（design.md 8.7）
            tooltip=_safe_tooltip(messages.checkpoint_line(checkpoint)),
            radius=_CHECKPOINT_RADIUS,
            fill=True,
        ).add_to(folium_map)

    return folium_map


def _safe_tooltip(text: str) -> str:
    """吹き出しに渡す文字列を無害化する（design.md 8.7）。

    **folium は吹き出しの値をエスケープしない。** `folium.map.Tooltip` は
    `self.text = str(text)` と受けたうえで、`branca.element.Template`
    （`jinja2.Template` の派生で **autoescape が無効**）の
    ``bindTooltip(`<div>{{ this.text }}</div>`, ...)`` に差し込む。
    つまり値は**バッククォートで囲まれた JS のテンプレートリテラルの内側**に、
    かつ `<div>` の内側に、生のまま置かれる。

    **HTML エスケープだけでは足りない。** テンプレートリテラルの内側では
    `${...}` が式として評価されるので、`<` を潰してもバッククォートを
    閉じる必要すらなく JavaScript を実行できる。

    危険なのは道の名前である。**OpenStreetMap 由来で誰でも編集できる**
    （`ors/mapper.py` は `"-"` を `None` にするだけで、他は素通しする）。
    被害者の生活圏の道路名を書き換えるだけで、アプリと同一オリジンで
    JavaScript を実行でき、`localStorage` の**丸めていない自宅座標**を
    読み出せる（design.md 8.2 / 8.7）。

    **HTML 実体参照に置き換える**（バックスラッシュでエスケープしない）。
    実体参照なら JS の文字列としても危険な文字が1つも残らず、`<div>` の
    内側では元の文字として表示される。順序が要る——`html.escape` を先に
    かけないと、あとから入れる `&#96;` の `&` が二重にエスケープされる。

    **ここに置くのは、必要なエスケープが描画技術ごとに違うため。**
    `ors/mapper.py` で文字集合を制限すると、記号を含む正当な道路名を壊す。
    `messages.py` は表示技術に依存しない文言を返す責務なので、そちらでもない。
    """
    escaped = html.escape(text, quote=True)
    return (
        escaped.replace("\\", "&#92;").replace("`", "&#96;").replace("${", "$&#123;")
    )


def _fit_to_course(
    folium_map: folium.Map, *, candidate: Candidate, origin: LatLon | None
) -> None:
    """コース全体が画面に入るように寄せる。

    起点を中心に固定倍率で描くと、目標距離が大きいコースが画面から
    はみ出して全体を見られない。**起点も範囲に含める**——接近区間の道は
    描かないが（design.md 7.2）、起点そのものはマーカーとして出ており、
    見えなくなってはいけない。

    **コースが無いときは呼ばない**（呼び出し側の条件）。1点だけに寄せると
    範囲が潰れて最大倍率になり、周りを見て起点を選び直せなくなる。
    """
    points = list(candidate.geometry)
    if origin is not None:
        points.append(origin)
    lats = [point.lat for point in points]
    lons = [point.lon for point in points]
    folium_map.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])
