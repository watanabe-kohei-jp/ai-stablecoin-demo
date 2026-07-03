r"""
ウォレット層（Phase 3b）— 鍵を持つ唯一の信頼境界

このモジュールだけが秘密鍵を扱い、署名する。頭脳(brain)やループ(agent)からは
高レベルの `buy(path)` だけが見え、鍵には触れない。買いすぎ・買ってはいけない先への
支払いを **署名する前に** 物理的に止める。

3つの手綱（人間が握る限定権限）:
  1. 予算上限     … 累計支払いが上限を超える支払いを on_before_payment_creation フックで中止
  2. 許可リスト   … 許可していないパスは buy() がネットワークに出る前に拒否
  3. 監査ログ     … 成功・拒否を含む全試行を self.audit に記録

二重防御：許可リストは buy() で早期拒否、予算は「実際の請求額」を知れる署名前フックで強制。
これにより、頭脳が暴走しても wallet 層が最後の砦になる。

USDC は decimals=6（最小単位 1,000,000 = 1 USDC）。

リトライの安全性（重要）:
  公開 facilitator は連続決済で一時的に settle 失敗することがある。再試行してよいのは
  「サーバーが明示的に 402 を返した＝資金が動いていないと確定できる」場合のみ。
  例外・タイムアウト・その他の非200は「支払い済みかどうか不明」なので、二重支払いを
  避けるため再試行しない（状態不明として中止する）。

既知の制限（デモのため未対応。本番化時の宿題）:
  - 並行実行は asyncio.Lock で直列化済みだが、複数プロセス/複数Walletでの予算共有は未対応
  - 「署名済/送信済/成功確認」の厳密な台帳分離は未実装（200確認時のみ spent に計上）
  - 金額は float 換算（厳密には Decimal 推奨）／決済の冪等キー(idempotency)は未導入
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

from x402 import x402Client, AbortResult
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.http.clients.httpx import wrapHttpxWithPayment
from x402.http import decode_payment_response_header, PAYMENT_RESPONSE_HEADER

USDC_DECIMALS = 6
_UNIT = 10 ** USDC_DECIMALS

# 自動スイープ用：Vault ABI（deploy_vault.py が出力）と、USDC の最小 ERC20 ABI
_VAULT_ABI_PATH = Path(__file__).resolve().parent.parent / "contracts" / "MockYieldVault.abi.json"
_ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


def usdc_to_base(amount_usdc: float) -> int:
    """USDC 額（例 0.05）を最小単位（50000）に変換。"""
    return int(round(amount_usdc * _UNIT))


def base_to_usdc(base: int) -> float:
    return base / _UNIT


@dataclass
class PurchaseResult:
    """buy() の結果。成功・拒否のどちらでも返す（例外を投げない設計）。"""
    path: str
    ok: bool
    status: int | None = None
    amount_base: int = 0          # 実際に支払った最小単位（拒否時は0）
    reason: str = ""              # 拒否理由 or 成功メモ
    data: dict | None = None      # 200 時の商品データ
    tx: str | None = None         # 決済トランザクションハッシュ


class Wallet:
    """鍵・署名・予算・許可リスト・監査ログを内包する信頼境界。"""

    def __init__(
        self,
        private_key: str,
        network: str,
        budget_usdc: float,
        allowlist: list[str],
        shop_url: str,
        *,
        rpc_url: str | None = None,
        usdc_address: str | None = None,
        vault_address: str | None = None,
        min_operating_usdc: float = 0.0,
        max_vault_alloc: float = 1.0,
    ) -> None:
        self._account = Account.from_key(private_key)   # 鍵はここだけ（非公開）
        self.address = self._account.address            # アドレスは公開情報
        self.network = network
        self.shop_url = shop_url.rstrip("/")
        self.budget_base = usdc_to_base(budget_usdc)
        self.spent_base = 0
        self.allowlist = set(allowlist)
        self.audit: list[PurchaseResult] = []

        # --- 財務（自動スイープ）層：任意。rpc_url/usdc_address が揃った時だけ有効化 ---
        # 設計：鍵・署名・tx送信は wallet のみ（信頼境界の維持）。
        self._w3: Web3 | None = None
        self._usdc = None
        self._vault = None
        self._chain_id = int(network.split(":")[-1])    # "eip155:84532" -> 84532
        self.min_operating_base = usdc_to_base(min_operating_usdc)
        self.max_vault_alloc = max_vault_alloc
        self.treasury_audit: list[str] = []
        if rpc_url and usdc_address:
            self._w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not self._w3.is_connected():
                raise RuntimeError(f"RPC接続失敗: {rpc_url}")
            # テストネット強制（メインネット誤接続・実資金事故の防止）
            on_chain = self._w3.eth.chain_id
            if on_chain != self._chain_id or self._chain_id != 84532:
                raise RuntimeError(
                    f"チェーンID不一致/非テストネット: rpc={on_chain}, env={self._chain_id}（84532のみ許可）"
                )
            self._usdc_address = Web3.to_checksum_address(usdc_address)
            self._usdc = self._w3.eth.contract(address=self._usdc_address, abi=_ERC20_ABI)
            if vault_address:
                vault_abi = json.loads(_VAULT_ABI_PATH.read_text(encoding="utf-8"))
                self._vault_address = Web3.to_checksum_address(vault_address)
                self._vault = self._w3.eth.contract(address=self._vault_address, abi=vault_abi)
                # Vault.asset() がこの USDC を指すか検証（誤Vault・誤資産の防止）
                vasset = Web3.to_checksum_address(self._vault.functions.asset().call())
                if vasset != self._usdc_address:
                    raise RuntimeError(f"Vault.asset()がUSDCと不一致: {vasset} != {self._usdc_address}")

        # x402 クライアント（署名前フックで予算を強制）
        self._signer = EthAccountSigner(self._account)
        self._client = x402Client()
        register_exact_evm_client(self._client, self._signer, networks=network)
        self._client.on_before_payment_creation(self._guard)
        # フックが承認した支払いの最小単位を一時保持（200成功時に確定計上）
        self._pending_base: int | None = None
        # フックが予算で中止した場合の理由（buy() で「エラー」と区別するため）
        self._last_abort_reason: str | None = None
        # 流動性不足で JIT 償還が必要な額（buy() が拾って償還→再実行）
        self._need_redeem_base: int = 0
        self._need_amount_base: int = 0
        # 予算チェック→計上を直列化（同一Walletの並行 buy() 競合を防ぐ: Codex#1）
        self._lock = asyncio.Lock()

    # --- 残予算 ---
    @property
    def remaining_base(self) -> int:
        return self.budget_base - self.spent_base

    @property
    def remaining_usdc(self) -> float:
        return base_to_usdc(self.remaining_base)

    # --- 署名前フック：予算超過を止め、流動性不足なら JIT 償還を要求（実額ベース） ---
    def _guard(self, ctx) -> AbortResult | None:
        req = ctx.selected_requirements
        amount = int(req.amount)
        # 1) 予算上限（ハードキャップ）：超えるなら確定的に中止
        if self.spent_base + amount > self.budget_base:
            self._pending_base = None
            self._need_redeem_base = 0
            reason = f"予算超過: 残り ${self.remaining_usdc:.2f} < 必要 ${base_to_usdc(amount):.2f}"
            self._last_abort_reason = reason
            return AbortResult(reason=reason)
        # 2) 流動性（Vault有効時のみ）：流動USDCが足りなければ一度中止し、
        #    buy() が不足分を Vault から償還（JIT）してから再実行する。価格表には依存しない。
        if self.vault_enabled:
            liquid = self.liquid_usdc_base()
            if liquid < amount:
                self._pending_base = None
                self._need_redeem_base = amount - liquid
                self._need_amount_base = amount
                return AbortResult(
                    reason=f"流動性不足→JIT償還: 流動 ${base_to_usdc(liquid):.4f} < 必要 ${base_to_usdc(amount):.4f}"
                )
        self._pending_base = amount  # 承認。200成功時に spent へ確定計上
        self._need_redeem_base = 0
        return None

    # --- 唯一の購買窓口（頭脳が呼ぶ） ---
    async def buy(self, path: str, params: dict | None = None) -> PurchaseResult:
        # 0. パス形式の検証（多層防御：URL組み立て前に変な値を弾く）
        if not path.startswith("/") or ".." in path or "//" in path[1:]:
            r = PurchaseResult(path=path, ok=False, reason="不正なパス形式（拒否）")
            self.audit.append(r)
            return r

        # 1. 許可リスト（ネットワークに出る前に弾く）
        if path not in self.allowlist:
            r = PurchaseResult(path=path, ok=False, reason="許可リスト外（買わない）")
            self.audit.append(r)
            return r

        # 2. 支払い（402→署名→支払い→200 を自動処理）。予算判定〜計上を直列化する。
        async with self._lock:
            return await self._buy_locked(path, params)

    async def _buy_locked(self, path: str, params: dict | None) -> PurchaseResult:
        url = self.shop_url + path
        MAX_TRIES = 3
        RETRY_WAIT = 1.5
        last_status: int | None = None
        jit_done = False   # JIT償還はこの buy() で最大1回
        for attempt in range(1, MAX_TRIES + 1):
            self._pending_base = None
            self._last_abort_reason = None
            self._need_redeem_base = 0
            try:
                async with wrapHttpxWithPayment(self._client, timeout=httpx.Timeout(60.0)) as http:
                    resp = await http.get(url, params=params)
            except Exception as e:
                # JIT: 流動性不足で中止された → 不足分を Vault から償還して再実行（価格表に非依存）
                if self._need_redeem_base and self.vault_enabled and not jit_done:
                    need_amount = self._need_amount_base
                    self._need_redeem_base = 0
                    pos = self.vault_position_base()
                    # 流動性の「読み」は公開RPCで遅延しうるので、不足分(=必要額−流動性)では
                    # なく「支払い額そのもの」を丸ごと償還する。これにより stale read による
                    # 過少償還を防ぎ、手元の運転資金(min_operating)も自然に温存される。
                    redeem_amt = min(need_amount, pos)
                    if redeem_amt > 0:
                        self.treasury_audit.append(
                            f"JIT償還: 支払い額 ${base_to_usdc(need_amount):.4f} 分を Vault から引き出し"
                        )
                        self._withdraw_from_vault_locked(redeem_amt)
                        self._wait_liquid(need_amount)   # 反映待ち（RPC遅延対策）
                        jit_done = True
                        continue
                # フックが予算で中止した場合は PaymentError に内包されて飛んでくる（意図的拒否・再試行しない）。
                if self._last_abort_reason:
                    r = PurchaseResult(path=path, ok=False, reason=f"予算で拒否: {self._last_abort_reason}")
                    self.audit.append(r)
                    return r
                # 例外＝決済状態が不明（支払い後タイムアウト等の可能性）。
                # 二重支払いを避けるため再試行しない（Codex#2）。
                r = PurchaseResult(path=path, ok=False,
                                   reason=f"決済状態不明のため中止（再試行せず）: {type(e).__name__}: {e}")
                self.audit.append(r)
                return r

            # 200 = 支払い成功
            if resp.status_code == 200:
                amount = self._pending_base or 0
                self.spent_base += amount  # 実支出を確定計上
                tx = None
                receipt = resp.headers.get(PAYMENT_RESPONSE_HEADER)
                if receipt:
                    try:
                        decoded = decode_payment_response_header(receipt)
                        tx = getattr(decoded, "transaction", None) or getattr(decoded, "tx_hash", None)
                    except Exception:
                        pass
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text[:300]}
                note = f"購入成功 ${base_to_usdc(amount):.2f}" + (f"（{attempt}回目で成功）" if attempt > 1 else "")
                r = PurchaseResult(path=path, ok=True, status=200, amount_base=amount,
                                   reason=note, data=data, tx=tx)
                self.audit.append(r)
                return r

            last_status = resp.status_code
            # 明示的な 402 ＝ サーバーが「未払い」と確定 → 資金未移動なので安全に再試行できる（Codex#2）
            if resp.status_code == 402 and attempt < MAX_TRIES:
                await asyncio.sleep(RETRY_WAIT)
                continue
            # それ以外の非200（5xx等）は状態不明 → 再試行せず中止
            if resp.status_code != 402:
                r = PurchaseResult(path=path, ok=False, status=last_status,
                                   reason=f"未購入（status={last_status}・状態不明のため再試行せず）")
                self.audit.append(r)
                return r
            # 402 のままリトライ上限に到達
            break

        r = PurchaseResult(path=path, ok=False, status=last_status,
                           reason=f"未購入（status={last_status}・{MAX_TRIES}回試行）")
        self.audit.append(r)
        return r

    # ====================== 財務（自動スイープ）層 ======================
    # 鍵・署名・tx送信は wallet のみ（信頼境界）。Vault への approve/deposit/withdraw は
    # 通常の書き込みtx＝ガスが要る。x402 と同じく「状態不明なら再送しない」を守る。
    # ※ web3 は同期APIなので、デモの単一エージェント前提で同期呼び出しする。

    @property
    def vault_enabled(self) -> bool:
        return self._vault is not None

    def liquid_usdc_base(self) -> int:
        """オンチェーンの実 USDC 残高（最小単位）。予算残(remaining_base)とは別物。"""
        if self._usdc is None:
            raise RuntimeError("財務層が未設定（rpc_url/usdc_address が必要）")
        return int(self._usdc.functions.balanceOf(self._account.address).call())

    def liquid_usdc(self) -> float:
        return base_to_usdc(self.liquid_usdc_base())

    def _send_tx(self, fn, label: str):
        """署名付きtxを送り receipt status==1 まで確認。
        送信後に状態不明（タイムアウト等）になったら再送せず例外（二重実行防止・Codex#5）。"""
        acct = self._account
        nonce = self._w3.eth.get_transaction_count(acct.address, "pending")
        # ガス見積りに2倍の余裕。公開RPCが古い状態でestimateすると過少になり
        # 実行時 out-of-gas で revert することがあるため（Base L2は実費=使った分だけ）。
        gas_est = fn.estimate_gas({"from": acct.address})
        tx = fn.build_transaction({
            "from": acct.address,
            "nonce": nonce,
            "chainId": self._chain_id,
            "gasPrice": self._w3.eth.gas_price,
            "gas": int(gas_est * 2),
        })
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        txh = self._w3.eth.send_raw_transaction(raw)
        receipt = self._w3.eth.wait_for_transaction_receipt(txh, timeout=180)
        if receipt.status != 1:
            raise RuntimeError(f"{label} 失敗 (status={receipt.status}, tx={txh.hex()})")
        self.treasury_audit.append(f"{label}: tx={txh.hex()}")
        return receipt

    def _wait_allowance(self, min_amount: int, tries: int = 12, wait: float = 2.0) -> None:
        """approve 反映を待つ。公開RPCは read-after-write 遅延があるため、
        allowance が min_amount 以上に見えるまでポーリングしてから次の tx を出す。"""
        for _ in range(tries):
            a = int(self._usdc.functions.allowance(self._account.address, self._vault_address).call())
            if a >= min_amount:
                return
            time.sleep(wait)
        raise RuntimeError("approve の反映待ちタイムアウト（RPC遅延の可能性）")

    def _wait_liquid(self, min_amount: int, tries: int = 12, wait: float = 2.0) -> None:
        """JIT償還後、流動USDCが必要額に達するまで待つ（公開RPCの反映遅延対策）。"""
        for _ in range(tries):
            if self.liquid_usdc_base() >= min_amount:
                return
            time.sleep(wait)

    # --- ロック保持前提（buy() 内JITから呼ぶ用） ---
    def _deposit_to_vault_locked(self, assets_base: int) -> None:
        # 有限 approve（無限承認は信頼境界を弱めるので禁止・Codex重要）：不足分だけ承認
        allowance = int(self._usdc.functions.allowance(self._account.address, self._vault_address).call())
        if allowance < assets_base:
            self._send_tx(self._usdc.functions.approve(self._vault_address, assets_base), "approve")
            self._wait_allowance(assets_base)   # RPC反映待ち（exceeds allowance 回避）
        self._send_tx(self._vault.functions.deposit(assets_base), f"deposit ${base_to_usdc(assets_base):.4f}")

    def _withdraw_from_vault_locked(self, assets_base: int) -> None:
        self._send_tx(self._vault.functions.withdraw(assets_base), f"withdraw ${base_to_usdc(assets_base):.4f}")

    # --- 公開API（ロックを取得して実行） ---
    async def deposit_to_vault(self, assets_usdc: float) -> None:
        async with self._lock:
            self._deposit_to_vault_locked(usdc_to_base(assets_usdc))

    async def withdraw_from_vault(self, assets_usdc: float) -> None:
        async with self._lock:
            self._withdraw_from_vault_locked(usdc_to_base(assets_usdc))

    async def poke_vault(self) -> None:
        """経過分の利回りをオンチェーンで確定させる（デモ表示用）。"""
        async with self._lock:
            self._send_tx(self._vault.functions.poke(), "poke")

    async def sweep_idle(self) -> int:
        """運転資金(min_operating)を残し、余剰USDCをVaultへ預ける。預けた最小単位を返す。"""
        if not self.vault_enabled:
            return 0
        async with self._lock:
            liquid = self.liquid_usdc_base()
            surplus = liquid - self.min_operating_base
            if surplus <= 0:
                self.treasury_audit.append(
                    f"sweep: 余剰なし（流動 ${base_to_usdc(liquid):.4f} ≤ 運転資金 "
                    f"${base_to_usdc(self.min_operating_base):.4f}）"
                )
                return 0
            self._deposit_to_vault_locked(surplus)
            self.treasury_audit.append(f"sweep: ${base_to_usdc(surplus):.4f} を Vault へ")
            return surplus

    def vault_position_base(self) -> int:
        """Vault内の持分を現在価値（USDC最小単位）で返す（直近 poke 時点の確定値）。"""
        if not self.vault_enabled:
            return 0
        sh = int(self._vault.functions.shares(self._account.address).call())
        if sh == 0:
            return 0
        return int(self._vault.functions.convertToAssets(sh).call())

    def vault_position_usdc(self) -> float:
        return base_to_usdc(self.vault_position_base())


