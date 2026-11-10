#!/usr/bin/env python3
"""
run.py -- CLI for the ph-crypto-agent starter kit.

  python3 run.py selftest                 # verify indicators + risk logic locally, no network
  python3 run.py backtest [--limit 1000]  # test the strategy on real historical candles
  python3 run.py cycle                    # ONE paper-trading cycle against live prices
  python3 run.py loop                     # keep running (uses config loop_seconds)
  python3 run.py status                   # show current paper portfolio
  python3 run.py brief                    # LLM market briefing (needs LLM_API_KEY)
  python3 run.py reset                    # wipe paper state and start over

Nothing here touches real money unless you edit config/config.json -> "mode": "live"
AND supply exchange API keys. Read GUIDE.md first.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.agent import TradingAgent          # noqa: E402
from agent.backtest import print_report, run_backtest  # noqa: E402
from agent.datafeed import instrument_type_for  # noqa: E402

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config.json")
EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config.example.json")


def load_cfg() -> dict:
    path = CONFIG if os.path.exists(CONFIG) else EXAMPLE
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if path == EXAMPLE:
        print(f"[config] using {os.path.basename(EXAMPLE)} -- copy it to config.json to customise.\n")
    # allow env overrides so you never commit secrets
    ex = cfg.setdefault("exchange", {})
    ex["api_key"] = os.environ.get("EXCHANGE_API_KEY", ex.get("api_key", ""))
    ex["api_secret"] = os.environ.get("EXCHANGE_API_SECRET", ex.get("api_secret", ""))
    n = cfg.setdefault("notify", {})
    n["telegram_bot_token"] = os.environ.get("TG_BOT_TOKEN", n.get("telegram_bot_token", ""))
    n["telegram_chat_id"] = os.environ.get("TG_CHAT_ID", n.get("telegram_chat_id", ""))
    return cfg


def cmd_selftest(_args) -> int:
    from agent.indicators import atr, ema, rsi, sma
    from agent.risk import RiskConfig, RiskManager, RiskState

    ok = True

    def check(label, got, want, tol=1e-6):
        nonlocal ok
        good = abs(got - want) <= tol
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] {label}: got {got:.6f}, want {want:.6f}")

    print("\nIndicators")
    series = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    check("sma(10,3) last", sma(series, 3)[-1], 9.0)
    check("ema(10,3) last", ema(series, 3)[-1], 9.0, tol=0.01)
    up = [float(i) for i in range(1, 40)]
    check("rsi(pure uptrend) == 100", rsi(up, 14)[-1], 100.0, tol=0.01)
    dn = [float(40 - i) for i in range(0, 39)]
    check("rsi(pure downtrend) == 0", rsi(dn, 14)[-1], 0.0, tol=0.01)
    # close == high each bar, range 1.0 -> true range is exactly 1.0 with no gap component
    h = [float(10 + i) for i in range(40)]
    l = [float(9 + i) for i in range(40)]
    c = [float(10 + i) for i in range(40)]
    check("atr(range 1, no gaps) == 1", atr(h, l, c, 14)[-1], 1.0, tol=0.02)
    # a 0.5 upward gap between bars must widen the true range to 1.5
    h2 = [float(10 + i) for i in range(40)]
    l2 = [float(9 + i) for i in range(40)]
    c2 = [float(9.5 + i) for i in range(40)]
    check("atr accounts for gaps (== 1.5)", atr(h2, l2, c2, 14)[-1], 1.5, tol=0.02)

    print("\nRisk manager")
    rm = RiskManager(RiskConfig(max_position_pct_of_equity=0.10, risk_per_trade_pct=1.0,
                                min_order_quote=10.0, require_stop_loss=True), RiskState())
    allowed, size, why = rm.approve_entry(equity=1000.0, open_positions={}, price=100.0, stop_loss=95.0, strength=1.0)
    check("1% risk / 5% stop -> 200 notional, capped at 10% -> 100", size, 100.0, tol=0.01)
    print(f"       (allowed={allowed}, {why})")

    allowed, size, why = rm.approve_entry(equity=1000.0, open_positions={}, price=100.0, stop_loss=None, strength=1.0)
    check("entry with NO stop is rejected", float(not allowed), 1.0, tol=0)
    print(f"       ({why})")

    allowed, size, why = rm.approve_entry(
        equity=1000.0, open_positions={"A": {}, "B": {}, "C": {}}, price=100.0, stop_loss=95.0)
    check("4th position rejected (max_open_positions=3)", float(not allowed), 1.0, tol=0)
    print(f"       ({why})")

    for _ in range(4):
        rm.record_close(-5.0)
    allowed, size, why = rm.approve_entry(equity=980.0, open_positions={}, price=100.0, stop_loss=95.0)
    check("halted after 4 consecutive losses", float(not allowed), 1.0, tol=0)
    print(f"       ({why})")

    rm2 = RiskManager(RiskConfig(), RiskState(equity_peak=1000.0, day_start_equity=1000.0))
    blk = rm2.account_level_block(equity=840.0)
    check("drawdown halt at 16%", float(blk is not None and "drawdown" in blk), 1.0, tol=0)
    print(f"       ({blk})")

    print("\nKill switch")
    open("KILL", "w").close()
    try:
        rm3 = RiskManager(RiskConfig(), RiskState())
        rm3.check_kill_switch()
        print("  [FAIL] kill switch did not fire")
        ok = False
    except Exception as e:
        print(f"  [PASS] kill switch fired: {e}")
    finally:
        os.remove("KILL")

    print("\n" + ("ALL SELF-TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


def cmd_backtest(args) -> int:
    cfg = load_cfg()
    if args.timeframe:
        cfg["timeframe"] = args.timeframe
    symbols = args.symbols.split(",") if args.symbols else cfg.get("symbols", ["BTC/USDT"])
    print(f"\nBacktesting {symbols} on {cfg['timeframe']} candles (last {args.limit} bars)...\n")
    results = run_backtest(cfg, symbols, args.limit)
    text = print_report(results)
    os.makedirs("data", exist_ok=True)
    with open("data/backtest_last.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    with open("data/backtest_last.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print("Saved: data/backtest_last.json")
    return 0


def cmd_cycle(_args) -> int:
    cfg = load_cfg()
    agent = TradingAgent(cfg)
    if cfg.get("mode") == "live":
        print("\n*** LIVE MODE *** real money. Press Ctrl-C in the next 10 seconds to abort.")
        import time
        time.sleep(10)
    agent.cycle()
    return 0


def cmd_loop(args) -> int:
    cfg = load_cfg()
    if cfg.get("mode") == "live":
        ack = os.environ.get("I_UNDERSTAND_I_CAN_LOSE_MONEY", "")
        if ack != "yes":
            print("\nRefusing to start LIVE mode.")
            print("If you have paper traded for weeks and backtested honestly, start it with:")
            print("  I_UNDERSTAND_I_CAN_LOSE_MONEY=yes python3 run.py loop\n")
            return 1
    TradingAgent(cfg).run_forever(max_cycles=args.cycles)
    return 0


def cmd_status(_args) -> int:
    cfg = load_cfg()
    agent = TradingAgent(cfg)
    prices = agent.prices()
    equity = agent.broker.equity(prices)
    start = float(agent.broker.state.get("starting_balance", equity) or equity)
    pos_list = []
    for s, p in agent.broker.positions.items():
        mark = prices.get(s, p["entry_price"])
        side = p.get("side", "long")
        itype = p.get("instrument_type", "spot")
        qty = p["qty"]
        entry = p["entry_price"]
        mult = p.get("contract_multiplier", 1)
        if itype == "spot":
            unrealised = (mark - entry) * qty
        else:
            if side == "long":
                unrealised = (mark - entry) * qty * mult
            else:
                unrealised = (entry - mark) * qty * mult
        pos_list.append({
            "symbol": s,
            "side": side,
            "type": itype,
            "qty": round(qty, 6),
            "entry": entry,
            "mark": mark,
            "unrealised": round(unrealised, 2),
            "stop_loss": p.get("stop_loss"),
            "take_profit": p.get("take_profit"),
            "liquidation_price": p.get("liquidation_price"),
            "leverage": p.get("leverage", 1),
            "notional": p.get("notional"),
            "margin": p.get("margin"),
            "opened_at": p.get("opened_at"),
        })
    print(json.dumps({
        "mode": agent.mode,
        "equity": round(equity, 2),
        "cash": round(float(agent.broker.state.get("cash", 0) or 0), 2),
        "starting_balance": start,
        "return_pct": round((equity - start) / start * 100.0, 2) if start else 0.0,
        "positions": pos_list,
        "risk": agent.risk.state.to_dict(),
    }, indent=2))
    return 0


def cmd_brief(_args) -> int:
    from agent import llm
    cfg = load_cfg()
    agent = TradingAgent(cfg)
    prices = agent.prices()
    table = {}
    for sym in agent.symbols:
        try:
            from agent.datafeed import fetch_candles, instrument_type_for
            itype = instrument_type_for(sym, cfg)
            candles = fetch_candles(sym, agent.timeframe, agent.candles_n, agent.source, itype)
            table[sym] = agent.strategy.evaluate(sym, candles, cfg).indicators
        except Exception as e:
            table[sym] = {"error": str(e)}
    equity = agent.broker.equity(prices)
    summary = {"equity": round(equity, 2), "positions": list(agent.broker.positions.keys()),
               "risk": agent.risk.state.to_dict()}
    print(llm.market_briefing(cfg, summary, table))
    return 0


def cmd_journal(_args) -> int:
    from agent import llm
    cfg = load_cfg()
    csv_path = cfg.get("trades_csv", "data/trades.csv")
    if not os.path.exists(csv_path):
        print(f"No trades yet at {csv_path}. Run some cycles first.")
        return 1
    with open(csv_path, "r", encoding="utf-8") as f:
        text = f.read()
    state_path = cfg.get("state_file", "data/state.json")
    with open(state_path, "r", encoding="utf-8") as f:
        st = json.load(f)
    print(llm.journal_review(cfg, text, json.dumps(st.get("risk", {}))))
    return 0


def cmd_reset(_args) -> int:
    cfg = load_cfg()
    broker = TradingAgent(cfg).broker
    broker.reset()
    print(f"Paper state reset to {cfg.get('starting_balance')} {cfg.get('base_currency')}.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="ph-crypto-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="verify indicators + risk guardrails offline").set_defaults(fn=cmd_selftest)

    bt = sub.add_parser("backtest", help="backtest the strategy on historical candles")
    bt.add_argument("--limit", type=int, default=1000)
    bt.add_argument("--symbols", type=str, default="")
    bt.add_argument("--timeframe", type=str, default="")
    bt.set_defaults(fn=cmd_backtest)

    sub.add_parser("cycle", help="run one trading cycle").set_defaults(fn=cmd_cycle)

    lp = sub.add_parser("loop", help="run continuously")
    lp.add_argument("--cycles", type=int, default=0)
    lp.set_defaults(fn=cmd_loop)

    sub.add_parser("status", help="show portfolio").set_defaults(fn=cmd_status)
    sub.add_parser("brief", help="LLM market briefing (needs LLM_API_KEY)").set_defaults(fn=cmd_brief)
    sub.add_parser("journal", help="LLM review of your trade log").set_defaults(fn=cmd_journal)
    sub.add_parser("reset", help="wipe paper state").set_defaults(fn=cmd_reset)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
