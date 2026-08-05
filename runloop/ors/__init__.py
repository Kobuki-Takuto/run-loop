"""OpenRouteService（ORS）向けのプロバイダ実装。

ORS のレスポンス形式と HTTP の事情をこのサブパッケージの中だけに閉じる。
上位層は ``runloop.ports`` の Protocol とドメイン例外しか見ない（docs/design.md 1.3）。
"""
