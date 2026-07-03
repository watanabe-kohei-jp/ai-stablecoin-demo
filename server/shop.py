r"""
売り手サーバー（Phase 2）— x402 で保護した従量課金 API

x402 の「売り手側」を最小構成で実装する。お金を払っていないリクエストには
HTTP 402 Payment Required（＋支払い条件）を返し、payment_middleware が
facilitator と連携して検証・決済できたリクエストだけ本体ハンドラに通す。

重要・役割分担（ここが x402 の肝）:
  - このサーバーは「いくら・どの通貨・誰宛で」という *支払い条件を提示するだけ*。
    売り手は **秘密鍵を持たない / 署名しない**。
  - 署名検証とオンチェーン決済は **facilitator**（外部サービス）が代行する。

構成部品（x402 2.12.0 の実APIで確認済み）:
  1. HTTPFacilitatorClient   … facilitator への窓口（既定 https://x402.org/facilitator）
  2. x402ResourceServer      … 支払い条件を組み立てる売り手の頭脳
  3. register_exact_evm_server … "exact"（きっかり指定額）EVMスキームを有効化
  4. payment_middleware      … 未払いを 402 で弾く FastAPI ミドルウェア

起動:
  .venv\Scripts\python.exe -m uvicorn server.shop:app --reload --port 8000
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact.register import register_exact_evm_server

# --- 設定読み込み（.env） ---
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

VENDOR_ADDRESS = os.environ["VENDOR_ADDRESS"]            # 売り手の受取アドレス
CHAIN_ID = os.environ.get("CHAIN_ID", "84532")          # Base Sepolia
NETWORK = f"eip155:{CHAIN_ID}"                           # CAIP-2 形式: eip155:84532
FACILITATOR_URL = os.environ.get("FACILITATOR_URL")     # 既定でも可（None なら内部既定値）

# ---------------------------------------------------------------------------
# 1〜3. facilitator → resource server → exact(EVM) スキーム登録
# ---------------------------------------------------------------------------
# FACILITATOR_URL が .env にあればそれを、無ければライブラリ既定値を使う
if FACILITATOR_URL:
    facilitator = HTTPFacilitatorClient({"url": FACILITATOR_URL})
else:
    facilitator = HTTPFacilitatorClient()

server = x402ResourceServer(facilitator)
register_exact_evm_server(server, networks=NETWORK)


def _accepts(price: str) -> dict:
    """1つの支払い条件（PaymentOption）を作る小さなヘルパー。

    price は "$0.02" のような USD 建て表記。Base Sepolia では
    テストUSDC(0x036C...dCF7e, decimals=6) に自動解決される（$0.02 → 20000 単位）。
    asset は明示しない（dollar-string が既定アセットに解決される設計）。
    """
    return {
        "scheme": "exact",          # きっかり指定額方式
        "pay_to": VENDOR_ADDRESS,   # 受取先（売り手）
        "price": price,             # USD建て表記
        "network": NETWORK,         # eip155:84532
    }


# ---------------------------------------------------------------------------
# ルート設定（どのパスをいくらで保護するか）
#   "安い業者" /search/basic と "高い業者" /search/premium は別ルートで表現
#   （同一ルートに複数 accepts を並べると買い手は安い方を選ぶだけなので分ける）
# ---------------------------------------------------------------------------
ROUTES = {
    "GET /search/basic": {
        "accepts": _accepts("$0.02"),
        "description": "標準の検索（安い業者）",
        "mime_type": "application/json",
    },
    "GET /search/premium": {
        "accepts": _accepts("$0.05"),
        "description": "高品質な検索（高い業者）",
        "mime_type": "application/json",
    },
    "GET /fetch": {
        "accepts": _accepts("$0.05"),
        "description": "記事・ページの取得",
        "mime_type": "application/json",
    },
    "GET /summarize": {
        "accepts": _accepts("$0.03"),
        "description": "テキストの要約",
        "mime_type": "application/json",
    },
}

# ---------------------------------------------------------------------------
# 4. FastAPI アプリ + 支払いミドルウェア
#    ミドルウェアは「起動時に1回だけ」生成して使い回す（毎リクエスト生成は非効率）。
#    sync_facilitator_on_start=True（既定）= 初回リクエスト時に facilitator情報を取得（lazy）。
# ---------------------------------------------------------------------------
app = FastAPI(title="x402 Demo Shop（売り手サーバー）", version="0.2.0")

_x402_mw = payment_middleware(ROUTES, server)


@app.middleware("http")
async def x402_middleware(request, call_next):
    # 未払いなら 402 を返し、支払い済みなら下のハンドラに通す
    return await _x402_mw(request, call_next)


# ---------------------------------------------------------------------------
# エンドポイント本体
#   ※ これらは「支払いが通った後」だけ実行される（未払いは middleware が 402 で遮断）。
#   返すデータは Phase 3 で買い手が受け取る「商品」に相当（デモ用のダミー）。
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """死活確認（無料・保護しない）。ROUTES に無いので素通しされる。"""
    return {
        "status": "ok",
        "service": "x402 demo shop",
        "network": NETWORK,
        "vendor": VENDOR_ADDRESS,
        "protected_routes": list(ROUTES.keys()),
    }


@app.get("/search/basic")
async def search_basic(q: str = "AI stablecoin"):
    return {
        "vendor": "basic",
        "price": "$0.02",
        "query": q,
        "results": [
            {"title": f"[basic] {q} の概要", "snippet": "標準品質の検索結果（デモ）"},
            {"title": f"[basic] {q} 関連トピック", "snippet": "..."},
        ],
    }


@app.get("/search/premium")
async def search_premium(q: str = "AI stablecoin"):
    return {
        "vendor": "premium",
        "price": "$0.05",
        "query": q,
        "results": [
            {"title": f"[premium] {q} の詳細分析", "snippet": "高品質・出典付きの検索結果（デモ）", "source": "https://example.com/a"},
            {"title": f"[premium] {q} の最新動向", "snippet": "...", "source": "https://example.com/b"},
            {"title": f"[premium] {q} の専門家見解", "snippet": "...", "source": "https://example.com/c"},
        ],
    }


@app.get("/fetch")
async def fetch(url: str = "https://example.com"):
    return {
        "price": "$0.05",
        "url": url,
        "content": f"（デモ）{url} から取得した本文テキスト ...",
    }


@app.get("/summarize")
async def summarize(text: str = ""):
    preview = (text[:40] + "...") if len(text) > 40 else text
    return {
        "price": "$0.03",
        "input_preview": preview,
        "summary": "（デモ）入力テキストの要約結果 ...",
    }
