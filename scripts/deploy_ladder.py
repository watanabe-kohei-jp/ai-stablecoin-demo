r"""
流動性の階段（トークン化預金Vault + 模擬ST）のコンパイル / デプロイ / シード
=======================================================================
⚠️ Base Sepolia 限定。本物の資産には触れない。どちらも学習用の「模擬」。

構成（3層の階段）:
  手前: USDC（決済用・利回りなし）
  中間: MockYieldVault を低APYで再利用 = 「模擬トークン化預金」（即時償還）
  奥  : MockSecurityToken = 「模擬ST」（高APY・二段階償還 = 償還ラグあり）

モード:
  --compile-only     両コントラクトのコンパイルのみ（ガス不要）
  --deploy-deposit   模擬トークン化預金（MockYieldVault, 低APY）をデプロイ
  --deploy-st        模擬ST（MockSecurityToken）をデプロイ
  --seed-deposit N   預金Vaultへ準備金 N USDC を送る（利回り原資）
  --seed-st N        STへ準備金 N USDC を送る（利回り原資）

.env に書くキー:
  DEPOSIT_VAULT_ADDRESS / ST_ADDRESS
  （任意）DEPOSIT_APY_BPS=100, ST_APY_BPS=800, ST_REDEEM_DELAY_SEC=120

実行例:
  .venv\Scripts\python.exe scripts\deploy_ladder.py --compile-only
  .venv\Scripts\python.exe scripts\deploy_ladder.py --deploy-deposit --deploy-st
  .venv\Scripts\python.exe scripts\deploy_ladder.py --seed-deposit 1 --seed-st 1
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
SOLC_VERSION = "0.8.24"
CONTRACTS = {
    "MockYieldVault": ROOT / "contracts" / "MockYieldVault.sol",
    "MockSecurityToken": ROOT / "contracts" / "MockSecurityToken.sol",
}

load_dotenv(ROOT / ".env")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
console = Console()


def compile_contract(name: str) -> tuple[list, str]:
    """指定コントラクトをコンパイルして (abi, bytecode) を返す。ABI はファイルにも保存。"""
    import solcx

    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        console.print(f"[dim]solc {SOLC_VERSION} を取得中...[/dim]")
        solcx.install_solc(SOLC_VERSION)

    path = CONTRACTS[name]
    compiled = solcx.compile_files(
        [str(path)], output_values=["abi", "bin"], solc_version=SOLC_VERSION, optimize=True,
    )
    key = next(k for k in compiled if k.endswith(f":{name}"))
    abi = compiled[key]["abi"]
    bytecode = compiled[key]["bin"]

    abi_out = ROOT / "contracts" / f"{name}.abi.json"
    abi_out.write_text(json.dumps(abi, indent=2), encoding="utf-8")
    console.print(f"[green]✅ compiled[/green] {name}  solc {SOLC_VERSION} / "
                  f"bytecode {len(bytecode)//2} bytes / ABI -> {abi_out.relative_to(ROOT)}")
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


def _w3_and_account():
    """Base Sepolia 接続＋チェーンID検証＋アカウント。（84532 以外は即中止）"""
    from web3 import Web3
    from eth_account import Account

    rpc = os.environ["RPC_URL"]
    chain_id = int(os.environ.get("CHAIN_ID", "84532"))
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        console.print(f"[red]RPC に接続できません: {rpc}[/red]")
        sys.exit(1)
    if w3.eth.chain_id != chain_id or chain_id != 84532:
        console.print(f"[red]チェーンID不一致（期待 84532, RPC={w3.eth.chain_id}）。中止。[/red]")
        sys.exit(1)
    acct = Account.from_key(os.environ["AGENT_PRIVATE_KEY"])
    return w3, acct, chain_id


def _send(w3, acct, chain_id, tx_builder, what: str):
    """署名付きtxを送信し receipt(status==1) まで確認。失敗は即中止（自動再送しない）。"""
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    tx = tx_builder({
        "from": acct.address, "nonce": nonce, "chainId": chain_id, "gasPrice": w3.eth.gas_price,
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txh = w3.eth.send_raw_transaction(raw)
    console.print(f"[dim]{what}: tx {txh.hex()} … receipt待ち[/dim]")
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    if receipt.status != 1:
        console.print(f"[red]{what} 失敗（status={receipt.status}）。basescanで確認してください。[/red]")
        sys.exit(1)
    return receipt


def deploy_deposit() -> None:
    """模擬トークン化預金 = MockYieldVault を低APYでデプロイ。"""
    from web3 import Web3
    abi, bytecode = compile_contract("MockYieldVault")
    w3, acct, chain_id = _w3_and_account()
    usdc = Web3.to_checksum_address(os.environ["USDC_ADDRESS"])
    apy_bps = int(os.environ.get("DEPOSIT_APY_BPS", "100"))  # 既定 1%（預金らしく低め）

    if w3.eth.get_balance(acct.address) == 0:
        console.print("[yellow]ガス(ETH)が 0 です。faucet 後に再実行してください。[/yellow]")
        sys.exit(1)

    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    receipt = _send(w3, acct, chain_id,
                    lambda p: contract.constructor(usdc, apy_bps).build_transaction(p),
                    "預金Vaultデプロイ")
    addr = receipt.contractAddress
    _upsert_env("DEPOSIT_VAULT_ADDRESS", addr)
    console.print(f"[bold green]✅ 模擬トークン化預金 デプロイ成功[/bold green]  {addr}  (APY {apy_bps}bps)")
    console.print(f"[blue]https://sepolia.basescan.org/address/{addr}[/blue]")


def deploy_st() -> None:
    """模擬ST = MockSecurityToken（高APY・二段階償還）をデプロイ。"""
    from web3 import Web3
    abi, bytecode = compile_contract("MockSecurityToken")
    w3, acct, chain_id = _w3_and_account()
    usdc = Web3.to_checksum_address(os.environ["USDC_ADDRESS"])
    apy_bps = int(os.environ.get("ST_APY_BPS", "800"))            # 既定 8%（STらしく高め）
    delay = int(os.environ.get("ST_REDEEM_DELAY_SEC", "120"))     # 既定 120秒 = T+N の模擬

    if w3.eth.get_balance(acct.address) == 0:
        console.print("[yellow]ガス(ETH)が 0 です。faucet 後に再実行してください。[/yellow]")
        sys.exit(1)

    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    receipt = _send(w3, acct, chain_id,
                    lambda p: contract.constructor(usdc, apy_bps, delay).build_transaction(p),
                    "STデプロイ")
    addr = receipt.contractAddress
    _upsert_env("ST_ADDRESS", addr)
    console.print(f"[bold green]✅ 模擬ST デプロイ成功[/bold green]  {addr}  "
                  f"(APY {apy_bps}bps / 償還ラグ {delay}秒)")
    console.print(f"[blue]https://sepolia.basescan.org/address/{addr}[/blue]")


def seed(env_key: str, label: str, amount_usdc: float) -> None:
    """準備金（テストUSDC）を送る＝利回りの原資。普通のERC20 transfer。"""
    from web3 import Web3
    w3, acct, chain_id = _w3_and_account()
    usdc_addr = Web3.to_checksum_address(os.environ["USDC_ADDRESS"])
    target = Web3.to_checksum_address(os.environ[env_key])

    erc20_transfer_abi = [{
        "constant": False,
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function",
    }]
    usdc = w3.eth.contract(address=usdc_addr, abi=erc20_transfer_abi)
    amount_base = int(round(amount_usdc * 10 ** 6))
    _send(w3, acct, chain_id,
          lambda p: usdc.functions.transfer(target, amount_base).build_transaction(p),
          f"{label}へ準備金シード")
    console.print(f"[bold green]✅ {label} に準備金 {amount_usdc} USDC を送付[/bold green]  {target}")


def main() -> None:
    ap = argparse.ArgumentParser(description="liquidity ladder deploy (Base Sepolia only)")
    ap.add_argument("--compile-only", action="store_true", help="両コントラクトのコンパイルのみ")
    ap.add_argument("--deploy-deposit", action="store_true", help="模擬トークン化預金をデプロイ")
    ap.add_argument("--deploy-st", action="store_true", help="模擬STをデプロイ")
    ap.add_argument("--seed-deposit", type=float, metavar="USDC", help="預金Vaultへ準備金を送る")
    ap.add_argument("--seed-st", type=float, metavar="USDC", help="STへ準備金を送る")
    args = ap.parse_args()

    if args.compile_only or not any([args.deploy_deposit, args.deploy_st,
                                     args.seed_deposit, args.seed_st]):
        # 既定は安全側＝コンパイルのみ
        compile_contract("MockYieldVault")
        compile_contract("MockSecurityToken")
        if not args.compile_only:
            console.print("[dim]デプロイは --deploy-deposit / --deploy-st を付けて再実行。[/dim]")
        return

    if args.deploy_deposit:
        deploy_deposit()
    if args.deploy_st:
        deploy_st()
    if args.seed_deposit:
        seed("DEPOSIT_VAULT_ADDRESS", "預金Vault", args.seed_deposit)
    if args.seed_st:
        seed("ST_ADDRESS", "ST", args.seed_st)


if __name__ == "__main__":
    main()