# ---------------------------------------------------------------------------
# 自己テスト：成功 / 許可リスト外ブロック / 予算超過ブロック を1度に確認
#   事前に売り手サーバーを起動しておくこと:
#     .venv\Scripts\python.exe -m uvicorn server.shop:app --port 8000
#   実行:
#     .venv\Scripts\python.exe agent\wallet.py
# ---------------------------------------------------------------------------
def _selftest() -> None:
    from rich.console import Console
    from rich.table import Table

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    console = Console()

    ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(ROOT / ".env")
    pk = os.environ["AGENT_PRIVATE_KEY"]
    chain_id = os.environ.get("CHAIN_ID", "84532")
    network = f"eip155:{chain_id}"
    shop_url = os.environ.get("SHOP_URL", "http://127.0.0.1:8000")

    # 予算 $0.05、許可は /search/basic と /fetch のみ
    wallet = Wallet(
        private_key=pk, network=network, budget_usdc=0.05,
        allowlist=["/search/basic", "/fetch"], shop_url=shop_url,
    )
    console.print(
        f"[bold]ウォレット自己テスト[/bold]  予算 ${base_to_usdc(wallet.budget_base):.2f} / "
        f"許可 {sorted(wallet.allowlist)}\n"
    )

    async def run():
        # ① 許可内・予算内 → 成功（$0.02）
        await wallet.buy("/search/basic")
        # ② 許可リスト外 → ネットワークに出ず拒否
        await wallet.buy("/summarize")
        # ③ 許可内だが予算超過（0.02+0.05=0.07 > 0.05）→ 署名前フックが中止
        await wallet.buy("/fetch")

    asyncio.run(run())

    table = Table(title="試行ログ（監査ログ）")
    table.add_column("#", justify="right")
    table.add_column("path", style="cyan")
    table.add_column("結果")
    table.add_column("理由 / メモ")
    for i, r in enumerate(wallet.audit, 1):
        mark = "[green]OK[/green]" if r.ok else "[red]拒否[/red]"
        table.add_row(str(i), r.path, mark, r.reason)
    console.print(table)
    console.print(
        f"\n累計支出 ${base_to_usdc(wallet.spent_base):.2f} / 予算 "
        f"${base_to_usdc(wallet.budget_base):.2f}（残り ${wallet.remaining_usdc:.2f}）"
    )

    # 期待：成功1・拒否2、支出$0.02
    oks = [r for r in wallet.audit if r.ok]
    if len(oks) == 1 and len(wallet.audit) == 3 and wallet.spent_base == usdc_to_base(0.02):
        console.print("[bold green]✅ 3b 検証成功[/bold green]：成功1・許可外ブロック・予算超過ブロックが意図どおり。")
    else:
        console.print("[bold red]❌ 期待と不一致[/bold red]：上のログを確認してください。")
        sys.exit(2)


if __name__ == "__main__":
    _selftest()
