import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.backtest import backtest, run_backtest
from agent.datafeed import fetch_candles, instrument_type_for
from agent.strategy import get_strategy

# Load exact config
with open("C:/Users/Loise/Downloads/workspace/ph-crypto-agent/config/config.example.json", "r") as f:
    cfg = json.load(f)

print("Config keys:", list(cfg.keys()))
print("strategy_name:", cfg.get("strategy_name"))
print("symbols:", cfg.get("symbols"))

# Test BTC/USDT specifically
sym = "BTC/USDT"
itype = instrument_type_for(sym, cfg)
print(f"\nInstrument type for {sym}: {itype}")

candles = fetch_candles(sym, cfg.get("timeframe", "1h"), 300, cfg.get("exchange", {}).get("id", "binance"), itype)
print(f"Fetched {len(candles)} candles")
print(f"First candle: {candles[0]}")
print(f"Last candle: {candles[-1]}")

strat = get_strategy(cfg.get("strategy_name", "trend_pullback"), cfg)
print(f"\nStrategy: {strat.name}")

# Count signals
buy_count = 0
sell_count = 0
hold_count = 0
no_data_count = 0
warming_count = 0

for i in range(50, len(candles) - 1):
    sig = strat.evaluate(sym, candles[: i + 1], cfg)
    if sig.action == "buy":
        buy_count += 1
        if buy_count <= 5:
            print(f"  BUY bar {i}: price={sig.price:.2f}, SL={sig.stop_loss}, TP={sig.take_profit}, strength={sig.strength:.4f}")
    elif sig.action == "sell":
        sell_count += 1
        if sell_count <= 5:
            print(f"  SELL bar {i}: price={sig.price:.2f}, SL={sig.stop_loss}, TP={sig.take_profit}, strength={sig.strength:.4f}")
    elif sig.action == "hold":
        hold_count += 1
    elif "not enough" in sig.reason:
        no_data_count += 1
    elif "warming" in sig.reason:
        warming_count += 1

print(f"\nSignal counts over {len(candles) - 51} bars:")
print(f"  BUY: {buy_count}")
print(f"  SELL: {sell_count}")
print(f"  HOLD: {hold_count}")
print(f"  No data: {no_data_count}")
print(f"  Warming: {warming_count}")

# Now run the actual backtest
print(f"\n\n=== Running backtest ===")
results = run_backtest(cfg, ["BTC/USDT"], 300)
for sym, m in results.items():
    print(f"\n{sym}:")
    if "error" in m:
        print(f"  ERROR: {m['error']}")
    else:
        print(f"  Return: {m['total_return_pct']:+.2f}%")
        print(f"  Trades: {m['trades']}")
        print(f"  Equity curve length: {len(m.get('equity_curve', []))}")
        print(f"  Trades detail length: {len(m.get('trades_detail', []))}")
        if m.get('trades_detail'):
            for t in m['trades_detail'][:3]:
                print(f"    Trade: {t['side']} entry={t['entry']} exit={t['exit']} pnl={t['pnl']} reason={t['exit_reason']}")
