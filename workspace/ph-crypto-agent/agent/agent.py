"""
agent.py -- the orchestration loop.

Each cycle:
  1. check the kill switch
  2. fetch candles for every symbol
  3. manage EXITS first (stop loss / take profit / trend flip / liquidation) <- exits before entries, always
  4. ask the strategy for entries
  5. pass every intent through the risk manager, which can only shrink or reject
  6. execute, log, notify, sleep

Instrument support:
  spot     -- long-only. Short signal = exit the long.
  futures  -- long OR short. Position reversal supported.
  forex    -- long OR short. Position reversal supported.

Run once:   python3 run.py cycle
Run forever: python3 run.py loop
"""

import datetime as _dt
import json
import os
import time
import traceback
import urllib.parse
import urllib.request
from typing import Dict, List

from .broker import LiveBroker, PaperBroker, _instrument_cfg
from .datafeed import fetch_candles, last_price, retry, instrument_type_for
from .risk import KillSwitch, RiskConfig, RiskManager, RiskState
from .strategy import get_strategy, list_strategies


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Notify:
    """Optional Telegram alerts. Set notify.telegram_bot_token / telegram_chat_id in config."""

    def __init__(self, cfg: dict):
        n = cfg.get("notify", {})
        self.token = n.get("telegram_bot_token") or os.environ.get("TG_BOT_TOKEN", "")
        self.chat = n.get("telegram_chat_id") or os.environ.get("TG_CHAT_ID", "")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10).read()
        except Exception as e:  # never let a notification failure kill the bot
            print(f"[notify] failed: {e}")


