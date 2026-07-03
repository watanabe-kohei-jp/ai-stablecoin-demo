r"""
LLM キー疎通チェック（Phase 5 補助）

御三家（Claude / GPT / Gemini）の API キーが「認証を通り、ツール呼び出し（構造化出力）
まで成功するか」を、購買ロジックやサーバーを介さずに 1 社ずつ直接テストする。
brain_llm のフォールバックを通さず、生の例外（無効キー・残高不足・モデル名誤り等）を表面化させる。

実行: .venv\Scripts\python.exe scripts\check_llm_keys.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "agent"))

import brain_llm as bl  # noqa: E402

PRICES = {"/search/basic": 0.02, "/search/premium": 0.05, "/fetch": 0.05, "/summarize": 0.03}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    console = Console()

    user = bl._build_user_prompt(
        "AI×ステーブルコインの最新動向", 1.0, PRICES, {"done": set(), "skipped": set()}
    )

    table = Table(title="LLM キー疎通チェック（認証＋ツール呼び出し）")
    table.add_column("社", style="cyan")
    table.add_column("モデル", style="dim")
    table.add_column("結果")
    table.add_column("詳細")

    all_ok = True
    for p in bl.PROVIDERS:
        model = os.environ.get(bl.ENV_MODEL[p]) or bl.DEFAULT_MODELS[p]
        if not os.environ.get(bl.ENV_KEY[p]):
            table.add_row(p, model, "[yellow]キー未設定[/yellow]", f"{bl.ENV_KEY[p]} が空")
            continue
        try:
            d = bl._PROVIDER_FN[p](model, bl.SYSTEM_PROMPT, user)
            act = d.get("action")
            path = d.get("path")
            table.add_row(p, model, "[green]OK[/green]", f"判断: {act} {path or ''}")
        except Exception as e:
            all_ok = False
            table.add_row(p, model, "[red]NG[/red]", f"{type(e).__name__}: {str(e)[:120]}")

    console.print(table)
    if all_ok:
        console.print("[bold green]✅ 設定済みの社はすべて疎通OK[/bold green] → compare_brains.py で本比較できます。")
    else:
        console.print(
            "[bold yellow]一部 NG[/bold yellow]：上の詳細を確認（多くは"
            "「残高不足 / モデル名 / キー誤り」）。NG の社は本比較で rule に降格します。"
        )


if __name__ == "__main__":
    main()
