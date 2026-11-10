"""
backtest.py -- test a strategy on real historical candles before it ever touches money.

IMPORTANT PROPERTIES OF THIS BACKTESTER (and of any backtester you should trust):
  * Indicators are computed from CAUSAL series only -- bar i never sees bar i+1.
  * Signals are generated on bar i's CLOSE and FILLED at bar i+1's OPEN.
    Filling at the signal bar's close is the #1 way people fake a profitable bot.
  * Stops/targets are checked against the next bars' HIGH/LOW. If both the stop and the
    target are inside the same bar, the STOP wins (pessimistic assumption).
  * Fees and slippage are charged on both legs.
  * Futures: liquidation is modeled -- if a bar's range crosses the liquidation price,
    the position is closed at the liquidation price (margin lost).
  * Futures: funding fees are deducted every 8 hours (3x/day) on open positions.
  * Works with any registered strategy (see agent/strategies.py).

A backtest is evidence that your code works. It is NOT evidence that you will make money.
Crypto regimes change; a strategy tuned on 2024 data is often dead by 2026.
"""

import json
import math
import statistics
from typing import Dict, List, Optional

from .datafeed import fetch_candles, instrument_type_for
from .indicators import atr
from .risk import _liquidation_price
from .strategy import get_strategy, Signal


