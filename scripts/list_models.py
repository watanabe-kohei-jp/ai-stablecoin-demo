r"""
利用可能モデル探索（Phase 5 ベンチ準備）

御三家それぞれの API に「このアカウントで使えるモデル一覧」を問い合わせる。
生成は行わない（models.list 相当）ので実費はほぼ無い。
モデル名は時とともに変わるため、ベンチのロスター選定前に“今の実在ID”を確認する用途。

実行: .venv\Scripts\python.exe scripts\list_models.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
console = Console()


def _hdr(name: str) -> None:
    console.rule(f"[bold]{name}[/bold]")


def list_anthropic() -> None:
    _hdr("Claude (Anthropic)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[yellow]ANTHROPIC_API_KEY 未設定[/yellow]")
        return
    try:
        import anthropic

        client = anthropic.Anthropic()
        for m in client.models.list(limit=100).data:
            disp = getattr(m, "display_name", "")
            console.print(f"  {m.id}    [dim]{disp}[/dim]")
    except Exception as e:
        console.print(f"[red]NG[/red] {type(e).__name__}: {str(e)[:160]}")


def list_openai() -> None:
    _hdr("GPT (OpenAI)")
    if not os.environ.get("OPENAI_API_KEY"):
        console.print("[yellow]OPENAI_API_KEY 未設定[/yellow]")
        return
    try:
        from openai import OpenAI

        client = OpenAI()
        ids = sorted(m.id for m in client.models.list().data)
        # チャット系だけ拾う（埋め込み/音声/画像などを除外）
        chat = [i for i in ids if i.startswith(("gpt", "o1", "o3", "o4", "chatgpt"))]
        for i in chat:
            console.print(f"  {i}")
        console.print(f"[dim]（全{len(ids)}件中 チャット系{len(chat)}件を表示）[/dim]")
    except Exception as e:
        console.print(f"[red]NG[/red] {type(e).__name__}: {str(e)[:160]}")


def list_gemini() -> None:
    _hdr("Gemini (Google)")
    if not os.environ.get("GEMINI_API_KEY"):
        console.print("[yellow]GEMINI_API_KEY 未設定[/yellow]")
        return
    try:
        from google import genai

        client = genai.Client()
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions:
                console.print(f"  {m.name}    [dim]{getattr(m, 'display_name', '')}[/dim]")
    except Exception as e:
        console.print(f"[red]NG[/red] {type(e).__name__}: {str(e)[:160]}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    list_anthropic()
    list_openai()
    list_gemini()


if __name__ == "__main__":
    main()
