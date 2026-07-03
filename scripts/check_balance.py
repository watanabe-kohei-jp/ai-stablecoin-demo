r"""
テストUSDC / ETH 残高確認スクリプト（Phase 1）

- .env に保存したアドレスについて、Base Sepolia 上の残高を読む
  - テストUSDC（ERC-20）の残高 … faucet で入手したぶんを確認する主目的
  - ネイティブ ETH の残高 … x402 はガスレス(EIP-3009)なので原則 0 でOK
- 「読むだけ」の操作なので秘密鍵は一切使わない（署名しない＝安全）
- USDC は decimals=6（最小単位 1,000,000 = 1 USDC）

実行: .venv\Scripts\python.exe scripts\check_balance.py
"""
import sys
from pathlib import Path
import os

from dotenv import load_dotenv
from web3 import Web3
from rich.console import Console
from rich.table import Table

# --- .env を読み込む（プロジェクト直下） ---
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

RPC_URL = os.environ["RPC_URL"]
USDC_ADDRESS = os.environ["USDC_ADDRESS"]
NETWORK = os.environ.get("NETWORK", "base-sepolia")

# 残高を見たいアドレス（公開情報。秘密鍵ではない）
WATCH = {
    "買い手 (agent)": os.environ["AGENT_ADDRESS"],
    "売り手 (vendor)": os.environ["VENDOR_ADDRESS"],
}

# ERC-20 のうち、残高確認に必要な関数だけの最小 ABI
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

# Windows の既定コンソール(cp932)でも日本語/記号が化けないよう UTF-8 に統一
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

console = Console()


def main() -> None:
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        console.print(f"[red]RPC に接続できません: {RPC_URL}[/red]")
        return

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI
    )
    # トークンの decimals / symbol をオンチェーンから取得（USDC は 6 桁のはず）
    decimals = usdc.functions.decimals().call()
    try:
        symbol = usdc.functions.symbol().call()
    except Exception:
        symbol = "USDC"

    table = Table(title=f"残高確認 - {NETWORK}（テストネット：価値ゼロ）")
    table.add_column("役割", style="cyan")
    table.add_column("アドレス", style="dim")
    table.add_column(f"{symbol}", justify="right", style="green")
    table.add_column("ETH", justify="right", style="yellow")

    for label, addr in WATCH.items():
        checksum = Web3.to_checksum_address(addr)
        raw_usdc = usdc.functions.balanceOf(checksum).call()
        usdc_human = raw_usdc / (10 ** decimals)
        wei_eth = w3.eth.get_balance(checksum)
        eth_human = w3.from_wei(wei_eth, "ether")
        table.add_row(label, checksum, f"{usdc_human:,.6f}", f"{eth_human:.6f}")

    console.print(table)
    console.print(
        f"\n[dim]USDC コントラクト: {USDC_ADDRESS}  decimals={decimals}[/dim]"
    )
    console.print(
        "[dim]※ ETH が 0 でも x402 はガスレス(EIP-3009)で動くので問題なし。"
        "買い手の USDC が 0 より大きくなれば faucet 成功。[/dim]"
    )


if __name__ == "__main__":
    main()