def _metrics(equity_curve: List[float], trades: List[dict], start_balance: float) -> dict:
    if len(equity_curve) < 2:
        return {"error": "not enough data"}
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100.0)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    rets = [(equity_curve[i] / equity_curve[i - 1] - 1) for i in range(1, len(equity_curve)) if equity_curve[i - 1] > 0]
    sharpe = 0.0
    if len(rets) > 2 and statistics.pstdev(rets) > 0:
        # bars are whatever timeframe you passed; annualise assuming 24 bars/day for 1h
        sharpe = (statistics.mean(rets) / statistics.pstdev(rets)) * math.sqrt(24 * 365)

    return {
        "final_equity": round(equity_curve[-1], 2),
        "total_return_pct": round((equity_curve[-1] - start_balance) / start_balance * 100.0, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "avg_hold_bars": round(statistics.mean([t["bars_held"] for t in trades]), 1) if trades else 0.0,
        "sharpe_approx": round(sharpe, 2),
        "total_fees": round(sum(t.get("fees", 0) for t in trades), 2),
        "total_funding": round(sum(t.get("funding", 0) for t in trades), 2),
    }


def _inst_cfg(symbol: str, cfg: dict) -> dict:
    sc = cfg.get("symbol_config", {})
    entry = sc.get(symbol, cfg.get("default_instrument", {"type": "spot", "leverage": 1}))
    if isinstance(entry, str):
        return {"type": entry, "leverage": 1}
    return dict(entry)


def _timeframe_hours(timeframe: str) -> float:
    """Estimate hours per bar for funding period calculations."""
    tf = timeframe.lower()
    if tf.endswith("m"):
        return int(tf[:-1]) / 60.0
    if tf.endswith("h"):
        return float(tf[:-1])
    if tf.endswith("d"):
        return float(tf[:-1]) * 24.0
    return 1.0


def backtest(
    cfg: dict,
    symbol: str,
    candles: List[dict],
    start_balance: float = 1000.0,
) -> dict:
    """
    Run a no-lookahead backtest using the configured strategy.

    For each bar i (from warm to len-2):
      1. Mark to market, check account halts, funding deduction
      2. Manage open position: check liquidation, stop, take-profit, trend-flip
      3. If no position, call strategy.evaluate(candles[:i+1]) for entry signal
      4. If entry signal, size via risk layer and fill at bar i+1's open
    """
    strategy_name = cfg.get("strategy_name", "trend_pullback")
    strat = get_strategy(strategy_name, cfg)
    risk_cfg = cfg.get("risk", {})
    fees = cfg.get("fees", {})
    taker = float(fees.get("taker_pct", 0.1)) / 100.0
    slip = float(fees.get("slippage_pct", 0.05)) / 100.0
    min_notional = float(risk_cfg.get("min_order_quote", 10.0))
    risk_pct = float(risk_cfg.get("risk_per_trade_pct", 1.0)) / 100.0
    max_pos_pct = float(risk_cfg.get("max_position_pct_of_equity", 0.10))
    daily_halt = float(risk_cfg.get("daily_loss_limit_pct", 4.0)) / 100.0
    dd_halt = float(risk_cfg.get("total_drawdown_halt_pct", 15.0)) / 100.0

    inst = _inst_cfg(symbol, cfg)
    itype = inst.get("type", "spot")
    leverage = float(inst.get("leverage", 1))
    multiplier = float(inst.get("contract_multiplier", 1))
    funding_rate = float(inst.get("funding_rate_pct", 0.0))
    can_short = itype in ("futures", "forex")

    close = [c["c"] for c in candles]
    high = [c["h"] for c in candles]
    low = [c["l"] for c in candles]
    opens = [c["o"] for c in candles]

    cash = start_balance
    pos: Optional[dict] = None
    trades: List[dict] = []
    equity_curve: List[float] = [start_balance]
    peak = start_balance
    day = ""
    day_start = start_balance
    halted = False
    bars_since_funding = 0
    funding_interval = max(1, int(8.0 / _timeframe_hours(cfg.get("timeframe", "1h"))))

    # Warm-up: need enough candles for the strategy's indicators
    warm = 50  # safe minimum; strategies return "not enough candles" if shorter

    for i in range(warm, len(candles) - 1):
        # ---- mark to market with THIS bar's close
        if pos:
            mark = close[i]
            side = pos["side"]
            entry = pos["entry_price"]
            qty = pos["qty"]
            mult = pos.get("contract_multiplier", 1)
            if side == "long":
                unrealized = (mark - entry) * qty * mult
            else:
                unrealized = (entry - mark) * qty * mult
            if itype == "spot":
                eq = cash + qty * mark
            else:
                eq = cash + pos["margin"] + unrealized
        else:
            eq = cash
        equity_curve.append(eq)
        peak = max(peak, eq)

        # ---- funding deduction (futures only, every funding interval)
        if pos and itype == "futures" and funding_rate != 0:
            bars_since_funding += 1
            if bars_since_funding >= funding_interval:
                bars_since_funding = 0
                funding_fee = pos["notional"] * funding_rate
                cash -= funding_fee
                eq -= funding_fee
                equity_curve[-1] = eq
                pos["_funding_paid"] = pos.get("_funding_paid", 0.0) + funding_fee

        # ---- account-level halts
        bar_day = str(candles[i]["t"])[:8]
        if bar_day != day:
            day, day_start = bar_day, eq
        if not halted:
            if peak > 0 and (peak - eq) / peak >= dd_halt:
                halted = True
            if day_start > 0 and (eq - day_start) / day_start <= -daily_halt:
                halted = True

        # ---- manage an open position using THIS bar's range
        if pos:
            exit_price, why = None, None
            side = pos["side"]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]
            liq = pos.get("liquidation_price")

            # Liquidation check first (pessimistic)
            if liq is not None:
                if side == "long" and low[i] <= liq:
                    exit_price, why = liq, "liquidation"
                elif side == "short" and high[i] >= liq:
                    exit_price, why = liq, "liquidation"

            # Stop loss
            if exit_price is None:
                if side == "long" and sl and low[i] <= sl:
                    exit_price, why = sl, "stop"
                elif side == "short" and sl and high[i] >= sl:
                    exit_price, why = sl, "stop"

            # Take profit
            if exit_price is None:
                if side == "long" and tp and high[i] >= tp:
                    exit_price, why = tp, "target"
                elif side == "short" and tp and low[i] <= tp:
                    exit_price, why = tp, "target"

            # Trend flip: ask the strategy on candles up to this bar
            if exit_price is None:
                try:
                    sig = strat.evaluate(symbol, candles[: i + 1], cfg)
                    if side == "long" and sig.action == "sell":
                        if can_short and sig.side == "short":
                            exit_price, why = close[i], "trend_flip_to_short"
                        else:
                            exit_price, why = close[i], "trend_flip_exit"
                    elif side == "short" and sig.action == "buy" and sig.side == "long":
                        exit_price, why = close[i], "trend_flip_to_long"
                except Exception:
                    pass

            if exit_price is not None:
                fill = exit_price * (1 - slip)
                side = pos["side"]
                qty = pos["qty"]
                entry = pos["entry_price"]
                mult = pos.get("contract_multiplier", 1)

                if itype == "spot":
                    gross = qty * fill
                    fee = gross * taker
                    proceeds = gross - fee
                    pnl = proceeds - pos["cost"]
                    cash += proceeds
                else:
                    if side == "long":
                        pnl = (fill - entry) * qty * mult
                    else:
                        pnl = (entry - fill) * qty * mult
                    close_fee = pos["notional"] * taker
                    pnl -= close_fee
                    fee = pos.get("fee", 0.0) + close_fee
                    funding_paid = pos.get("_funding_paid", 0.0)
                    pnl -= funding_paid
                    proceeds = pos["margin"] + pnl
                    cash += proceeds

                pnl_pct = (pnl / pos["cost"] * 100.0) if pos["cost"] else 0.0
                trades.append({
                    "symbol": symbol, "side": side,
                    "entry": round(entry, 6), "exit": round(fill, 6),
                    "qty": round(qty, 8), "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": why, "bars_held": i - pos["bar"],
                    "fees": round(fee, 2),
                    "funding": round(pos.get("_funding_paid", 0.0), 2),
                    "instrument_type": itype,
                })
                pos = None

        # ---- entry decision on bar i's CLOSE, filled at bar i+1's OPEN
        if pos is None and not halted:
            try:
                sig = strat.evaluate(symbol, candles[: i + 1], cfg)
            except Exception:
                continue

            # Determine entry side
            entry_side = None
            if sig.action == "buy" and sig.side == "long":
                entry_side = "long"
            elif sig.action == "sell" and sig.side == "short" and can_short:
                entry_side = "short"

            if entry_side is None:
                continue  # no actionable signal (e.g. spot sell with no position)

            entry_raw = opens[i + 1]
            fill = entry_raw * (1 + slip)
            stop_loss = sig.stop_loss
            if stop_loss is None:
                continue

            # Size via risk layer
            stop_dist_pct = abs(fill - stop_loss) / fill
            if stop_dist_pct <= 0:
                continue

            risk_cash = eq * risk_pct
            by_risk_notional = risk_cash / stop_dist_pct
            by_risk_margin = by_risk_notional / leverage if leverage > 1 else by_risk_notional
            size = min(by_risk_margin, eq * max_pos_pct, cash)
            if leverage > 1:
                max_notional = eq * max_pos_pct * leverage
                if size * leverage > max_notional:
                    size = max_notional / leverage

            if size < min_notional:
                continue

            # Compute fees
            if itype == "spot":
                fee = size * taker
                qty = ((size - fee) / fill)
                notional = size
                margin = 0.0
                cost = size
            else:
                notional = size * leverage
                qty = notional / (fill * multiplier)
                margin = size
                cost = size
                fee = notional * taker

            liq = _liquidation_price(fill, entry_side, leverage) if leverage > 1 else None

            cash -= cost
            pos = {
                "qty": qty, "entry_price": fill, "cost": cost, "fee": fee,
                "stop_loss": stop_loss, "take_profit": sig.take_profit, "bar": i + 1,
                "side": entry_side, "notional": notional, "margin": margin,
                "leverage": leverage, "contract_multiplier": multiplier,
                "instrument_type": itype, "liquidation_price": liq,
                "reason": sig.reason,
            }

    # close any remaining position at the last close
    if pos:
        fill = close[-1] * (1 - slip)
        side = pos["side"]
        entry = pos["entry_price"]
        qty = pos["qty"]
        mult = pos.get("contract_multiplier", 1)
        if itype == "spot":
            gross = qty * fill
            fee = gross * taker
            proceeds = gross - fee
            pnl = proceeds - pos["cost"]
            cash += proceeds
        else:
            if side == "long":
                pnl = (fill - entry) * qty * mult
            else:
                pnl = (entry - fill) * qty * mult
            close_fee = pos["notional"] * taker
            pnl -= close_fee
            pnl -= pos.get("_funding_paid", 0.0)
            proceeds = pos["margin"] + pnl
            cash += proceeds
        pnl_pct = (pnl / pos["cost"] * 100.0) if pos["cost"] else 0.0
        trades.append({
            "symbol": symbol, "side": side,
            "entry": round(entry, 6), "exit": round(fill, 6),
            "qty": round(qty, 8), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": "end_of_data", "bars_held": len(candles) - pos["bar"],
            "fees": round(pos.get("fee", 0.0) + (pos.get("notional", 0) * taker if itype != "spot" else 0.0), 2),
            "funding": round(pos.get("_funding_paid", 0.0), 2),
            "instrument_type": itype,
        })
        equity_curve[-1] = cash

    bh = (close[-1] - close[warm]) / close[warm] * 100.0
    m = _metrics(equity_curve, trades, start_balance)
    m["symbol"] = symbol
    m["bars"] = len(candles)
    m["timeframe"] = cfg.get("timeframe", "1h")
    m["instrument_type"] = itype
    m["leverage"] = leverage
    m["strategy"] = strategy_name
    m["buy_and_hold_pct"] = round(bh, 2)
    m["halted_by_risk_limits"] = halted
    m["trades_detail"] = trades
    m["equity_curve"] = [round(v, 2) for v in equity_curve[:: max(1, len(equity_curve) // 200)]]
    return m


def run_backtest(cfg: dict, symbols: List[str], limit: int = 1000) -> Dict[str, dict]:
    out = {}
    for sym in symbols:
        itype = instrument_type_for(sym, cfg)
        candles = fetch_candles(
            sym,
            cfg.get("timeframe", "1h"),
            limit,
            cfg.get("exchange", {}).get("id", "binance"),
            itype,
        )
        out[sym] = backtest(cfg, sym, candles, float(cfg.get("starting_balance", 1000.0)))
    return out


def print_report(results: Dict[str, dict]) -> str:
    lines = ["", "=" * 78, "BACKTEST REPORT  (past performance != future results, obviously)", "=" * 78]
    for sym, m in results.items():
        if "error" in m:
            lines.append(f"{sym}: {m['error']}")
            continue
        lines += [
            f"\n{sym}  |  {m.get('strategy', '?')}  |  {m['instrument_type']} {m['leverage']}x  |  {m['timeframe']}  |  {m['bars']} bars",
            f"  strategy return : {m['total_return_pct']:+.2f}%   (buy & hold: {m['buy_and_hold_pct']:+.2f}%)",
            f"  final equity    : {m['final_equity']}",
            f"  max drawdown    : {m['max_drawdown_pct']:.2f}%",
            f"  trades          : {m['trades']}   win rate {m['win_rate_pct']}%   profit factor {m['profit_factor']}",
            f"  avg win / loss  : {m['avg_win']:+.2f} / {m['avg_loss']:+.2f}   avg hold {m['avg_hold_bars']} bars",
            f"  fees paid       : {m['total_fees']}   funding paid: {m['total_funding']}   sharpe(approx) {m['sharpe_approx']}",
            f"  risk limits hit : {m['halted_by_risk_limits']}",
        ]
    lines += ["", "=" * 78,
              "Read this BEFORE you get excited:",
              "  - If it beat buy & hold by a lot on one symbol, that is usually luck or curve-fit.",
              "  - Re-run it on a different timeframe and different symbols. Robust > spectacular.",
              "  - Now change one parameter by 20%. If the result collapses, it was curve-fit.",
              "=" * 78, ""]
    text = "\n".join(lines)
    print(text)
    return text