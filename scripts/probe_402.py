r"""
402 プローブ（Phase 2 の検証用クライアント）

売り手サーバー(server/shop.py)に *未払いのまま* アクセスして、
  - HTTP 402 Payment Required が返るか
  - PAYMENT-REQUIRED ヘッダの支払い条件が正しいか
    （scheme / network / pay_to / asset / amount）
を確認する。

※ これは「402 が正しく返るか」を確かめるだけの検査用クライアント。
  実際に署名して支払う「本物の買い手」は Phase 3 で作る（agent/）。
  facilitator 不調時は 402 ではなく 502 になり得るので、その場合は通信エラーとして区別する。

事前に別ターミナルでサーバーを起動しておくこと:
  .venv\Scripts\python.exe -m uvicorn server.shop:app --port 8000

実行:
  .venv\Scripts\python.exe scripts\probe_402.py
"""
import sys
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from x402.http import decode_payment_required_header, PAYMENT_REQUIRED_HEADER

# --- 設定読み込み ---
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE_URL = os.environ.get("SHOP_URL", "http://127.0.0.1:8000")
VENDOR_ADDRESS = os.environ["VENDOR_ADDRESS"]
USDC_ADDRESS = os.environ["USDC_ADDRESS"]
CHAIN_ID = os.environ.get("CHAIN_ID", "84532")
NETWORK = f"eip155:{CHAIN_ID}"

# 期待値：パス → amount（テストUSDC decimals=6 の最小単位）
EXPECT = {
    "/search/basic": "20000",    # $0.02
    "/search/premium": "50000",  # $0.05
    "/fetch": "50000",           # $0.05
    "/summarize": "30000",       # $0.03
}

# Windows 既定コンソール(cp932)でも化けないよう UTF-8 に統一
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

console = Console()


def _eq_addr(a: str, b: str) -> bool:
    return (a or "").lower() == (b or "").lower()


def check_one(path: str, expected_amount: str) -> dict:
    """1エンドポイントを未払いアクセスして検証結果を辞書で返す。"""
    url = BASE_URL + path
    result = {"path": path, "status": None, "ok": False, "notes": []}
    try:
        resp = httpx.get(url, timeout=20.0)
    except Exception as e:
        result["notes"].append(f"接続失敗: {e}")
        return result

    result["status"] = resp.status_code

    # facilitator 通信エラー等は 402 と区別する
    if resp.status_code == 502:
        result["notes"].append("502: facilitator 通信/初期化エラーの可能性")
        return result
    if resp.status_code != 402:
        result["notes"].append(f"402 以外が返った（設定ミスの可能性）: body={resp.text[:120]}")
        return result

    header = resp.headers.get(PAYMENT_REQUIRED_HEADER)
    if not header:
        result["notes"].append(f"{PAYMENT_REQUIRED_HEADER} ヘッダが無い")
        return result

    try:
        pr = decode_payment_required_header(header)
    except Exception as e:
        result["notes"].append(f"ヘッダ decode 失敗: {e}")
        return result

    accepts = getattr(pr, "accepts", None) or []
    if not accepts:
        result["notes"].append("accepts が空")
        return result

    req = accepts[0]
    scheme = getattr(req, "scheme", None)
    network = getattr(req, "network", None)
    pay_to = getattr(req, "pay_to", None)
    asset = getattr(req, "asset", None)
    amount = str(getattr(req, "amount", None))

    result.update({"scheme": scheme, "network": network, "pay_to": pay_to,
                   "asset": asset, "amount": amount})

    checks = [
        ("scheme==exact", scheme == "exact"),
        (f"network=={NETWORK}", network == NETWORK),
        ("pay_to==売り手", _eq_addr(pay_to, VENDOR_ADDRESS)),
        ("asset==テストUSDC", _eq_addr(asset, USDC_ADDRESS)),
        (f"amount=={expected_amount}", amount == expected_amount),
    ]
    failed = [name for name, ok in checks if not ok]
    if failed:
        result["notes"].append("NG: " + ", ".join(failed))
    else:
        result["ok"] = True
        result["notes"].append("全条件OK")
    return result


def main() -> None:
    console.print(f"[bold]402 プローブ[/bold]  対象: {BASE_URL}  network: {NETWORK}\n")

    # サーバー死活確認
    try:
        h = httpx.get(BASE_URL + "/health", timeout=10.0)
        console.print(f"[dim]/health -> {h.status_code} {h.json().get('status')}[/dim]\n")
    except Exception as e:
        console.print(f"[red]サーバーに接続できません（先に uvicorn を起動してください）: {e}[/red]")
        sys.exit(1)

    table = Table(title="未払いアクセス検証（期待: 402 + 正しい支払い条件）")
    table.add_column("パス", style="cyan")
    table.add_column("status", justify="right")
    table.add_column("amount", justify="right")
    table.add_column("pay_to", style="dim")
    table.add_column("asset", style="dim")
    table.add_column("判定")

    all_ok = True
    for path, amount in EXPECT.items():
        r = check_one(path, amount)
        all_ok = all_ok and r["ok"]
        mark = "[green]OK[/green]" if r["ok"] else "[red]NG[/red]"
        table.add_row(
            path,
            str(r.get("status")),
            str(r.get("amount", "-")),
            (r.get("pay_to") or "-")[:14] + "…" if r.get("pay_to") else "-",
            (r.get("asset") or "-")[:14] + "…" if r.get("asset") else "-",
            f"{mark}  " + "; ".join(r["notes"]),
        )

    console.print(table)
    if all_ok:
        console.print("\n[bold green]✅ Phase 2 検証成功[/bold green]：全ルートが正しい 402 支払い条件を返しました。")
    else:
        console.print("\n[bold red]❌ 一部のルートが期待どおりではありません。[/bold red]上の判定欄を確認してください。")
        sys.exit(2)


if __name__ == "__main__":
    main()
