r"""
ベンチ結果の図生成（Phase 5+）

docs/assets/bench_results_*.json（stats_brains.py の出力）を全て読み込んでマージし、
matplotlib で PNG を生成する。ラベルは英語（フォント問題回避）、README に日本語キャプションを付ける。

出力:
  docs/assets/bench_spend.png     … モデル×シナリオ 平均支出（誤差バー=SD）
  docs/assets/bench_behavior.png  … 通常=計画完了率 / 意地悪=誘導追従率

実行: .venv\Scripts\python.exe scripts\plot_bench.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 画面なしで PNG 出力
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"

# 表示順の優先（存在するものだけ使う）
ORDER = [
    "rule",
    "claude-haiku-4-5", "claude-sonnet-4-6",
    "gpt-4o-mini", "gpt-4.1",
    "gemini-2.5-flash", "gemini-2.5-pro",
    "qwen2.5:3b", "llama3.1:8b",
]


def load_rows() -> list[dict]:
    files = sorted(ASSETS.glob("bench_results_*.json"))
    if not files:
        print("結果 JSON が見つかりません。先に stats_brains.py を実行してください。")
        sys.exit(1)
    merged: dict[tuple, dict] = {}
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for r in data.get("results", []):
            key = (r["label"], r["scenario"])
            if key in merged:
                print(f"⚠ 重複: {key} を {f.name} の値で上書き（意図したマージか確認）")
            merged[key] = r  # 後勝ち（label,scenario でユニーク）
    rows = list(merged.values())
    # 応答率が低い（大半が rule に fallback＝モデルの判断ではない）行はラベル単位で図から除外。
    # いずれかのシナリオで <0.9 なら、そのモデルは「計測不能」として全シナリオ除外（正直な欠測扱い）。
    LOW = 0.9
    bad = {r["label"] for r in rows if r["provider"] != "rule" and r.get("llm_rate", 1.0) < LOW}
    if bad:
        print(f"ℹ 計測不能（応答率<{int(LOW*100)}%）のため図から除外: {sorted(bad)}")
    return [r for r in rows if r["label"] not in bad]


def ordered_labels(rows: list[dict]) -> list[str]:
    present = {r["label"] for r in rows}
    labels = [l for l in ORDER if l in present]
    labels += [l for l in sorted(present) if l not in labels]  # ORDER 外も末尾に
    return labels


def get(rows, label, scenario, key, default=0.0):
    for r in rows:
        if r["label"] == label and r["scenario"] == scenario:
            return r.get(key, default)
    return default


def display_labels(rows, labels) -> tuple[list[str], bool]:
    """応答率が低い（レート制限等で大半 fallback の）モデルに '*' を付ける。"""
    disp = []
    any_low = False
    for l in labels:
        rates = [r["llm_rate"] for r in rows if r["label"] == l and r["provider"] != "rule"]
        low = bool(rates) and max(rates) < 0.9
        any_low = any_low or low
        disp.append(l + " *" if low else l)
    return disp, any_low


def _footnote(ax, any_low: bool) -> None:
    if any_low:
        ax.text(0.0, -0.32, "* low LLM response rate (free-tier rate-limited; mostly fell back to rule) = unreliable",
                transform=ax.transAxes, fontsize=8, color="#888888")


def plot_spend(rows, labels, disp, any_low) -> Path:
    x = np.arange(len(labels))
    w = 0.38
    normal = [get(rows, l, "normal", "spend_mean") for l in labels]
    normal_sd = [get(rows, l, "normal", "spend_std") for l in labels]
    adv = [get(rows, l, "adversarial", "spend_mean") for l in labels]
    adv_sd = [get(rows, l, "adversarial", "spend_std") for l in labels]

    fig, ax = plt.subplots(figsize=(11, 5.4))
    ax.bar(x - w / 2, normal, w, yerr=normal_sd, capsize=3, label="normal (budget $0.50)", color="#4C78A8")
    ax.bar(x + w / 2, adv, w, yerr=adv_sd, capsize=3, label="adversarial (budget $0.06)", color="#E45756")
    ax.axhline(0.13, ls="--", lw=1, color="#4C78A8", alpha=0.6)
    ax.axhline(0.06, ls="--", lw=1, color="#E45756", alpha=0.6)
    ax.set_ylabel("Mean spend (USDC)")
    ax.set_title("Autonomous spend by brain (mean ± SD)")
    ax.set_xticks(x)
    ax.set_xticklabels(disp, rotation=30, ha="right")
    ax.legend()
    _footnote(ax, any_low)
    fig.tight_layout()
    out = ASSETS / "bench_spend.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_behavior(rows, labels, disp, any_low) -> Path:
    x = np.arange(len(labels))
    w = 0.38
    completion = [get(rows, l, "normal", "completion_rate") * 100 for l in labels]
    bait = [get(rows, l, "adversarial", "bait_rate") * 100 for l in labels]

    fig, ax = plt.subplots(figsize=(11, 5.4))
    ax.bar(x - w / 2, completion, w, label="plan completion % (normal)", color="#54A24B")
    ax.bar(x + w / 2, bait, w, label="took the bait % (adversarial)", color="#F58518")
    ax.set_ylabel("Rate (%)")
    ax.set_ylim(0, 105)
    ax.set_title("Behavior by brain: completes the plan vs takes the bait")
    ax.set_xticks(x)
    ax.set_xticklabels(disp, rotation=30, ha="right")
    ax.legend()
    _footnote(ax, any_low)
    fig.tight_layout()
    out = ASSETS / "bench_behavior.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def main() -> None:
    rows = load_rows()
    labels = ordered_labels(rows)
    disp, any_low = display_labels(rows, labels)
    p1 = plot_spend(rows, labels, disp, any_low)
    p2 = plot_behavior(rows, labels, disp, any_low)
    print(f"生成: {p1.relative_to(ROOT)}")
    print(f"生成: {p2.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
