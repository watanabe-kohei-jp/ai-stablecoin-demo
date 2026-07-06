r"""
流動性の階段 通しデモ：SC ⇄ 預金Vault（即時） ⇄ ST（高利回り・償還ラグ）
=======================================================================
ストーリー（＝流動性ミスマッチの実演）:
  ① 余剰USDCを3層へスイープ（即時層には少額・大半は高利回りのST=遅延層へ）
  ② 買い物1・2 → 手元不足 → wallet が預金Vaultから自動JIT償還して決済成功
  ③ 即時層が枯渇した状態で3つ目の買い物
      → ST には資金があるのに「償還ラグ」で間に合わない
      → wallet は支払いを【見送る】（= 24/7の約束 vs 原資産T+N の流動性ミスマッチ）
  ④ wallet が前もって償還を予約（plan_liquidity）→ ラグ経過 → 受取（claim）
  ⑤ 再挑戦 → 決済成功
  判断(brain)は無関与。資金配置・流動性計画はすべて wallet（信頼境界）の仕事。

前提: 売り手サーバー起動 + .env に DEPOSIT_VAULT_ADDRESS / ST_ADDRESS + 少量のガス(ETH)。
  別ターミナル:  .venv\Scripts\python.exe -m uvicorn server.shop:app --port 8000
  実行:          .venv\Scripts\python.exe scripts\ladder_demo.py
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

MIN_OPERATING = 0.01     # 手元（SC層）に常に残す運転資金
DEP_TARGET = 0.065       # 即時層（預金Vault）に置く額：買い物2回でほぼ枯渇する少額
BUYS_JIT = ["/search/basic", "/summarize"]   # $0.02 + $0.03 → 即時層からJITできる
BUY_BIG = "/fetch"                            # $0.05 → 即時層枯渇後＝ミスマッチを踏む
EXPECTED_SPEND = 0.10                         # 0.02 + 0.03 + 0.05


def _stable_read(read_fn, tries: int = 8, wait: float = 3.0) -> int:
    """公開RPCの read-after-write 遅延対策：2回連続で同じ値になるまで待って返す。
    （表示用。判定は in-memory の確実な値で行うので、ここは見た目の正確さのため）"""
    import time as _t
    prev = read_fn()
    for _ in range(tries):
        _t.sleep(wait)
        cur = read_fn()
        if cur == prev:
            return cur
        prev = cur
    return prev


def _row(table, label, w):
    liquid = _stable_read(w.liquid_usdc_base)
    dep = _stable_read(w.vault_position_base)
    st = _stable_read(w.st_position_base)
    table.add_row(
        label,
        f"{W.base_to_usdc(liquid):.4f}",
        f"{W.base_to_usdc(dep):.4f}",
        f"{W.base_to_usdc(st):.4f}",
    )


async def run() -> None:
    w = W.Wallet(
        private_key=os.environ["AGENT_PRIVATE_KEY"],
        network=f"eip155:{os.environ.get('CHAIN_ID', '84532')}",
        budget_usdc=0.12,
        allowlist=BUYS_JIT + [BUY_BIG],
        shop_url=os.environ.get("SHOP_URL", "http://127.0.0.1:8000"),
        rpc_url=os.environ["RPC_URL"],
        usdc_address=os.environ["USDC_ADDRESS"],
        vault_address=os.environ["DEPOSIT_VAULT_ADDRESS"],   # 即時層＝模擬トークン化預金
        st_address=os.environ["ST_ADDRESS"],                 # 遅延層＝模擬ST
        min_operating_usdc=MIN_OPERATING,
    )

    table = Table(title="流動性の階段 通しデモ（USDC）")
    table.add_column("段階", style="cyan")
    table.add_column("SC(流動)", justify="right", style="green")
    table.add_column("預金Vault(即時)", justify="right", style="yellow")
    table.add_column("ST(遅延)", justify="right", style="magenta")

    liquid0 = w.liquid_usdc_base()
    _row(table, "開始", w)

    # ① スイープ：即時層には DEP_TARGET だけ、残りの余剰は全部 ST（高利回り）へ。
    #    ※配分比はデモを決定的にするため実行時に計算（st_alloc = 1 - DEP_TARGET/余剰）
    surplus0 = liquid0 - w.min_operating_base
    if surplus0 <= W.usdc_to_base(DEP_TARGET) + W.usdc_to_base(0.05):
        console.print("[red]流動USDCが少なすぎます（最低 $0.2 程度必要）。[/red]")
        sys.exit(2)
    w.st_alloc = 1.0 - (W.usdc_to_base(DEP_TARGET) / surplus0)
    console.print(f"[bold]① 3層へスイープ[/bold]（即時層 ≈ ${DEP_TARGET}、残りはST・配分比 {w.st_alloc:.3f}）…")
    await w.sweep_idle()
    _row(table, "sweep後", w)

    # ② 買い物1・2：手元 < 価格 → 預金Vault（即時層）から自動JIT償還して決済
    console.print("[bold]② 買い物1・2 → 即時層からJIT償還で決済[/bold]")
    results = []
    for path in BUYS_JIT:
        console.print(f"   buy {path} …")
        r = await w.buy(path)
        results.append(r)
        mark = "[green]OK[/green]" if r.ok else "[red]NG[/red]"
        console.print(f"     {mark} {r.reason}")
    _row(table, "買い物1・2後", w)

    # ③ 即時層がほぼ枯渇した状態で $0.05 の買い物
    #    → STには十分な資金があるが「償還ラグ」で即時には出せない → 見送り
    console.print(f"[bold]③ 買い物3（{BUY_BIG} / $0.05）→ 即時層は枯渇・STはラグ中[/bold]")
    r_miss = await w.buy(BUY_BIG)
    mark = "[green]OK[/green]" if r_miss.ok else "[red]見送り[/red]"
    console.print(f"     {mark} {r_miss.reason}")
    _row(table, "ミスマッチ発生", w)

    # ④ 流動性計画：STの償還を予約 → ラグ経過を待って受取
    console.print("[bold]④ wallet が償還を予約（plan_liquidity）→ ラグ経過 → 受取（claim）[/bold]")
    req = await w.plan_liquidity(0.05)
    delay = int(os.environ.get("ST_REDEEM_DELAY_SEC", "120"))
    console.print(f"   予約 id={req['id']} / 受取可能まで最長 {delay} 秒待機…")
    await w.claim_st(req["id"])
    _row(table, "ST償還 受取後", w)

    # ⑤ 再挑戦 → 今度は流動性が足りるので決済成功
    console.print(f"[bold]⑤ 再挑戦 buy {BUY_BIG} …[/bold]")
    r_retry = await w.buy(BUY_BIG)
    mark = "[green]OK[/green]" if r_retry.ok else "[red]NG[/red]"
    console.print(f"     {mark} {r_retry.reason}")
    _row(table, "再挑戦後", w)

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

    # --- 後片付け：残高を可能な範囲で手元へ戻す（再実行用・best-effort） ---
    try:
        pos = w.vault_position_base()
        if pos > 0:
            console.print("\n[dim]後片付け：預金Vault残高を全額償還…[/dim]")
            await w.withdraw_from_vault(W.base_to_usdc(pos))
        st_pos = w.st_position_base()
        if st_pos > 0:
            console.print(f"[dim]後片付け：ST残高 ${W.base_to_usdc(st_pos):.4f} の償還を予約→受取（{int(os.environ.get('ST_REDEEM_DELAY_SEC', '120'))}秒待ち）…[/dim]")
            req2 = await w.plan_liquidity(W.base_to_usdc(st_pos))
            await w.claim_st(req2["id"])
    except Exception as e:
        console.print(f"[dim]（後片付けはスキップ：{type(e).__name__}: {e}）[/dim]")

    # --- 期待値 assert（in-memory の確実な値で判定） ---
    ok = True
    if not all(r.ok for r in results):
        console.print("[red]NG: JIT購入（1・2）に失敗がある[/red]"); ok = False
    if r_miss.ok or "流動性不足で見送り" not in r_miss.reason:
        console.print("[red]NG: 流動性ミスマッチ（見送り）が発生していない[/red]"); ok = False
    if not r_retry.ok:
        console.print("[red]NG: 償還受取後の再挑戦が失敗[/red]"); ok = False
    if w.spent_base != W.usdc_to_base(EXPECTED_SPEND):
        console.print(f"[red]NG: 支出が想定外 ${W.base_to_usdc(w.spent_base):.4f} != ${EXPECTED_SPEND}[/red]"); ok = False
    need_logs = ["ST（遅延層）へ", "預金Vault（即時層）へ", "JIT償還", "流動性計画", "ST償還を受取"]
    for key in need_logs:
        if not any(key in line for line in w.treasury_audit):
            console.print(f"[red]NG: 財務監査ログに「{key}」が無い[/red]"); ok = False

    if ok:
        console.print(
            "\n[bold green]✅ 流動性の階段 検証成功[/bold green]：3層スイープ→即時層からJIT→"
            "即時層枯渇でSTは間に合わず見送り（流動性ミスマッチの実演）→償還予約→ラグ後に受取→再挑戦成功。"
            "判断(brain)は無関与、すべて wallet（信頼境界）が物理的に担保した。"
        )
    else:
        console.print("\n[bold red]❌ 期待と不一致[/bold red]：上のログを確認。")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(run())
