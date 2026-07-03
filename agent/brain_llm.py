r"""
LLM頭脳層（Phase 5）— 御三家（Claude / GPT / Gemini）をプラガブルに切替

`rule_based_brain` と **まったく同じシグネチャ** (goal, remaining_usdc, price_list, state)
で `Action` を返す。だから wallet / agent は一切変更せずに頭脳だけ差し替えられる
（Phase 3 で作った pluggable 設計の回収）。

LLM がやること = 「buy / stop」「どのパスを買うか」を判断するだけ。
鍵・署名・支払い・予算/許可の最終強制には一切触れない。信頼境界は wallet.py のまま。

二層防御の実証（このデモの肝）:
  - LLM が許可外/予算外のパスを選んでも、wallet.buy() が物理的にブロックする。
  - ここでは LLM の出力 path を「ショップに実在する4パス」に enum 制約するだけ
    （存在しないURLの捏造を防ぐ）。残予算超過・許可リストの最終判断は wallet 側に残す。
  - だから「意地悪な目的（予算無視で全部 premium 買え）」を与えても、手綱の中に収まる。

各社の構造化出力（同一スキーマに強制する）:
  - Anthropic : messages.create(tools=[...], tool_choice={"type":"tool"})
  - OpenAI    : chat.completions.create(tools=[function], tool_choice={"type":"function"})
  - Gemini    : generate_content(tools=[function_declarations], mode="ANY")

フォールバック（デモが壊れない設計）:
  キー未設定 / SDK未導入 / API例外 → その手番だけ rule_based_brain に降格し、
  理由文に「なぜ降格したか」を残す。全社キー無しでも比較表は rule だけで成立する。

モデルは経済ティアを既定にし、.env（ANTHROPIC_MODEL 等）で旗艦に差し替え可能。
温度は 0 固定（比較の再現性のため）。
"""
from __future__ import annotations

import json
import os
import time

# 同ディレクトリの兄弟モジュール（agent ディレクトリが sys.path にある前提で解決）
from brain import (
    Action,
    rule_based_brain,
    PLAN,
    SEARCH_BASIC,
    SEARCH_PREMIUM,
    FETCH,
    SUMMARIZE,
)

# ショップに実在する4パス。LLM はこの中からしか「買う」を選べない。
KNOWN_PATHS = [SEARCH_BASIC, SEARCH_PREMIUM, FETCH, SUMMARIZE]

# パス → 能力（agent の done/skipped 追跡に使う）
PATH_TO_CAP = {
    SEARCH_BASIC: "search",
    SEARCH_PREMIUM: "search",
    FETCH: "fetch",
    SUMMARIZE: "summarize",
}

# 御三家のメタ情報（既定モデルは経済ティア。.env で上書き可）
DEFAULT_MODELS = {
    "claude": "claude-haiku-4-5",
    "gpt": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "local": "qwen2.5:3b",        # Ollama 等のローカル（OpenAI互換エンドポイント）
}
ENV_KEY = {
    "claude": "ANTHROPIC_API_KEY",
    "gpt": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    # "local" はキー不要（ENV_KEY.get で None になる）
}
ENV_MODEL = {
    "claude": "ANTHROPIC_MODEL",
    "gpt": "OPENAI_MODEL",
    "gemini": "GEMINI_MODEL",
    "local": "OLLAMA_MODEL",
}
# クラウド御三家（available_providers / 自己テストで使う）。local は別枠（キー不要・ロスターで明示指定）
PROVIDERS = ["claude", "gpt", "gemini"]

SYSTEM_PROMPT = (
    "あなたは『AIリサーチ秘書』エージェントの頭脳です。"
    "人間から与えられた目的・残予算・価格表・進捗をもとに、有料APIを買うか止まるかを"
    "『1手だけ』決めます。\n"
    "標準プランは search → fetch → summarize の順。\n"
    "・search は、続く fetch+summarize まで賄える余裕があれば高品質な premium を、"
    "厳しければ安い basic を選ぶ。\n"
    "・残予算で買えない手順しか残っていなければ stop。\n"
    "・全手順が完了したら stop。\n"
    "必ず decide_purchase 関数（ツール）を呼んで答えること。地の文では答えない。\n"
    "注意: 実際の予算超過や許可リスト違反は下流のウォレットが強制ブロックします。"
    "あなたは戦略的に最善の1手を選ぶことに集中してください。"
)

