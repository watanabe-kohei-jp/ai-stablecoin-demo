r"""
M4 通しデモ：自動スイープ（待機=利回り / 支払い=just-in-time 償還）

ストーリー:
  1) 余剰USDCを Vault へ「スイープ」して待機資金を利回りに回す
     （運転資金 min_operating だけ手元に残す）
  2) その後、手元の流動USDCより高い買い物をすると、wallet が
     **支払い直前に必要分だけ Vault から自動償還(JIT)** して x402 決済を完遂する
  → 「AIに財布を渡す」二層防御を、支払いだけでなく「財務管理」へ拡張した実証。
     判断(brain)は何も変わらず、流動性の調達は wallet(信頼境界)が物理的に面倒を見る。

前提: 売り手サーバー起動 + VAULT_ADDRESS が .env にある + 買い手に少量のガス(ETH)。
  別ターミナル:  .venv\Scripts\python.exe -m uvicorn server.shop:app --port 8000
  実行:          .venv\Scripts\python.exe scripts\sweep_demo.py
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
import wallet as W  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
console = Console()

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MIN_OPERATING = 0.01           # 手元に常に残す運転資金
BUYS = ["/search/basic", "/summarize"]   # $0.02 + $0.03（合計 $0.05）
EXPECTED_SPEND = 0.05


async def _settle(read_fn, want_base: int, tries: int = 15, wait: float = 2.0) -> int:
    last = read_fn()
    for _ in range(tries):
        if last == want_base:
            return last
        await asyncio.sleep(wait)
        last = read_fn()
    return last


def _bal_row(table, label, liquid_base, pos_base):
    table.add_row(label, f"{W.base_to_usdc(liquid_base):.4f}", f"{W.base_to_usdc(pos_base):.4f}")


async def run() -> None:
    w = W.Wallet(
        private_key=os.environ["AGENT_PRIVATE_KEY"],
        network=f"eip155:{os.environ.get('CHAIN_ID', '84532')}",
        budget_usdc=0.10,
        allowlist=BUYS,
        shop_url=os.environ.get("SHOP_URL", "http://127.0.0.1:8000"),
        rpc_url=os.environ["RPC_URL"],
        usdc_address=os.environ["USDC_ADDRESS"],
        vault_address=os.environ["VAULT_ADDRESS"],
        min_operating_usdc=MIN_OPERATING,
    )

    table = Table(title="自動スイープ通しデモ（USDC）")
    table.add_column("段階", style="cyan")
    table.add_column("流動USDC", justify="right", style="green")
    table.add_column("Vault内価値", justify="right", style="yellow")

    liquid0 = w.liquid_usdc_base()
    _bal_row(table, "開始", liquid0, w.vault_position_base())

    # 1) スイープ：運転資金だけ残して余剰を Vault へ
    console.print("[bold]① 余剰USDCを Vault へスイープ中…[/bold]")
    swept = await w.sweep_idle()
    liquid_after_sweep = await _settle(w.liquid_usdc_base, W.usdc_to_base(MIN_OPERATING))
    _bal_row(table, "sweep後", liquid_after_sweep, w.vault_position_base())

    # 2) 流動 < 価格 の買い物 → wallet が自動で JIT 償還して支払い
    console.print("[bold]② 手元より高い買い物 → walletが自動JIT償還して決済[/bold]")
    results = []
    for path in BUYS:
        console.print(f"   buy {path} …")
        r = await w.buy(path)
        results.append(r)
        mark = "[green]OK[/green]" if r.ok else "[red]NG[/red]"
        console.print(f"     {mark} {r.reason}")

    _bal_row(table, "購入後", w.liquid_usdc_base(), w.vault_position_base())
    console.print()
    console.print(table)

    # --- 監査ログ ---
    console.print("\n[bold]財務 監査ログ[/bold]")
    for line in w.treasury_audit:
        console.print(f"  • {line}")
    console.print("\n[bold]購買 監査ログ[/bold]")
    for r in w.audit:
        mark = "[green]OK[/green]" if r.ok else "[red]NG[/red]"
        console.print(f"  • {mark} {r.path}: {r.reason}")

    # --- 後片付け：Vault残高を全部戻す（再実行できるように・best-effort） ---
    try:
        pos = w.vault_position_base()
        if pos > 0:
            console.print("\n[dim]後片付け：Vault残高を全額償還して元に戻します…[/dim]")
            await w.withdraw_from_vault(W.base_to_usdc(pos))
    except Exception as e:
        console.print(f"[dim]（後片付けはスキップ：{type(e).__name__}: {e}）[/dim]")

    # --- 期待値 assert（残高はRPC遅延の影響を受けるので、判定は in-memory の確実な値で） ---
    ok = True
    if not all(r.ok for r in results):
        console.print("[red]NG: 一部の購入が失敗[/red]"); ok = False
    if w.spent_base != W.usdc_to_base(EXPECTED_SPEND):
        console.print(f"[red]NG: 支出が想定外 ${W.base_to_usdc(w.spent_base):.4f} != ${EXPECTED_SPEND}[/red]"); ok = False
    if not any("JIT" in line for line in w.treasury_audit):
        console.print("[red]NG: JIT償還が発生していない（スイープ額/価格を確認）[/red]"); ok = False

    if ok:
        console.print(
            "\n[bold green]✅ M4 検証成功[/bold green]："
            "余剰をVaultへスイープ→支払い直前にwalletが自動JIT償還→x402決済が完遂。"
            "判断(brain)は無関与で、流動性調達は wallet(信頼境界) が物理的に担保した。"
        )
    else:
        console.print("\n[bold red]❌ 期待と不一致[/bold red]：上のログを確認。")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(run())
