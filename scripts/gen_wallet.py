"""
使い捨てテストウォレット生成スクリプト（Phase 1）

- 買い手(agent) と 売り手(vendor) の2つのEVMアカウントを生成する
- 秘密鍵は .env にだけ書き込む（.env は .gitignore 済み）
- 画面には「アドレス（公開してよい情報）」だけ表示する
- 既存の .env があれば上書きせず中断する（安全装置）

※ ここで作る鍵は「テストネット練習用の使い捨て」。本物の資産は絶対に入れないこと。
"""
from pathlib import Path
from eth_account import Account

# プロジェクトルート（このファイルの1つ上）
ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# --- 安全装置：既存 .env を壊さない ---
if ENV_PATH.exists():
    print(f"[中断] {ENV_PATH} は既に存在します。")
    print("       上書きを避けるため何もしませんでした。作り直したい場合は手動で .env を削除してください。")
    raise SystemExit(1)

# --- 2つのアカウントを生成（秘密鍵はランダム） ---
agent = Account.create()    # 買い手：APIを買う側
vendor = Account.create()   # 売り手：代金を受け取る側

# --- .env の中身を組み立てる ---
env_text = f"""# ===== 自動生成（テストネット専用・使い捨て）=====
# このファイルは .gitignore 済み。秘密鍵をGitHubに上げない安全装置。
# ここの鍵には本物の資産を絶対に入れないこと。

# --- 買い手エージェントのウォレット ---
AGENT_PRIVATE_KEY={agent.key.hex()}
AGENT_ADDRESS={agent.address}

# --- 売り手（代金の受取先）のウォレット ---
VENDOR_PRIVATE_KEY={vendor.key.hex()}
VENDOR_ADDRESS={vendor.address}

# --- ネットワーク設定（Base Sepolia テストネット） ---
NETWORK=base-sepolia
CHAIN_ID=84532
# Base Sepolia の公式テストUSDC（decimals=6）
USDC_ADDRESS=0x036CbD53842c5426634e7929541eC2318f3dCF7e
# テストネット用 facilitator（無認証）
FACILITATOR_URL=https://x402.org/facilitator

# --- 残高確認用 RPC（公開エンドポイント） ---
RPC_URL=https://sepolia.base.org
"""

ENV_PATH.write_text(env_text, encoding="utf-8")

# --- 画面には公開情報（アドレス）だけ ---
print("[OK] .env を生成しました（秘密鍵はファイル内のみ。画面には出しません）")
print()
print("=== 公開してよい情報（アドレス）===")
print(f"  買い手(agent)  アドレス : {agent.address}")
print(f"  売り手(vendor) アドレス : {vendor.address}")
print()
print("→ faucet に貼るのは『買い手(agent)アドレス』です。")
