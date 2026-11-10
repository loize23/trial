"""
llm.py -- where an LLM actually belongs in a trading system.

READ THIS FIRST
---------------
An LLM should never be the thing that decides and fires your orders:
  * it is non-deterministic (same input -> different output -> unbacktestable)
  * it hallucinates prices, indicators and news
  * it has no concept of position sizing or ruin probability
  * latency + token cost per candle is a terrible trade loop
  * every "ChatGPT trading bot" backtest you see online is either look-ahead biased
    or curve-fitted, and the ones that print money are selling you something

Where an LLM genuinely helps:
  1. RESEARCH / BRIEFING  -- summarise the market state each morning in plain language
  2. TRADE JOURNALING     -- read your trade CSV and find your behavioural patterns
  3. CODE REVIEW          -- audit your strategy for look-ahead bias and bugs
  4. REGIME NARRATION     -- explain *why* the bot is sitting in cash this week

This module does 1 and 2. It returns TEXT. It has no access to your broker object and
no ability to place an order. That is intentional and you should keep it that way.
"""

import json
import os
import urllib.request
from typing import Optional


def _chat(base_url: str, api_key: str, model: str, system: str, user: str, temperature: float = 0.2) -> Optional[str]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM call failed: {e}]"


SYSTEM = (
    "You are a risk-first crypto research assistant for a retail trader in the Philippines. "
    "You NEVER give financial advice, never tell the user to buy or sell, and never claim to predict price. "
    "You explain market structure, point out risk, and flag when the user's own rules are being violated. "
    "If data is missing, say it is missing instead of inventing numbers. Be concise and concrete."
)


def market_briefing(cfg: dict, state_summary: dict, indicator_table: dict) -> str:
    """Morning briefing: what the bot sees, in plain English."""
    api_key = os.environ.get(cfg.get("llm", {}).get("api_key_env", "LLM_API_KEY"), "")
    if not api_key:
        return "[LLM disabled: set the env var named in llm.api_key_env (default LLM_API_KEY)]"
    user = (
        "Here is my automated trading bot's current state.\n\n"
        f"PORTFOLIO: {json.dumps(state_summary, indent=2)}\n\n"
        f"INDICATORS PER SYMBOL: {json.dumps(indicator_table, indent=2)}\n\n"
        "Write a short briefing (max 200 words) with these sections:\n"
        "1) Regime -- is each symbol trending, ranging, or extended?\n"
        "2) Risk flags -- drawdown, consecutive losses, concentration, anything near a limit.\n"
        "3) One question I should answer before touching anything.\n"
        "Do not recommend trades."
    )
    return _chat(cfg["llm"].get("base_url", "https://api.openai.com/v1"), api_key,
                 cfg["llm"].get("model", "gpt-4o-mini"), SYSTEM, user)


def journal_review(cfg: dict, trades_csv_text: str, equity_curve: str) -> str:
    """Weekly review of your own trading behaviour."""
    api_key = os.environ.get(cfg.get("llm", {}).get("api_key_env", "LLM_API_KEY"), "")
    if not api_key:
        return "[LLM disabled: set the env var named in llm.api_key_env (default LLM_API_KEY)]"
    user = (
        "These are my bot's executed trades and equity curve.\n\n"
        f"TRADES (CSV):\n{trades_csv_text[:12000]}\n\n"
        f"EQUITY:\n{equity_curve[:3000]}\n\n"
        "Analyse my process, not my luck. Report: win rate, average win vs average loss, "
        "whether stops are being respected, holding-time patterns, and the single biggest "
        "structural weakness. Then give me 3 concrete rule changes to test in the backtester. "
        "Be blunt."
    )
    return _chat(cfg["llm"].get("base_url", "https://api.openai.com/v1"), api_key,
                 cfg["llm"].get("model", "gpt-4o-mini"), SYSTEM, user, temperature=0.1)


def code_audit(cfg: dict, source_code: str) -> str:
    """Point an LLM at your strategy file to hunt for look-ahead bias. Cheap and useful."""
    api_key = os.environ.get(cfg.get("llm", {}).get("api_key_env", "LLM_API_KEY"), "")
    if not api_key:
        return "[LLM disabled: set the env var named in llm.api_key_env (default LLM_API_KEY)]"
    user = (
        "Audit this trading code for: look-ahead bias (using data not available at decision time), "
        "survivorship bias, off-by-one errors in indicator windows, unhandled exceptions mid-cycle, "
        "and any path where an order can be placed without a stop loss.\n\n"
        f"```python\n{source_code[:20000]}\n```"
    )
    return _chat(cfg["llm"].get("base_url", "https://api.openai.com/v1"), api_key,
                 cfg["llm"].get("model", "gpt-4o-mini"), SYSTEM, user, temperature=0.0)
