r"""
頭脳層（Phase 3c）— 「次に何を買うか」を決める純粋ロジック

このモジュールは **判断だけ** を行う。鍵・署名・ネットワーク・支払いには一切触れない。
入力（目的・残予算・価格表・これまでの状態）から次の行動を1つ返すだけ。
将来ここを LLM 駆動（Phase 5）に差し替えても、wallet/agent 側は変えずに済む（pluggable）。

デモの題材＝「AIリサーチ秘書」。1回の目的に対して
  search（調べる）→ fetch（取ってくる）→ summarize（要約する）
という定型プランを、予算をにらみながら実行する。

判断の肝（ルールベースでも"賢さ"を見せる）:
  - search は、続く fetch+summarize まで賄えるなら高品質な premium、無理なら安い basic を選ぶ
  - どうしても予算が足りない手順が来たら stop（人間が渡した予算を超えない）
  - 全手順を終えたら stop（目的達成）
"""
from __future__ import annotations

from dataclasses import dataclass, field


# このデモでの「能力（capability）」と、それを満たす売り手パスの対応
SEARCH_BASIC = "/search/basic"
SEARCH_PREMIUM = "/search/premium"
FETCH = "/fetch"
SUMMARIZE = "/summarize"

# 定型プラン（能力名の順序）
PLAN = ["search", "fetch", "summarize"]


@dataclass
class Action:
    """頭脳の出力。買うか止まるか、だけ。"""
    type: str                       # "buy" | "stop"
    capability: str | None = None   # この行動が満たす能力（done 更新用）
    path: str | None = None         # 買うなら売り手パス
    params: dict = field(default_factory=dict)  # クエリ等
    reason: str = ""                # なぜそうしたか（ログ・学習用）


def rule_based_brain(
    goal: str,
    remaining_usdc: float,
    price_list: dict[str, float],
    state: dict,
) -> Action:
    """次の行動を1つ返す。state["done"] は完了済み能力名の集合。

    price_list: 例 {"/search/basic":0.02, "/search/premium":0.05, "/fetch":0.05, "/summarize":0.03}

    state["done"]    … 実際に購入できた能力（成功）
    state["skipped"] … 失敗/ブロックで諦めた能力（許可外・決済失敗など）
    どちらも「解決済み」として再提案しない。最後に skipped があれば「部分完了」と正直に報告する。
    """
    done: set[str] = state.get("done", set())
    skipped: set[str] = state.get("skipped", set())
    resolved = done | skipped

    for cap in PLAN:
        if cap in resolved:
            continue

        if cap == "search":
            premium = price_list.get(SEARCH_PREMIUM)
            basic = price_list.get(SEARCH_BASIC)
            # 続く手順（fetch+summarize）の概算
            rest = price_list.get(FETCH, 0.0) + price_list.get(SUMMARIZE, 0.0)

            if premium is not None and remaining_usdc >= premium + rest:
                return Action(
                    "buy", capability="search", path=SEARCH_PREMIUM,
                    params={"q": goal},
                    reason=f"予算に余裕あり（残${remaining_usdc:.2f} ≥ ${premium+rest:.2f}）→ 高品質な premium を選択",
                )
            if basic is not None and remaining_usdc >= basic:
                return Action(
                    "buy", capability="search", path=SEARCH_BASIC,
                    params={"q": goal},
                    reason=f"予算優先（残${remaining_usdc:.2f}）→ 安い basic を選択",
                )
            return Action("stop", reason=f"検索の予算が不足（残${remaining_usdc:.2f}）")

        # fetch / summarize：単純に1つの売り手パスに対応
        path = FETCH if cap == "fetch" else SUMMARIZE
        price = price_list.get(path)
        if price is not None and remaining_usdc >= price:
            return Action(
                "buy", capability=cap, path=path, params={},
                reason=f"{cap} を実行（${price:.2f}、残${remaining_usdc:.2f}）",
            )
        return Action("stop", reason=f"{cap} の予算が不足（必要${price}、残${remaining_usdc:.2f}）で停止")

    # 全能力が解決済み。skipped があれば「部分完了」と正直に区別する
    if skipped:
        return Action("stop", reason=f"停止：{sorted(skipped)} を達成できず（部分完了）")
    return Action("stop", reason="目的達成：計画の全手順を完了")


# ---------------------------------------------------------------------------
# 自己テスト：ネットワーク不要（純粋ロジックなので価格表と状態を渡すだけ）
#   実行: .venv\Scripts\python.exe agent\brain.py
# ---------------------------------------------------------------------------
def _selftest() -> None:
    import sys
    from rich.console import Console
    from rich.table import Table

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    console = Console()

    PRICES = {SEARCH_BASIC: 0.02, SEARCH_PREMIUM: 0.05, FETCH: 0.05, SUMMARIZE: 0.03}
    goal = "AI×ステーブルコインの最新動向"

    # (説明, 残予算, done, 期待する type, 期待する path)
    cases = [
        ("潤沢な予算（premiumを選ぶ）", 1.00, set(), "buy", SEARCH_PREMIUM),
        ("検索しかできない予算（basicを選ぶ）", 0.04, set(), "buy", SEARCH_BASIC),
        ("検索すら無理（停止）", 0.01, set(), "stop", None),
        ("検索済み→次はfetch", 0.10, {"search"}, "buy", FETCH),
        ("検索+取得済み→次はsummarize", 0.10, {"search", "fetch"}, "buy", SUMMARIZE),
        ("fetchの予算不足→停止", 0.01, {"search"}, "stop", None),
        ("全完了→停止（目的達成）", 1.00, {"search", "fetch", "summarize"}, "stop", None),
    ]

    table = Table(title="頭脳ロジック検証（ネットワーク不要）")
    table.add_column("ケース", style="cyan")
    table.add_column("残予算", justify="right")
    table.add_column("done", style="dim")
    table.add_column("→ 行動")
    table.add_column("判定")

    all_ok = True
    for desc, rem, done, exp_type, exp_path in cases:
        act = rule_based_brain(goal, rem, PRICES, {"done": set(done)})
        ok = (act.type == exp_type) and (act.path == exp_path)
        all_ok = all_ok and ok
        action_str = act.type + (f" {act.path}" if act.path else "")
        mark = "[green]OK[/green]" if ok else "[red]NG[/red]"
        table.add_row(desc, f"${rem:.2f}", "{" + ",".join(sorted(done)) + "}", action_str, mark)

    console.print(table)
    if all_ok:
        console.print("\n[bold green]✅ 3c 検証成功[/bold green]：予算に応じた品質選択・予算切れ停止・目的達成停止が意図どおり。")
    else:
        console.print("\n[bold red]❌ 期待と不一致[/bold red]：上の判定を確認してください。")
        sys.exit(2)


if __name__ == "__main__":
    _selftest()