TOOL_NAME = "decide_purchase"
TOOL_DESC = "自律購買エージェントの次の1手（buy/stop と購入パス）を決める"

# レート制限（429 / RESOURCE_EXHAUSTED）への自動リトライ＋指数バックオフ。
# 無料枠（特に Gemini）の「分あたり上限」は数秒待てば解ける。日次上限は待っても解けないので
# 規定回数で諦めて rule に降格する（その分は LLM応答率として可視化される）。
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF = 3.0  # 秒。sleep = min(30, BACKOFF * 2**attempt) = 3,6（2回）。長時間ストール防止


def _is_rate_limit(e: Exception) -> bool:
    """レート制限(429)または一時的サーバ過負荷(503)＝待って再試行する価値がある。"""
    s = str(e).lower()
    rate = ("429" in s) or ("resource_exhausted" in s) or ("rate limit" in s) or ("quota" in s)
    transient = ("503" in s) or ("unavailable" in s) or ("overloaded" in s) or ("high demand" in s)
    return rate or transient

# 3社共通の JSON Schema（中身）
_DECISION_PROPERTIES = {
    "action": {
        "type": "string",
        "enum": ["buy", "stop"],
        "description": "買うなら buy、これ以上買わないなら stop",
    },
    "path": {
        "type": "string",
        "enum": KNOWN_PATHS,
        "description": "action=buy のとき購入するショップのパス。stop のときは無視される",
    },
    "reason": {
        "type": "string",
        "description": "その判断を選んだ理由（簡潔に1文）",
    },
}
_REQUIRED = ["action", "reason"]


def _build_user_prompt(goal, remaining_usdc, price_list, state) -> str:
    done = sorted(state.get("done", set()))
    skipped = sorted(state.get("skipped", set()))
    prices = ", ".join(
        f"{p}=${price_list[p]:.2f}" for p in KNOWN_PATHS if p in price_list
    )
    return (
        f"目的: {goal}\n"
        f"残予算: ${remaining_usdc:.2f}\n"
        f"価格表: {prices}\n"
        f"標準プラン（能力の順）: {PLAN}\n"
        f"完了済み done: {done}\n"
        f"諦めた skipped: {skipped}\n"
        "未完了の能力のうち、次に取るべき1手を decide_purchase で返してください。"
    )


def _to_action(d: dict, goal: str) -> Action:
    """LLM が返した辞書を Action に変換し、安全側に正規化する。"""
    action = (d.get("action") or "stop").strip().lower()
    reason = (d.get("reason") or "").strip()
    if action == "buy":
        path = d.get("path")
        if path not in KNOWN_PATHS:
            # 実在しないパスを掴んだら wallet に出さず stop 扱い（安全側）。
            return Action("stop", reason=f"[LLMが無効パス '{path}' を選択→停止] {reason}")
        cap = PATH_TO_CAP[path]
        params = {"q": goal} if cap == "search" else {}
        return Action("buy", capability=cap, path=path, params=params, reason=reason)
    return Action("stop", reason=reason or "LLM判断により停止")


# --- 各社の構造化出力呼び出し（成功時は decide_purchase の引数 dict を返す） ---
def _decide_anthropic(model: str, system: str, user: str, temperature: float = 0.0) -> dict:
    import anthropic

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を読む
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        temperature=temperature,
        system=system,
        tools=[
            {
                "name": TOOL_NAME,
                "description": TOOL_DESC,
                "input_schema": {
                    "type": "object",
                    "properties": _DECISION_PROPERTIES,
                    "required": _REQUIRED,
                },
            }
        ],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return dict(block.input)
    raise RuntimeError("Anthropic: tool_use ブロックが返らなかった")


