"""座標計算。純関数のみ（design.md 1.2）。

接近距離の算出は AC-01-2 の判定に直結するため、単体テストで固定する。
"""

import math
from typing import Final

from runloop.models import LatLon

# 地球を球とみなしたときの半径。スパイクと同じ値（spike/ors_feasibility.py:99）。
# 50m 前後の判定に使うので球近似で十分で、楕円体との差は 0.5% 未満。
# **この値を変えると接近距離が変わり、AC-01-3 の 50m / 300m 判定がずれる。**
EARTH_RADIUS_M: Final = 6_371_000.0


def haversine(a: LatLon, b: LatLon) -> float:
    """2点間の距離（m）。球近似。

    接近距離（起点とスナップ先の距離）の算出に使う。
    丸めない。丸めるのは表示のときだけである（design.md 2.2）。
    """
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(b.lon - a.lon)
    h = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))
