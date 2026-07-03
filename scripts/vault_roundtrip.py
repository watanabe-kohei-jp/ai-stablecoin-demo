r"""
M3 検証：wallet の財務メソッド（deposit / poke / withdraw）が実チェーンで通るか確認。

二層防御の信頼境界（鍵・署名・tx送信は wallet のみ）を保ったまま、
待機USDC → Vault預入 → 償還 が会計どおり動くことを assert する。

前提: VAULT_ADDRESS が .env にある（deploy 済み）／買い手にガス(ETH)が少しある。
実行: .venv\Scripts\python.exe scripts\vault_roundtrip.py
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

DEPOSIT_USDC = 5.0


async def _settle(read_fn, want_base: int, tries: int = 15, wait: float = 2.0) -> int:
    """公開RPCの read-after-write 遅延対策：read_fn() が want_base になるまで待つ。
    タイムアウトしたら最後の読み値を返す（asで判定側に委ねる）。"""
    last = read_fn()
    for _ in range(tries):
        if last == want_base:
            return last
        await asyncio.sleep(wait)
        last = read_fn()
    return last


async def run() -> None:
    w = W.Wallet(
        private_key=os.environ["AGENT_PRIVATE_KEY"],
        network=f"eip155:{os.environ.get('CHAIN_ID', '84532')}",
        budget_usdc=100.0,          # この検証は x402 予算とは無関係
        allowlist=[],
        shop_url="http://127.0.0.1:8000",
        rpc_url=os.environ["RPC_URL"],
        usdc_address=os.environ["USDC_ADDRESS"],
        vault_address=os.environ["VAULT_ADDRESS"],
        min_operating_usdc=0.0,
    )

    dep = W.usdc_to_base(DEPOSIT_USDC)
    rows = []

    liquid0 = w.liquid_usdc_base()
    pos0 = w.vault_position_base()
    rows.append(("開始", liquid0, pos0))

    console.print(f"[bold]Vault[/bold] {os.environ['VAULT_ADDRESS']}  預入予定 ${DEPOSIT_USDC}\n")

    # 1) 預け入れ（読み取りは反映までポーリング）
    console.print("[dim]deposit 実行中…（approve→deposit の2tx）[/dim]")
    await w.deposit_to_vault(DEPOSIT_USDC)
    liquid1 = await _settle(w.liquid_usdc_base, liquid0 - dep)
    pos1 = await _settle(w.vault_position_base, dep)
    rows.append(("deposit後", liquid1, pos1))

    # 2) 利回り算入（短時間なので増分はほぼ0だが、txが通ることを確認）
    console.print("[dim]poke 実行中…[/dim]")
    await w.poke_vault()
    rows.append(("poke後", w.liquid_usdc_base(), w.vault_position_base()))

    # 3) 償還（必要額指定 withdraw）。流動が開始に戻るまでポーリング
    console.print("[dim]withdraw 実行中…[/dim]")
    await w.withdraw_from_vault(DEPOSIT_USDC)
    liquid2 = await _settle(w.liquid_usdc_base, liquid0)
    pos2 = w.vault_position_base()
    rows.append(("withdraw後", liquid2, pos2))

    # --- 結果表示 ---
    table = Table(title="財務ラウンドトリップ（USDC, 単位=枚）")
    table.add_column("段階", style="cyan")
    table.add_column("流動USDC", justify="right", style="green")
    table.add_column("Vault内価値", justify="right", style="yellow")
    for label, liq, pos in rows:
        table.add_row(label, f"{W.base_to_usdc(liq):.6f}", f"{W.base_to_usdc(pos):.6f}")
    console.print(table)

    console.print("\n[bold]監査ログ（treasury）[/bold]")
    for line in w.treasury_audit:
        console.print(f"  • {line}")

    # --- 期待値 assert ---
    ok = True
    # deposit で流動はちょうど dep 減り、Vault価値は dep（初回は1:1）
    if liquid1 != liquid0 - dep:
        console.print(f"[red]NG: deposit後の流動が想定外 {liquid1} != {liquid0 - dep}[/red]"); ok = False
    if pos1 != dep:
        console.print(f"[red]NG: deposit後のVault価値が想定外 {pos1} != {dep}[/red]"); ok = False
    # withdraw で要求額ちょうど戻り、流動は開始に復帰（利回り分はVaultに残りうる）
    if liquid2 != liquid0:
        console.print(f"[red]NG: withdraw後の流動が開始に戻らない {liquid2} != {liquid0}[/red]"); ok = False
    if pos2 < 0:
        console.print(f"[red]NG: Vault価値が負[/red]"); ok = False

    if ok:
        console.print("\n[bold green]✅ M3 検証成功[/bold green]：deposit/poke/withdraw が会計どおり実チェーンで通った。")
    else:
        console.print("\n[bold red]❌ 期待と不一致[/bold red]：上のログを確認。")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(run())