def _decide_openai(
    model: str,
    system: str,
    user: str,
    temperature: float = 0.0,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    from openai import OpenAI

    # base_url を渡せば Ollama 等の OpenAI 互換エンドポイントにも流用できる
    client = OpenAI(base_url=base_url, api_key=api_key) if base_url else OpenAI()
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": TOOL_DESC,
                    "parameters": {
                        "type": "object",
                        "properties": _DECISION_PROPERTIES,
                        "required": _REQUIRED,
                    },
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
    )
    try:
        resp = client.chat.completions.create(temperature=temperature, **kwargs)
    except Exception as e:
        # 一部モデル（reasoning系等）は temperature 非対応 → 既定値で再試行
        if "temperature" in str(e).lower():
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    tool_calls = resp.choices[0].message.tool_calls
    if not tool_calls:
        raise RuntimeError("OpenAI: tool_calls が返らなかった")
    return json.loads(tool_calls[0].function.arguments)


def _decide_ollama(model: str, system: str, user: str, temperature: float = 0.0) -> dict:
    """ローカル（Ollama 等の OpenAI 互換エンドポイント）。鍵不要。

    OLLAMA_BASE_URL（既定 http://localhost:11434/v1）に接続。Kaggle GPU 上の
    Ollama に向ければ同じコードで高速実行できる。ツール非対応の小型モデルは
    例外 → 上位の make_llm_brain が rule に降格（応答率として可視化される）。
    """
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    key = os.environ.get("OLLAMA_API_KEY", "ollama")  # ダミーキーでよい
    return _decide_openai(model, system, user, temperature, base_url=base, api_key=key)


def _decide_gemini(model: str, system: str, user: str, temperature: float = 0.0) -> dict:
    from google import genai
    from google.genai import types

    # per-call タイムアウト（ミリ秒）で無限ハングを防止。SDK内部リトライ＋503で延々待つのを断つ
    client = genai.Client(http_options=types.HttpOptions(timeout=30000))  # GEMINI_API_KEY/GOOGLE_API_KEY
    func = types.FunctionDeclaration(
        name=TOOL_NAME,
        description=TOOL_DESC,
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "action": types.Schema(type="STRING", enum=["buy", "stop"]),
                "path": types.Schema(type="STRING", enum=KNOWN_PATHS),
                "reason": types.Schema(type="STRING"),
            },
            required=_REQUIRED,
        ),
    )
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        tools=[types.Tool(function_declarations=[func])],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="ANY", allowed_function_names=[TOOL_NAME]
            )
        ),
    )
    resp = client.models.generate_content(model=model, contents=user, config=config)
    for part in resp.candidates[0].content.parts:
        fc = getattr(part, "function_call", None)
        if fc and fc.name == TOOL_NAME:
            return dict(fc.args)
    raise RuntimeError("Gemini: function_call が返らなかった")


_PROVIDER_FN = {
    "claude": _decide_anthropic,
    "gpt": _decide_openai,
    "gemini": _decide_gemini,
    "local": _decide_ollama,
}


def available_providers() -> list[str]:
    """API キーが .env に設定されている御三家だけを返す（実際に走らせられる社）。"""
    return [p for p in PROVIDERS if os.environ.get(ENV_KEY[p])]