class TradingAgent:
    def __init__(self, cfg: dict, verbose: bool = True):
        self.cfg = cfg
        self.verbose = verbose
        self.mode = cfg.get("mode", "paper")
        self.symbols: List[str] = cfg.get("symbols", ["BTC/USDT"])
        self.timeframe = cfg.get("timeframe", "1h")
        self.candles_n = int(cfg.get("candles", 300))
        self.source = cfg.get("exchange", {}).get("id", "binance")
        strategy_name = cfg.get("strategy_name", "trend_pullback")
        self.strategy = get_strategy(strategy_name, cfg)
        self.strategy_name = strategy_name
        self.notify = Notify(cfg)

        if self.mode == "live":
            self.broker = LiveBroker(cfg)
        else:
            self.broker = PaperBroker(cfg)

        risk_state = RiskState.from_dict(self.broker.state.get("risk", {}))
        if risk_state.equity_peak <= 0:
            risk_state.equity_peak = float(self.broker.state.get("cash", cfg.get("starting_balance", 1000.0)) or 0)
        self.risk = RiskManager(RiskConfig.from_dict(cfg.get("risk", {})), risk_state)

    # ------------------------------------------------------------------ output

    def log(self, msg: str) -> None:
        line = f"[{_now()}] {msg}"
        print(line, flush=True)

    def _persist_risk(self) -> None:
        self.broker.state["risk"] = self.risk.state.to_dict()
        self.broker.save()

    # ------------------------------------------------------------------ prices

    def prices(self) -> Dict[str, float]:
        out = {}
        for s in self.symbols:
            try:
                itype = instrument_type_for(s, self.cfg)
                out[s] = retry(lambda s=s, t=itype: last_price(s, self.source, t))
            except Exception as e:
                self.log(f"price fetch failed for {s}: {e}")
        return out

    # ------------------------------------------------------------------- exits

    def manage_exits(self, prices: Dict[str, float]) -> List[dict]:
        closed = []
        for symbol in list(self.broker.positions.keys()):
            pos = self.broker.positions[symbol]
            price = prices.get(symbol)
            if price is None:
                try:
                    itype = instrument_type_for(symbol, self.cfg)
                    candles = retry(lambda: fetch_candles(symbol, self.timeframe, self.candles_n, self.source, itype))
                    price = candles[-1]["c"]
                except Exception as e:
                    self.log(f"cannot price {symbol}, skipping exit check: {e}")
                    continue

            # --- liquidation check first (futures/forex only) ---
            liq_rec = self.broker.check_liquidation(symbol, price)
            if liq_rec:
                pnl = liq_rec.get("pnl", 0.0) or 0.0
                self.risk.record_close(pnl)
                closed.append(liq_rec)
                msg = f"LIQUIDATED {symbol} | {liq_rec.get('reason','')} | PnL {pnl:+.2f} {self.cfg.get('base_currency','USDT')}"
                self.log(msg)
                if self.notify.enabled:
                    self.notify.send(msg)
                continue

            side = pos.get("side", "long")
            sl, tp = pos.get("stop_loss"), pos.get("take_profit")
            reason = None

            if side == "long":
                if sl and price <= sl:
                    reason = f"STOP LOSS hit @ {price:.6f} (stop {sl:.6f})"
                elif tp and price >= tp:
                    reason = f"TAKE PROFIT hit @ {price:.6f} (target {tp:.6f})"
            else:  # short
                if sl and price >= sl:
                    reason = f"STOP LOSS hit @ {price:.6f} (stop {sl:.6f})"
                elif tp and price <= tp:
                    reason = f"TAKE PROFIT hit @ {price:.6f} (target {tp:.6f})"

            if reason is None:
                # Trend flip check
                try:
                    itype = instrument_type_for(symbol, self.cfg)
                    candles = retry(lambda: fetch_candles(symbol, self.timeframe, self.candles_n, self.source, itype))
                    sig = self.strategy.evaluate(symbol, candles, self.cfg)
                    can_short = itype in ("futures", "forex")
                    if side == "long" and sig.action == "sell" and sig.side == "short" and can_short:
                        reason = f"trend flip to short: {sig.reason}"
                    elif side == "long" and sig.action == "sell":
                        reason = f"trend flip: {sig.reason}"
                    elif side == "short" and sig.action == "buy" and sig.side == "long":
                        reason = f"trend flip to long: {sig.reason}"
                except Exception as e:
                    self.log(f"signal check failed for {symbol}: {e}")

            if reason:
                rec = self.broker.close_position(symbol, price, reason)
                pnl = rec.get("pnl", 0.0) or 0.0
                self.risk.record_close(pnl)
                closed.append(rec)
                msg = f"CLOSED {symbol} ({side}) | {reason} | PnL {pnl:+.2f} {self.cfg.get('base_currency','USDT')}"
                self.log(msg)
                if self.notify.enabled:
                    self.notify.send(msg)
        return closed

    # ----------------------------------------------------------------- entries

    def consider_entries(self, prices: Dict[str, float]) -> List[dict]:
        opened = []
        for symbol in self.symbols:
            price = prices.get(symbol)
            if price is None:
                continue
            try:
                itype = instrument_type_for(symbol, self.cfg)
                candles = retry(lambda s=symbol, t=itype: fetch_candles(s, self.timeframe, self.candles_n, self.source, t))
            except Exception as e:
                self.log(f"fetch failed {symbol}: {e}")
                continue

            sig = self.strategy.evaluate(symbol, candles, self.cfg)
            if sig.action == "hold":
                if self.verbose:
                    self.log(f"{symbol}: {sig.action} -- {sig.reason} | {json.dumps(sig.indicators)}")
                continue

            can_short = itype in ("futures", "forex")
            existing = self.broker.positions.get(symbol)

            # --- decide what to do ---
            # sig.action == "buy"  -> want long
            # sig.action == "sell" -> want short (if can_short) or exit long (spot)
            want_long = sig.action == "buy" and sig.side == "long"
            want_short = sig.action == "sell" and sig.side == "short" and can_short

            # No actionable signal (e.g. spot sell signal with no open position to exit)
            if not want_long and not want_short:
                if self.verbose:
                    self.log(f"{symbol}: {sig.action} ({sig.side}) -- {sig.reason} | {json.dumps(sig.indicators)}")
                continue

            if existing:
                existing_side = existing.get("side", "long")
                if want_long and existing_side == "short":
                    # Reverse: close short, open long
                    self.log(f"{symbol}: reversing short→long ({sig.reason})")
                    try:
                        self.broker.close_position(symbol, price, f"reverse to long: {sig.reason}")
                    except Exception as e:
                        self.log(f"failed to close short for reversal {symbol}: {e}")
                        continue
                    existing = None
                elif want_short and existing_side == "long":
                    # Reverse: close long, open short
                    self.log(f"{symbol}: reversing long→short ({sig.reason})")
                    try:
                        self.broker.close_position(symbol, price, f"reverse to short: {sig.reason}")
                    except Exception as e:
                        self.log(f"failed to close long for reversal {symbol}: {e}")
                        continue
                    existing = None
                elif want_long and existing_side == "long":
                    continue  # already long, nothing to do
                elif want_short and existing_side == "short":
                    continue  # already short, nothing to do
                elif not can_short and sig.action == "sell":
                    # Spot: sell signal = exit long
                    self.log(f"{symbol}: exiting long on trend flip ({sig.reason})")
                    try:
                        self.broker.close_position(symbol, price, f"trend flip exit: {sig.reason}")
                    except Exception as e:
                        self.log(f"failed to exit {symbol}: {e}")
                    continue
                else:
                    continue  # unsure state, skip

            if existing is not None:
                # Already in desired position, skip
                continue

            # --- size and open new position ---
            equity = self.broker.equity(prices)
            stop_loss = sig.stop_loss
            if stop_loss is None:
                self.log(f"{symbol}: no stop loss from strategy, skipping entry")
                continue

            allowed, size, why = self.risk.approve_entry(
                equity=equity,
                open_positions=self.broker.positions,
                price=sig.price,
                stop_loss=stop_loss,
                strength=sig.strength,
            )
            cash = self.broker.state.get("cash", equity) if self.mode != "live" else equity
            size = min(size, max(0.0, cash - 1e-9))

            if not allowed or size < self.risk.cfg.min_order_quote:
                self.risk.record_rejection(symbol, why if not allowed else "size below minimum / insufficient cash")
                self.log(f"{symbol}: entry REJECTED by risk layer -- {why}")
                continue

            pos = self.broker.open_position(
                symbol=symbol,
                side=sig.side,
                quote_amount=size,
                price=sig.price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                reason=sig.reason,
            )
            opened.append(pos)
            itype = pos.get("instrument_type", "spot")
            liq = pos.get("liquidation_price")
            liq_str = f" | liq {liq:.6f}" if liq else ""
            msg = (
                f"OPENED {symbol} ({sig.side}) | qty {pos['qty']:.6f} @ {pos['entry_price']:.6f} | "
                f"margin {size:.2f} | SL {sig.stop_loss} TP {sig.take_profit}{liq_str} | {sig.reason}"
            )
            self.log(msg)
            if self.notify.enabled:
                self.notify.send(msg)
        return opened

    # ------------------------------------------------------------------ cycle

    def cycle(self) -> dict:
        self.risk.check_kill_switch()
        prices = self.prices()
        if not prices:
            raise RuntimeError("no market data available -- refusing to act blind")

        closed = self.manage_exits(prices)
        opened = self.consider_entries(prices)

        equity = self.broker.equity(prices)
        start = float(self.broker.state.get("starting_balance", equity) or equity)
        self.risk.roll_day(equity)
        self._persist_risk()

        summary = {
            "ts": _now(),
            "equity": round(equity, 2),
            "cash": round(float(self.broker.state.get("cash", 0) or 0), 2),
            "return_pct": round((equity - start) / start * 100.0, 2) if start else 0.0,
            "open_positions": {
                s: {
                    "qty": round(p["qty"], 6),
                    "side": p.get("side", "long"),
                    "entry": p["entry_price"],
                    "sl": p.get("stop_loss"),
                    "tp": p.get("take_profit"),
                    "liq": round(p.get("liquidation_price", 0), 6) if p.get("liquidation_price") else None,
                    "type": p.get("instrument_type", "spot"),
                }
                for s, p in self.broker.positions.items()
            },
            "opened": len(opened),
            "closed": len(closed),
            "halted": self.risk.state.halted_reason or None,
            "drawdown_pct": round(
                (self.risk.state.equity_peak - equity) / self.risk.state.equity_peak * 100.0, 2
            ) if self.risk.state.equity_peak else 0.0,
        }
        self.log("STATE " + json.dumps(summary))
        if summary["halted"]:
            self.log(f"*** HALTED: {summary['halted']} ***")
            if self.notify.enabled:
                self.notify.send(f"⛔ TRADING HALTED: {summary['halted']}")
        return summary

    def run_forever(self, max_cycles: int = 0) -> None:
        interval = int(self.cfg.get("loop_seconds", 300))
        self.log(f"agent started | mode={self.mode} | symbols={self.symbols} | tf={self.timeframe} | every {interval}s")
        if self.notify.enabled:
            self.notify.send(f"🤖 agent started in {self.mode.upper()} mode on {', '.join(self.symbols)}")
        n = 0
        while True:
            n += 1
            try:
                self.cycle()
            except KillSwitch as e:
                self.log(f"KILL SWITCH: {e}")
                if self.notify.enabled:
                    self.notify.send(f"🛑 {e}")
                return
            except Exception as e:  # noqa: BLE001
                self.log(f"cycle error: {e}")
                traceback.print_exc()
            if max_cycles and n >= max_cycles:
                return
            time.sleep(interval)