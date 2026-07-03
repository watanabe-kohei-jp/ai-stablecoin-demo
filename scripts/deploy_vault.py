r"""
MockYieldVault.sol のコンパイル / デプロイ（自動スイープ用・テストネット専用）

⚠️ Base Sepolia 限定。本物の資産には触れない。Vault は「利回り模擬」であり本物のRWAではない。

2モード:
  --compile-only   コンパイルのみ（ガス不要。先に検証できる）
                   成功すると ABI を contracts/MockYieldVault.abi.json に保存。
  --deploy         Base Sepolia へデプロイ（署名付きtx＝ガスが要る）。
                   成功すると VAULT_ADDRESS を .env に追記し、basescan リンクを表示。

実行:
  .venv\Scripts\python.exe scripts\deploy_vault.py --compile-only
  .venv\Scripts\python.exe scripts\deploy_vault.py --deploy
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "contracts" / "MockYieldVault.sol"
ABI_OUT = ROOT / "contracts" / "MockYieldVault.abi.json"
SOLC_VERSION = "0.8.24"

load_dotenv(ROOT / ".env")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
console = Console()


def compile_contract() -> tuple[list, str]:
    """MockYieldVault をコンパイルして (abi, bytecode) を返す。ABI はファイルにも保存。"""
    import solcx

    # 必要な solc が無ければ取得（初回のみネットワークアクセス）
    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        console.print(f"[dim]solc {SOLC_VERSION} を取得中...[/dim]")
        solcx.install_solc(SOLC_VERSION)

    compiled = solcx.compile_files(
        [str(CONTRACT_PATH)],
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
        optimize=True,
    )
    # キーは "<path>:MockYieldVault"。区切りはOS差があるので末尾一致で拾う
    key = next(k for k in compiled if k.endswith(":MockYieldVault"))
    abi = compiled[key]["abi"]
    bytecode = compiled[key]["bin"]

    ABI_OUT.write_text(json.dumps(abi, indent=2), encoding="utf-8")
    console.print(f"[green]✅ compiled[/green]  solc {SOLC_VERSION} / "
                  f"bytecode {len(bytecode)//2} bytes / ABI -> {ABI_OUT.relative_to(ROOT)}")
    return abi, bytecode


def _upsert_env(key: str, value: str) -> None:
    """.env の key を更新（無ければ追記）。他の行・秘密は保持。"""
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def deploy() -> None:
    from web3 import Web3
    from eth_account import Account

    abi, bytecode = compile_contract()

    rpc = os.environ["RPC_URL"]
    chain_id = int(os.environ.get("CHAIN_ID", "84532"))
    usdc = Web3.to_checksum_address(os.environ["USDC_ADDRESS"])
    apy_bps = int(os.environ.get("VAULT_APY_BPS", "500"))  # 既定 5%
    pk = os.environ["AGENT_PRIVATE_KEY"]

    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        console.print(f"[red]RPC に接続できません: {rpc}[/red]")
        sys.exit(1)

    # 安全確認：必ず Base Sepolia (84532) であること
    on_chain_id = w3.eth.chain_id
    if on_chain_id != chain_id or chain_id != 84532:
        console.print(f"[red]チェーンID不一致（期待 84532, RPC={on_chain_id}, env={chain_id}）。"
                      f"メインネット誤接続防止のため中止。[/red]")
        sys.exit(1)

    acct = Account.from_key(pk)
    eth = w3.eth.get_balance(acct.address)
    if eth == 0:
        console.print(
            f"[yellow]デプロイ用のガス(ETH)が 0 です。[/yellow]\n"
            f"  デプロイ元: {acct.address}\n"
            f"  Base Sepolia の faucet でテストETHを入れてください（例: Coinbase CDP / Alchemy）。\n"
            f"  コンパイル自体は ✅ 成功しているので、ETH 投入後に再実行してください。"
        )
        sys.exit(1)

    console.print(f"[dim]デプロイ元 {acct.address} / ETH {w3.from_wei(eth,'ether')} / "
                  f"asset(USDC) {usdc} / apy {apy_bps}bps[/dim]")

    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    tx = contract.constructor(usdc, apy_bps).build_transaction({
        "from": acct.address,
        "nonce": nonce,
        "chainId": chain_id,
        "gasPrice": w3.eth.gas_price,
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txh = w3.eth.send_raw_transaction(raw)
    console.print(f"[dim]デプロイtx送信: {txh.hex()} … receipt待ち[/dim]")
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    if receipt.status != 1:
        console.print(f"[red]デプロイ失敗（status={receipt.status}）。basescanで確認してください。[/red]")
        sys.exit(1)

    vault = receipt.contractAddress
    _upsert_env("VAULT_ADDRESS", vault)
    console.print(f"[bold green]✅ Vault デプロイ成功[/bold green]  {vault}")
    console.print(f"[dim]VAULT_ADDRESS を .env に記録しました。[/dim]")
    console.print(f"[blue]https://sepolia.basescan.org/address/{vault}[/blue]")


def seed(amount_usdc: float) -> None:
    """Vault に準備金（テストUSDC）を送る＝利回りの原資。普通のERC20 transfer。"""
    from web3 import Web3
    from eth_account import Account

    rpc = os.environ["RPC_URL"]
    chain_id = int(os.environ.get("CHAIN_ID", "84532"))
    usdc_addr = Web3.to_checksum_address(os.environ["USDC_ADDRESS"])
    vault_addr = Web3.to_checksum_address(os.environ["VAULT_ADDRESS"])
    pk = os.environ["AGENT_PRIVATE_KEY"]

    w3 = Web3(Web3.HTTPProvider(rpc))
    if w3.eth.chain_id != chain_id or chain_id != 84532:
        console.print("[red]チェーンID不一致（84532のみ）。中止。[/red]")
        sys.exit(1)

    erc20_transfer_abi = [{
        "constant": False,
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function",
    }]
    usdc = w3.eth.contract(address=usdc_addr, abi=erc20_transfer_abi)
    acct = Account.from_key(pk)
    amount_base = int(round(amount_usdc * 10 ** 6))

    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    tx = usdc.functions.transfer(vault_addr, amount_base).build_transaction({
        "from": acct.address, "nonce": nonce, "chainId": chain_id, "gasPrice": w3.eth.gas_price,
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txh = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    if receipt.status != 1:
        console.print(f"[red]シード失敗（status={receipt.status}）[/red]")
        sys.exit(1)
    console.print(f"[bold green]✅ 準備金シード成功[/bold green]  {amount_usdc} USDC -> Vault {vault_addr}")
    console.print(f"[blue]https://sepolia.basescan.org/tx/0x{txh.hex().lstrip('0x')}[/blue]")


def main() -> None:
    ap = argparse.ArgumentParser(description="MockYieldVault compile/deploy/seed (Base Sepolia only)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--compile-only", action="store_true", help="コンパイルのみ（ガス不要）")
    g.add_argument("--deploy", action="store_true", help="Base Sepolia へデプロイ")
    ap.add_argument("--seed", type=float, metavar="USDC", help="Vault に準備金(テストUSDC)を送る")
    args = ap.parse_args()

    if args.deploy:
        deploy()
    elif not args.seed:
        # 既定は安全側＝コンパイルのみ
        compile_contract()
        if not args.compile_only:
            console.print("[dim]デプロイするには --deploy を付けて再実行してください。[/dim]")

    if args.seed:
        seed(args.seed)


if __name__ == "__main__":
    main()