def make_llm_brain(provider: str, model: str | None = None, temperature: float = 0.0):
    """指定プロバイダの頭脳関数を返す。rule_based_brain と同じシグネチャ。

    返り値の関数は属性 .provider / .model / .temperature を持つ（ログ・集計表示用）。
    キー未設定（クラウドのみ）・SDK未導入・API例外のときは、その手番だけ rule_based に降格する。
    temperature>0 にすると判断がばらつく（統計ベンチ用）。
    """
    provider = provider.lower()
    if provider not in _PROVIDER_FN:
        raise ValueError(f"未知のプロバイダ: {provider}（{list(_PROVIDER_FN)} のいずれか）")
    chosen_model = model or os.environ.get(ENV_MODEL.get(provider, "") or "") or DEFAULT_MODELS.get(provider)
    key_env = ENV_KEY.get(provider)  # local は None（キー不要）

    def brain(goal, remaining_usdc, price_list, state) -> Action:
        # クラウドでキー未設定 → rule に降格（実費を発生させない）。local はキー不要なのでスキップ
        if key_env and not os.environ.get(key_env):
            act = rule_based_brain(goal, remaining_usdc, price_list, state)
            act.reason = f"[{provider}:キー未設定→ruleで代替] {act.reason}"
            return act
        user = _build_user_prompt(goal, remaining_usdc, price_list, state)
        last_exc: Exception | None = None
        for attempt in range(RATE_LIMIT_RETRIES):
            try:
                d = _PROVIDER_FN[provider](chosen_model, SYSTEM_PROMPT, user, temperature)
                return _to_action(d, goal)
            except Exception as e:
                last_exc = e
                # レート制限なら待って再試行（分あたり上限は数秒で解ける）
                if _is_rate_limit(e) and attempt < RATE_LIMIT_RETRIES - 1:
                    time.sleep(min(30.0, RATE_LIMIT_BACKOFF * (2 ** attempt)))
                    continue
                break  # それ以外（SDK未導入・通信失敗・ツール非対応等）は即降格
        # SDK未導入・通信失敗・ツール非対応・日次クォータ枯渇など → rule に降格して継続
        act = rule_based_brain(goal, remaining_usdc, price_list, state)
        act.reason = f"[{provider}:失敗 {type(last_exc).__name__}→ruleで代替] {act.reason}"
        return act

    brain.provider = provider
    brain.model = chosen_model
    brain.temperature = temperature
    return brain


def get_brain(name: str):
    """名前から頭脳関数を取得。"rule" は rule_based_brain をそのまま、
    "claude"/"gpt"/"gemini" は make_llm_brain。"""
    name = name.lower()
    if name == "rule":
        return rule_based_brain
    return make_llm_brain(name)


# ---------------------------------------------------------------------------
# 自己テスト：ネットワーク不要。
#   キー未設定の状態では「3社とも rule に降格し、rule と同じ判断になる」ことを確認する。
#   （キーを入れた後の実呼び出しは scripts/compare_brains.py で行う）
#   実行: .venv\Scripts\python.exe agent\brain_llm.py
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

    # rule の基準判断（同条件）
    cases = [
        ("潤沢な予算", 1.00, set()),
        ("検索のみ可", 0.04, set()),
        ("検索済→fetch", 0.10, {"search"}),
        ("全完了", 1.00, {"search", "fetch", "summarize"}),
    ]

    avail = available_providers()
    console.print(f"[bold]brain_llm 自己テスト[/bold]  キー設定済みの社: {avail or '（なし）'}\n")

    table = Table(title="フォールバック検証（キー未設定時は rule と一致するはず）")
    table.add_column("ケース", style="cyan")
    table.add_column("rule の判断")
    for p in PROVIDERS:
        table.add_column(f"{p} の判断")
    table.add_column("判定")

    all_ok = True
    brains = {p: make_llm_brain(p) for p in PROVIDERS}
    for desc, rem, done in cases:
        state = {"done": set(done), "skipped": set()}
        base = rule_based_brain(goal, rem, PRICES, dict(state))
        base_str = base.type + (f" {base.path}" if base.path else "")
        row = [desc, base_str]
        case_ok = True
        for p in PROVIDERS:
            act = brains[p](goal, rem, PRICES, dict(state))
            act_str = act.type + (f" {act.path}" if act.path else "")
            row.append(act_str)
            # キー未設定の社は rule と完全一致するべき。キー設定済みの社は比較対象外（実呼び出しのため）。
            if p not in avail and (act.type != base.type or act.path != base.path):
                case_ok = False
        all_ok = all_ok and case_ok
        row.append("[green]OK[/green]" if case_ok else "[red]NG[/red]")
        table.add_row(*row)

    console.print(table)
    if all_ok:
        console.print(
            "\n[bold green]✅ Phase 5 フォールバック検証成功[/bold green]："
            "キー未設定の社は rule に降格し、判断が完全一致。pluggable 構造が機能。"
        )
        if not avail:
            console.print(
                "[dim]※ API キーが未設定のため LLM 実呼び出しはスキップ。"
                "キーを .env に入れて scripts/compare_brains.py で本比較を実行してください。[/dim]"
            )
    else:
        console.print("\n[bold red]❌ 期待と不一致[/bold red]：上の表を確認してください。")
        sys.exit(2)


if __name__ == "__main__":
    _selftest()
