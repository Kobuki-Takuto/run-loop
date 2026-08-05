"""層の規律を守るテスト。

docs/design.md 1.2 の「``ui/`` 以外は Streamlit を import しない」を検査する。
ruff の flake8-tidy-imports でも禁止しているが、設定を緩めたときに
静かに崩れるのを防ぐため、テストとしても固定する。
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ``import streamlit`` と ``from streamlit.x import y`` に一致する。
# ``streamlit_js_eval`` には一致しない（runloop/persistence.py が使う。design.md 8.1）。
STREAMLIT_IMPORT = re.compile(r"^\s*(?:import|from)\s+streamlit(?:\.|\s|$)")


def _python_files(package: str) -> list[Path]:
    """パッケージ配下の .py ファイルを列挙する。"""
    return sorted((PROJECT_ROOT / package).rglob("*.py"))


def test_runloop_does_not_import_streamlit() -> None:
    """本体パッケージが Streamlit に依存していないこと（design.md 1.2）。"""
    offenders: list[str] = []
    for path in _python_files("runloop"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if STREAMLIT_IMPORT.match(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], "runloop/ で Streamlit を import している: " + ", ".join(offenders)


def test_runloop_package_is_importable() -> None:
    """本体パッケージが import できること（骨格の疎通確認）。"""
    import runloop

    assert runloop.__doc__ is not None
