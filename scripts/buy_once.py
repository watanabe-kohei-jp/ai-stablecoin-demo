r"""
手動で1回だけ x402 決済を体感するスクリプト（Phase 3a）

買い手側の最小フロー「402 → 署名(EIP-3009) → 支払い → 200」を *1回* だけ実行する。
wrapHttpxWithPayment が 402 を受けると自動で支払いペイロードを作り、再送して 200 を得る。

  - 署名に秘密鍵を使うが、**画面には出さない**（アドレス＝公開情報のみ表示）。
  - 買い手は ETH 不要（EIP-3009 ガスレス、ガスは facilitator が肩代わり）。
  - これは本物のオンチェーン決済（テストネット・価値ゼロ）。実行後に check_balance で
    買い手USDCが減り売り手が増えることを確認できる。

事前に別ターミナルで売り手サーバーを起動しておくこと:
  .venv\Scripts\python.exe -m uvicorn server.shop:app --port 8000

実行:
  .venv\Scripts\python.exe scripts\buy_once.py
"""
import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from eth_account import Account
from rich.console import Console
from rich.panel import Panel

from x402 import x402Client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.http.clients.httpx import wrapHttpxWithPayment
from x402.http import decode_payment_response_header, PAYMENT_RESPONSE_HEADER

# --- 設定読み込み ---
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AGENT_PRIVATE_KEY = os.environ["AGENT_PRIVATE_KEY"]
AGENT_ADDRESS = os.environ["AGENT_ADDRESS"]
CHAIN_ID = os.environ.get("CHAIN_ID", "84532")
NETWORK = f"eip155:{CHAIN_ID}"
SHOP_URL = os.environ.get("SHOP_URL", "http://127.0.0.1:8000")
TARGET_PATH = "/search/basic"  # $0.02 の安い業者を1回だけ買う

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

console = Console()


async def main() -> None:
    # 1. 秘密鍵から署名者を作る（鍵はメモリ上だけ・表示しない）
    account = Account.from_key(AGENT_PRIVATE_KEY)
    if account.address.lower() != AGENT_ADDRESS.lower():
        console.print(f"[red]警告: .env のアドレスと鍵から導いたアドレスが不一致[/red]")
    console.print(Panel.fit(
        f"買い手アドレス（公開情報）: [cyan]{account.address}[/cyan]\n"
        f"ネットワーク: {NETWORK}\n"
        f"購入先: {SHOP_URL}{TARGET_PATH}（$0.02）",
        title="x402 手動購入（Phase 3a）",
    ))

    signer = EthAccountSigner(account)

    # 2. x402 クライアントを作り、exact(EVM) スキームを登録
    client = x402Client()
    register_exact_evm_client(client, signer, networks=NETWORK)

    # 3. httpx を包んで GET（402 を受けたら自動で署名・支払い・再送）
    console.print("\n[bold]→ GET 実行（未払い→402→署名→支払い→200 を自動処理）[/bold]")
    try:
        async with wrapHttpxWithPayment(client, timeout=httpx.Timeout(60.0)) as http:
            resp = await http.get(SHOP_URL + TARGET_PATH)
    except Exception as e:
        console.print(f"[red]決済中にエラー: {type(e).__name__}: {e}[/red]")
        console.print("[dim]サーバー未起動 / facilitator通信 / 署名ドメイン不一致 などを確認[/dim]")
        sys.exit(1)

    # 4. 結果表示
    console.print(f"\n[bold]最終ステータス:[/bold] {resp.status_code}")
    if resp.status_code == 200:
        console.print("[green]✅ 支払い成功 → 商品データを受け取りました[/green]")
        try:
            console.print_json(data=resp.json())
        except Exception:
            console.print(resp.text[:500])

        # 決済レシート（PAYMENT-RESPONSE ヘッダ）があれば中身を表示
        receipt = resp.headers.get(PAYMENT_RESPONSE_HEADER)
        if receipt:
            try:
                decoded = decode_payment_response_header(receipt)
                console.print("\n[bold]決済レシート（PAYMENT-RESPONSE）:[/bold]")
                tx = getattr(decoded, "transaction", None) or getattr(decoded, "tx_hash", None)
                console.print(f"  success = {getattr(decoded, 'success', '?')}")
                console.print(f"  network = {getattr(decoded, 'network', '?')}")
                if tx:
                    console.print(f"  tx      = {tx}")
                    console.print(f"  explorer: https://sepolia.basescan.org/tx/{tx}")
            except Exception as e:
                console.print(f"[dim]レシートdecode失敗: {e}（生値: {receipt[:80]}…）[/dim]")
        console.print(
            "\n[dim]次: scripts\\check_balance.py を実行し、買い手USDCが約0.02減り"
            "売り手が増えていれば、本物のオンチェーン決済が成立した証拠です。[/dim]"
        )
    else:
        console.print(f"[red]想定外のステータス: {resp.status_code}[/red]")
        console.print(resp.text[:500])
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
