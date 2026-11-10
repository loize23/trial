# ph-crypto-agent

A guarded, **paper-first** crypto trading agent starter kit for someone in the Philippines.
Pure Python standard library — no dependencies for paper trading and backtesting.

**Read `GUIDE.md` first.** It covers PH regulation (SEC CASP rules, which exchanges are
flagged), the three build routes, security hardening, scams, and a 30-day plan.

## Quick start (no account, no money, 2 minutes)

```bash
python3 run.py selftest          # verify indicators + every risk guardrail
python3 run.py backtest          # strategy vs buy & hold on real historical candles
python3 run.py cycle             # ONE paper-trading cycle against live prices
python3 run.py status            # your simulated portfolio
python3 run.py loop              # run continuously (Ctrl-C to stop)
python3 run.py reset             # start the paper account over
```

Customise by copying the config:

```bash
cp config/config.example.json config/config.json
```

## Commands

| Command | What it does |
|---|---|
| `selftest` | Offline checks: SMA/EMA/RSI/ATR correctness + every risk rule + kill switch |
| `backtest [--limit N] [--symbols BTC/USDT,...] [--timeframe 4h]` | No-lookahead backtest on real candles; writes `data/backtest_last.{json,txt}` |
| `cycle` | One live-price paper cycle |
| `loop [--cycles N]` | Continuous loop, interval from `loop_seconds` |
| `status` | JSON dump of equity, positions, unrealised PnL, risk state |
| `brief` | LLM market briefing (needs `LLM_API_KEY`; research only, cannot trade) |
| `journal` | LLM review of `data/trades.csv` — critiques your process, not your luck |
| `reset` | Wipe paper state back to `starting_balance` |

## Safety model

- **Paper mode is the default.** Live mode needs `"mode": "live"` **and**
  `I_UNDERSTAND_I_CAN_LOSE_MONEY=yes` **and** exchange API keys.
- The risk layer (`agent/risk.py`) sits between strategy and broker and can only
  **reject** or **shrink** an order. Never enlarge one.
- No stop loss → no trade. Max 3 positions. Daily loss limit. Drawdown halt.
  Consecutive-loss halt. `touch KILL` = emergency stop.
- Fees and slippage are modelled in both paper trading and backtesting.
- Backtester signals on bar close and fills on the **next bar's open** (no look-ahead).

## Going live

1. Paper trade for weeks. Compare live-paper results against your backtest.
2. Create an API key with **withdrawals DISABLED** and an **IP allowlist**.
3. Use a sub-account holding only what the bot may lose.
4. Start with an amount you can lose entirely (~₱5,000–15,000).
5. Check PH SEC/BSP status of your chosen exchange before depositing — see `GUIDE.md` §2.

## Optional dependencies

```bash
pip install ccxt      # only needed for live trading
```

## Files

```
run.py                  CLI
config/config.example.json   every setting, documented
agent/indicators.py     SMA EMA RSI ATR (stdlib, tested)
agent/datafeed.py       public candles + optional ccxt
agent/strategy.py       ← EDIT THIS: your signal logic
agent/risk.py           ← READ THIS: the guardrails
agent/broker.py         paper + live execution
agent/agent.py          the loop
agent/backtest.py       backtester
agent/llm.py            optional research/journal/audit layer (no order access)
data/                   state.json, trades.csv, backtest output
```

Not financial advice. Crypto trading can lose your entire balance; most retail bots do.
