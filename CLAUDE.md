# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト

RunLoop: 起点と目標距離を指定すると、その距離に近い周回ランニングコースを1本提案する
Streamlit Web アプリ。仕様の正典は [docs/requirements.md](docs/requirements.md)（版1.0・確定）。
実装判断で迷ったら、そこの受け入基準（AC-xx）を根拠にする。

現状はほぼ空のスケルトン（`main.py` は雛形のまま、テスト・CI・`.env.example` は未作成）。
開発の進め方は [docs/WORKFLOW.md](docs/WORKFLOW.md) に定義されている。

## コマンド

```bash
uv sync --all-extras --dev             # 依存の同期
uv run pytest                          # テスト全体
uv run pytest tests/test_x.py::test_y  # 単体指定
uv run pytest --cov
uv run ruff check .
uv run ruff format .
uv run mypy .
```

Python 3.13 固定。パッケージ追加は `uv add` / `uv add --dev`（`pip` は使わない）。

## 設計上の要点

- 外部 API は OpenRouteService（round_trip、`avoid_features` で階段回避）を想定。
  差し替え可能な形に抽象化する（非機能要件「保守性」）。
- 候補選択は順序が仕様。**まず距離誤差 ±300m で絞り、その中で獲得標高が最小**を選ぶ。
  ±300m の候補が0件なら誤差最小を出し、条件未達を画面に明示する（AC-01-4）。
- ORS の返す距離は近似値。±50m の精度はアプリでは担保しない。アプリの責任は
  「±300m のコース生成」と「実距離・差分の正確な表示」まで。
- 1回の実行で外部 API 呼び出しは10回以内、結果表示まで10秒以内。
- 異常時（API 失敗／キー未設定／候補0件）もアプリを止めず、再実行可能な状態を保つ（US-06）。
- 起点は `st.session_state` ではなくブラウザに永続化して次回復元する（US-07）。
- API キーはコードにもリポジトリにも置かない。ローカルは `.env`、公開時は Streamlit Secrets。

## 進め方の規律（WORKFLOW.md より）

- 実装は1タスク=1ブランチ=1PR、半日以内の粒度。タスク開始時に `/clear`。
- **テストを先に書き、失敗を確認してから実装する。**
- 外部 API を実際に叩く自動テストは書かない。`responses` でモックし、
  実レスポンスは `tests/fixtures/` に JSON で固定する。
- Streamlit 画面は手動確認とする（自動化しない）。
- 設計から逸脱したら `docs/design.md` を更新する。技術選定は `docs/adr/` に1決定1ファイル。
