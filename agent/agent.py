r"""
自律購買ループ（Phase 3d）— 頭脳(brain) と 手綱(wallet) を繋ぐ本体

人間が「目的＋予算」を1回渡すと、エージェントが自律的に
  頭脳に聞く → walletで買う → 記録 → 停止判定
を繰り返す。鍵・署名・予算/許可の強制は wallet 層に隔離され、頭脳は判断だけ。

実行（事前に売り手サーバーを起動しておくこと）:
  別ターミナル: .venv\Scripts\python.exe -m uvicorn server.shop:app --port 8000
  本体:         .venv\Scripts\python.exe agent\agent.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

# 同ディレクトリの兄弟モジュール（python agent\agent.py 実行時に解決される）
from wallet import Wallet, base_to_usdc
from brain import rule_based_brain, SEARCH_BASIC, SEARCH_PREMIUM, FETCH, SUMMARIZE
from brain_llm import get_brain

# 売り手の価格表（頭脳が予算判断に使う。実値は server/shop.py と一致）
KNOWN_PRICES = {
    SEARCH_BASIC: 0.02,
    SEARCH_PREMIUM: 0.05,
    FETCH: 0.05,
    SUMMARIZE: 0.03,
}

MAX_STEPS = 8  # 暴走防止の安全上限


async def run_agent(
    *, label: str, goal: str, budget_usdc: float, allowlist: list[str],
    price_list: dict[str, float], shop_url: str, network: str, private_key: str,
    console: Console, brain_fn=None, verbose: bool = True,
) -> Wallet:
    """1回の自律購買セッションを実行し、使った wallet を返す。

    brain_fn: 頭脳関数（goal, remaining_usdc, price_list, state)->Action。
              省略時は rule_based_brain。LLM版は brain_llm.get_brain() で取得。
    verbose:  False にすると逐次ログを抑制（比較ハーネスでまとめて出すとき用）。
    """
    if brain_fn is None:
        brain_fn = rule_based_brain
    brain_label = getattr(brain_fn, "provider", "rule")
    brain_model = getattr(brain_fn, "model", None)

    if verbose:
        console.rule(f"[bold]{label}[/bold]")
    wallet = Wallet(
        private_key=private_key, network=network, budget_usdc=budget_usdc,
        allowlist=allowlist, shop_url=shop_url,
    )
    if verbose:
        head = f"頭脳　: {brain_label}" + (f" ({brain_model})" if brain_model else "")
        console.print(head)
        console.print(f"目的　: {goal}")
        console.print(f"予算　: ${budget_usdc:.2f}")
        console.print(f"許可　: {sorted(allowlist)}\n")

    state: dict = {"done": set(), "skipped": set()}
    for step in range(1, MAX_STEPS + 1):
        action = brain_fn(goal, wallet.remaining_usdc, price_list, state)
        if action.type == "stop":
            if verbose:
                console.print(f"[bold yellow]■ 停止[/bold yellow]: {action.reason}")
            break

        if verbose:
            console.print(
                f"[bold]Step {step}[/bold]  頭脳の判断 → 買う [cyan]{action.path}[/cyan]\n"
                f"         理由: {action.reason}"
            )
        result = await wallet.buy(action.path, action.params)
        # 成功は done、失敗/ブロックは skipped に分けて記録（無限ループ防止＋達成判定を正直に）
        if action.capability:
            if result.ok:
                state["done"].add(action.capability)
            else:
                state["skipped"].add(action.capability)

        if verbose:
            if result.ok:
                tx = (result.tx or "")
                tx_disp = (tx[:18] + "…") if tx else "(なし)"
                console.print(f"         [green]✓ {result.reason}[/green]  tx={tx_disp}")
                if result.data:
                    # 受け取った商品のさわりだけ表示
                    preview = result.data.get("vendor") or result.data.get("summary") or result.data.get("content")
                    console.print(f"         受領データ例: {str(preview)[:60]}")
            else:
                console.print(f"         [red]✗ 拒否[/red]: {result.reason}")
            console.print()
    else:
        if verbose:
            console.print("[bold yellow]■ 安全上限(MAX_STEPS)に到達して停止[/bold yellow]")

    if verbose:
        # セッションの監査ログ
        table = Table(title=f"監査ログ — {label}")
        table.add_column("#", justify="right")
        table.add_column("path", style="cyan")
        table.add_column("結果")
        table.add_column("理由")
        for i, r in enumerate(wallet.audit, 1):
            mark = "[green]OK[/green]" if r.ok else "[red]拒否[/red]"
            table.add_row(str(i), r.path, mark, r.reason)
        console.print(table)
        console.print(
            f"累計支出 [bold]${base_to_usdc(wallet.spent_base):.2f}[/bold] / 予算 ${budget_usdc:.2f}"
            f"（残り ${wallet.remaining_usdc:.2f}）\n"
        )
    return wallet


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    console = Console()

    parser = argparse.ArgumentParser(description="x402 自律購買エージェント（Phase 3d / 5）")
    parser.add_argument(
        "--brain", default="rule", choices=["rule", "claude", "gpt", "gemini"],
        help="頭脳の選択（既定 rule）。LLMはキー未設定だと自動で rule に降格",
    )
    args = parser.parse_args()

    ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(ROOT / ".env")
    pk = os.environ["AGENT_PRIVATE_KEY"]
    chain_id = os.environ.get("CHAIN_ID", "84532")
    network = f"eip155:{chain_id}"
    shop_url = os.environ.get("SHOP_URL", "http://127.0.0.1:8000")
    goal = "AI×ステーブルコインの最新動向"

    brain_fn = get_brain(args.brain)
    common = dict(price_list=KNOWN_PRICES, shop_url=shop_url, network=network,
                  private_key=pk, console=console, brain_fn=brain_fn)

    # シナリオA：通常運転（予算潤沢）→ premium検索→fetch→summarize で目的達成
    wa = await run_agent(
        label="シナリオA：通常運転（目的達成）",
        goal=goal, budget_usdc=0.50,
        allowlist=[SEARCH_BASIC, SEARCH_PREMIUM, FETCH, SUMMARIZE],
        **common,
    )

    # シナリオB：予算しばり（$0.04）→ 安いbasic検索だけして、次手順は予算不足で自律停止
    wb = await run_agent(
        label="シナリオB：予算しばり（自律停止）",
        goal=goal, budget_usdc=0.04,
        allowlist=[SEARCH_BASIC, SEARCH_PREMIUM, FETCH, SUMMARIZE],
        **common,
    )

    # シナリオC：許可リストで /fetch を禁止 → 検索→(fetchはブロック)→要約 で継続
    wc = await run_agent(
        label="シナリオC：許可リスト制限（/fetch 禁止）",
        goal=goal, budget_usdc=0.50,
        allowlist=[SEARCH_BASIC, SEARCH_PREMIUM, SUMMARIZE],  # /fetch を含めない
        **common,
    )

    # 通し検証の判定
    console.rule("[bold]通し検証[/bold]")
    a_ok = any(r.ok for r in wa.audit) and wa.spent_base > 0          # ①払えた(200)
    b_ok = any("予算" in r.reason or "不足" in r.reason for r in wb.audit) or \
           wb.spent_base <= wb.budget_base                            # ②予算で止まる
    c_ok = any((not r.ok and "許可リスト外" in r.reason) for r in wc.audit)  # ③許可外ブロック
    console.print(f"① 払う前402/払った後200（実決済が成立）: {'[green]OK[/green]' if a_ok else '[red]NG[/red]'}")
    console.print(f"② 予算切れで自律停止　　　　　　　　　: {'[green]OK[/green]' if b_ok else '[red]NG[/red]'}")
    console.print(f"③ 許可リスト外をブロック　　　　　　　: {'[green]OK[/green]' if c_ok else '[red]NG[/red]'}")
    if a_ok and b_ok and c_ok:
        console.print("\n[bold green]✅ Phase 3d 通し検証成功[/bold green]：自律ループが手綱の中で正しく動作。")
    else:
        console.print("\n[bold red]❌ 一部が期待どおりではありません[/bold red]")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
