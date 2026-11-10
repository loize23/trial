"""
broker.py -- execution layer.

PaperBroker  : simulates fills locally with fees + slippage. Use this for weeks.
LiveBroker   : real orders via ccxt. Refuses to run unless you have explicitly
                acknowledged the risks in config.

Both expose the same interface, so you can switch modes without touching strategy code.

Instrument support:
    spot     -- buy/sell base asset with quote currency (no leverage, no shorting)
    futures  -- USDT-margined perpetuals. Long OR short. Leverage + margin + liquidation.
    forex    -- spot FX. Long OR short. Lot-based sizing, pip-distance stops.

Position dict keys (all types):
    symbol, side ("long"|"short"), qty, entry_price, stop_loss, take_profit,
    liquidation_price, opened_at, opened_ts, reason, mode, instrument_type,
    contract_multiplier, leverage, margin, notional, cost, fee
"""

import csv
import datetime as _dt
import json
import os
import time
from typing import Dict, List, Optional


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _instrument_cfg(symbol: str, cfg: dict) -> dict:
    sc = cfg.get("symbol_config", {})
    entry = sc.get(symbol, cfg.get("default_instrument", {"type": "spot", "leverage": 1}))
    if isinstance(entry, str):
        return {"type": entry, "leverage": 1}
    return dict(entry)


def _pip_size(symbol: str) -> float:
    """Forex pip size: 0.01 for JPY pairs, 0.0001 otherwise."""
    parts = symbol.split("/") if "/" in symbol else [symbol, ""]
    quote = parts[1] if len(parts) > 1 else ""
    if quote == "JPY" or symbol.endswith("JPY"):
        return 0.01
    return 0.0001


def _liquidation_price(entry: float, side: str, leverage: float) -> Optional[float]:
    """Approximate liquidation price for a leveraged position (isolated margin)."""
    if leverage <= 1:
        return None
    if side == "long":
        # Long liquidates when price drops enough to wipe margin: entry * (1 - 1/leverage)
        return entry * (1.0 - 1.0 / leverage)
    else:
        # Short liquidates when price rises enough to wipe margin: entry * (1 + 1/leverage)
        return entry * (1.0 + 1.0 / leverage)


class BaseBroker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        fees = cfg.get("fees", {})
        self.taker_fee = float(fees.get("taker_pct", 0.1)) / 100.0
        self.slippage = float(fees.get("slippage_pct", 0.05)) / 100.0

    def _fill_price(self, price: float, side: str) -> float:
        """Model slippage: you buy a bit higher and sell a bit lower than the signal price."""
        return price * (1 + self.slippage) if side == "buy" else price * (1 - self.slippage)


class PaperBroker(BaseBroker):
    """Local simulation. No exchange account required. State persists to JSON + CSV."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.state_file = cfg.get("state_file", "data/state.json")
        self.trades_csv = cfg.get("trades_csv", "data/trades.csv")
        self.state = self._load()

    # ------------------------------------------------------------------ state

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "cash": float(self.cfg.get("starting_balance", 1000.0)),
            "starting_balance": float(self.cfg.get("starting_balance", 1000.0)),
            "positions": {},
            "risk": {},
            "created": _now(),
        }

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_file)

    def reset(self) -> None:
        self.state = {
            "cash": float(self.cfg.get("starting_balance", 1000.0)),
            "starting_balance": float(self.cfg.get("starting_balance", 1000.0)),
            "positions": {},
            "risk": {},
            "created": _now(),
        }
        self.save()

    # ---------------------------------------------------------------- queries

    @property
    def positions(self) -> Dict[str, dict]:
        return self.state["positions"]

    def equity(self, prices: Dict[str, float]) -> float:
        eq = self.state["cash"]
        for sym, pos in self.positions.items():
            mark = prices.get(sym, pos["entry_price"])
            itype = pos.get("instrument_type", "spot")
            side = pos.get("side", "long")
            qty = pos["qty"]
            entry = pos["entry_price"]
            if itype == "spot":
                eq += qty * mark
            else:
                # Futures/forex: unrealized PnL on top of margin already deducted from cash
                if side == "long":
                    pnl = (mark - entry) * qty * pos.get("contract_multiplier", 1)
                else:
                    pnl = (entry - mark) * qty * pos.get("contract_multiplier", 1)
                eq += pos["margin"] + pnl  # margin returned + unrealized PnL
        return eq

    # ------------------------------------------------------------ position sizing

    def _size_for_instrument(
        self,
        symbol: str,
        side: str,
        quote_amount: float,
        price: float,
        stop_loss: Optional[float],
        cfg: dict,
    ) -> dict:
        """Compute qty, margin, notional, liquidation_price for the instrument type."""
        inst = _instrument_cfg(symbol, cfg)
        itype = inst.get("type", "spot")
        leverage = float(inst.get("leverage", 1))
        multiplier = float(inst.get("contract_multiplier", 1))
        min_qty = float(inst.get("min_qty", 0.001))
        tick_size = float(inst.get("tick_size", 0.01))

        result = {
            "instrument_type": itype,
            "leverage": leverage,
            "contract_multiplier": multiplier,
            "min_qty": min_qty,
            "tick_size": tick_size,
            "qty": 0.0,
            "notional": 0.0,
            "margin": 0.0,
            "liquidation_price": None,
            "cost": quote_amount,  # default: cash cost (spot)
        }

        if itype == "spot":
            # Spot: quote_amount buys base asset. qty = quote_amount / price (minus fees).
            fill = self._fill_price(price, "buy")
            qty = (quote_amount / fill) * (1 - self.taker_fee)
            result["qty"] = round(qty, 8)
            result["notional"] = quote_amount
            result["margin"] = 0.0
            result["cost"] = quote_amount
            result["liquidation_price"] = None
        elif itype in ("futures", "forex"):
            # Futures/forex: quote_amount is margin posted. Notional = margin * leverage.
            # qty = notional / (price * multiplier)
            margin = quote_amount
            notional = margin * leverage
            fill = self._fill_price(price, "buy")
            qty = notional / (fill * multiplier)
            # Round to tick size / min qty
            if tick_size > 0:
                qty = max(min_qty, round(qty / tick_size) * tick_size)
            else:
                qty = max(min_qty, round(qty, 8))
            result["qty"] = qty
            result["notional"] = qty * fill * multiplier
            result["margin"] = margin
            result["cost"] = margin  # cash deducted = margin
            result["liquidation_price"] = _liquidation_price(fill, side, leverage)
        return result

    # ----------------------------------------------------------------- orders

    def open_position(
        self,
        symbol: str,
        side: str,
        quote_amount: float,
        price: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        reason: str = "",
    ) -> dict:
        """Open a new position. side: "long" or "short"."""
        inst = _instrument_cfg(symbol, self.cfg)
        itype = inst.get("type", "spot")

        if itype == "spot" and side == "short":
            raise ValueError(f"{symbol}: spot trading does not support shorting")

        size_info = self._size_for_instrument(symbol, side, quote_amount, price, stop_loss, self.cfg)
        qty = size_info["qty"]
        fill = self._fill_price(price, "buy" if side == "long" else "sell")

        # Fee: for spot it's on quote amount; for futures/forex on notional.
        if itype == "spot":
            fee = quote_amount * self.taker_fee
            cost = quote_amount
        else:
            fee = size_info["notional"] * self.taker_fee
            cost = size_info["margin"]

        if cost > self.state["cash"] + 1e-9:
            raise ValueError(f"insufficient cash: need {cost:.2f}, have {self.state['cash']:.2f}")
        self.state["cash"] -= cost

        pos = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": fill,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "liquidation_price": size_info["liquidation_price"],
            "notional": size_info["notional"],
            "margin": size_info["margin"],
            "leverage": size_info["leverage"],
            "contract_multiplier": size_info["contract_multiplier"],
            "instrument_type": itype,
            "cost": cost,
            "fee": fee,
            "opened_at": _now(),
            "opened_ts": time.time(),
            "reason": reason,
            "mode": "paper",
        }
        self.positions[symbol] = pos
        self._log_trade("OPEN_" + side.upper(), symbol, qty, fill, cost, fee, None, None, reason)
        self.save()
        return pos

    def close_position(self, symbol: str, price: float, reason: str = "") -> dict:
        """Close an open position, realising PnL. Returns the trade record."""
        pos = self.positions.get(symbol)
        if not pos:
            raise ValueError(f"no open position for {symbol}")

        itype = pos.get("instrument_type", "spot")
        side = pos.get("side", "long")
        qty = pos["qty"]
        entry = pos["entry_price"]
        multiplier = pos.get("contract_multiplier", 1)
        fill = self._fill_price(price, "sell" if side == "long" else "buy")

        if itype == "spot":
            # Spot: sell base asset back to quote
            gross = qty * fill
            fee = gross * self.taker_fee
            proceeds = gross - fee
            self.state["cash"] += proceeds
            pnl = proceeds - pos["cost"]
            pnl_pct = (pnl / pos["cost"] * 100.0) if pos["cost"] else 0.0
        else:
            # Futures/forex: PnL on position, margin returned
            if side == "long":
                pnl = (fill - entry) * qty * multiplier
            else:
                pnl = (entry - fill) * qty * multiplier
            fee = pos["notional"] * self.taker_fee  # closing fee on notional
            pnl -= fee
            proceeds = pos["margin"] + pnl
            self.state["cash"] += proceeds
            pnl_pct = (pnl / pos["margin"] * 100.0) if pos["margin"] else 0.0

        record = {
            "symbol": symbol,
            "side": side,
            "entry": round(entry, 6),
            "exit": round(fill, 6),
            "qty": round(qty, 8),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "closed_at": _now(),
            "held_hours": round((time.time() - pos["opened_ts"]) / 3600.0, 2),
            "fees": round(pos.get("fee", 0) + abs(fee), 2),
            "instrument_type": itype,
        }
        del self.positions[symbol]
        self._log_trade("CLOSE_" + side.upper(), symbol, qty, fill, pos.get("notional", pos["cost"]), abs(fee), round(pnl, 2), round(pnl_pct, 2), reason)
        self.save()
        return record

    def check_liquidation(self, symbol: str, price: float) -> Optional[dict]:
        """If price crosses liquidation price, close the position at liquidation price.
        Returns the close record, or None if no liquidation."""
        pos = self.positions.get(symbol)
        if not pos:
            return None
        liq = pos.get("liquidation_price")
        if liq is None:
            return None
        side = pos.get("side", "long")
        hit = (side == "long" and price <= liq) or (side == "short" and price >= liq)
        if hit:
            # Liquidation: close at liq price, lose margin (plus any additional loss)
            return self.close_position(symbol, liq, f"LIQUIDATION @ {liq:.6f} (liquidation price {liq:.6f})")
        return None

    def _log_trade(self, side, symbol, qty, price, quote, fee, pnl, pnl_pct, reason) -> None:
        os.makedirs(os.path.dirname(self.trades_csv) or ".", exist_ok=True)
        new = not os.path.exists(self.trades_csv)
        with open(self.trades_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "side", "symbol", "qty", "price", "quote", "fee", "pnl", "pnl_pct", "reason"])
            w.writerow([_now(), side, symbol, f"{qty:.8f}", f"{price:.8f}", f"{quote:.2f}", f"{fee:.4f}",
                        "" if pnl is None else pnl, "" if pnl_pct is None else pnl_pct, reason])


class LiveBroker(BaseBroker):
    """
    Real money. Requires ccxt (`pip install ccxt`) and an API key with:
      - trading permission ENABLED
      - withdrawal permission DISABLED  (non-negotiable)
      - an IP allowlist containing only your server's IP
    """

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        from .datafeed import ccxt_exchange

        ex_cfg = cfg.get("exchange", {})
        if not ex_cfg.get("api_key") or not ex_cfg.get("api_secret"):
            raise RuntimeError("live mode requires exchange.api_key and exchange.api_secret")
        if cfg.get("mode") != "live":
            raise RuntimeError("LiveBroker instantiated but mode != 'live'")
        # Determine defaultType from the first symbol's instrument config
        symbols = cfg.get("symbols", [])
        default_type = "spot"
        for sym in symbols:
            inst = _instrument_cfg(sym, cfg)
            if inst.get("type") in ("futures", "forex"):
                default_type = "swap"
                break
        self.ex = ccxt_exchange(
            ex_cfg.get("id", "binance"),
            ex_cfg["api_key"],
            ex_cfg["api_secret"],
            default_type=default_type,
        )
        self.state_file = cfg.get("state_file", "data/state.json").replace(".json", ".live.json")
        self.trades_csv = cfg.get("trades_csv", "data/trades.csv").replace(".csv", ".live.csv")
        self.state = self._load_live_state()

    def _load_live_state(self) -> dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"risk": {}, "positions": {}, "created": _now()}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    @property
    def positions(self) -> Dict[str, dict]:
        """Live positions are re-synced from the exchange each cycle -- never trust local cache."""
        return self._sync_positions()

    def _sync_positions(self) -> Dict[str, dict]:
        bal = self.ex.fetch_balance()
        out = {}
        for sym in self.cfg.get("symbols", []):
            base = sym.split("/")[0]
            free = float(bal.get(base, {}).get("free", 0) or 0)
            used = float(bal.get(base, {}).get("used", 0) or 0)
            total = free + used
            inst = _instrument_cfg(sym, self.cfg)
            itype = inst.get("type", "spot")
            if itype == "spot":
                if total * self._approx_price(sym) >= float(self.cfg.get("risk", {}).get("min_order_quote", 10)):
                    out[sym] = {"symbol": sym, "side": "long", "qty": total, "entry_price": self._approx_price(sym),
                                "cost": total * self._approx_price(sym), "instrument_type": "spot"}
            else:
                # Futures/forex positions come from ccxt positions endpoint if available
                try:
                    positions = self.ex.fetch_positions([sym])
                    for p in positions:
                        if float(p.get("contracts", 0) or 0) > 0:
                            out[sym] = {
                                "symbol": sym,
                                "side": p.get("side", "long"),
                                "qty": float(p.get("contracts", 0)),
                                "entry_price": float(p.get("entryPrice", 0) or 0),
                                "stop_loss": None,
                                "take_profit": None,
                                "instrument_type": itype,
                                "leverage": float(p.get("leverage", 1) or 1),
                                "notional": float(p.get("notional", 0) or 0),
                                "margin": float(p.get("margin", 0) or 0),
                                "contract_multiplier": float(inst.get("contract_multiplier", 1)),
                            }
                except Exception:
                    pass
        return out

    def _approx_price(self, symbol: str) -> float:
        t = self.ex.fetch_ticker(symbol)
        return float(t["last"])

    def equity(self, prices: Dict[str, float]) -> float:
        bal = self.ex.fetch_balance()
        quote = self.cfg.get("base_currency", "USDT")
        eq = float(bal.get(quote, {}).get("total", 0) or 0)
        for sym, p in prices.items():
            base = sym.split("/")[0]
            eq += float(bal.get(base, {}).get("total", 0) or 0) * p
        return eq

    def open_position(self, symbol, side, quote_amount, price, stop_loss, take_profit, reason=""):
        inst = _instrument_cfg(symbol, self.cfg)
        itype = inst.get("type", "spot")
        leverage = float(inst.get("leverage", 1))

        if itype == "spot":
            if side == "short":
                raise ValueError(f"{symbol}: spot does not support shorting")
            amount = quote_amount / price
            order = self.ex.create_order(symbol, "market", "buy", amount)
            fill = float(order.get("average") or order.get("price") or price)
            pos = {
                "symbol": symbol, "side": "long", "qty": amount, "entry_price": fill,
                "cost": quote_amount, "stop_loss": stop_loss, "take_profit": take_profit,
                "opened_at": _now(), "opened_ts": time.time(), "reason": reason,
                "order_id": order.get("id"), "mode": "live", "instrument_type": "spot",
            }
        else:
            # Futures/forex: set leverage, then open position
            try:
                self.ex.set_leverage(leverage, symbol)
            except Exception:
                pass  # some exchanges don't support set_leverage via REST
            amount = (quote_amount * leverage) / price  # contracts = notional / price
            order_side = "buy" if side == "long" else "sell"
            params = {"leverage": leverage}
            order = self.ex.create_order(symbol, "market", order_side, amount, params=params)
            fill = float(order.get("average") or order.get("price") or price)
            pos = {
                "symbol": symbol, "side": side, "qty": amount, "entry_price": fill,
                "stop_loss": stop_loss, "take_profit": take_profit,
                "opened_at": _now(), "opened_ts": time.time(), "reason": reason,
                "order_id": order.get("id"), "mode": "live", "instrument_type": itype,
                "leverage": leverage, "notional": amount * fill,
                "contract_multiplier": float(inst.get("contract_multiplier", 1)),
            }

        self.state.setdefault("positions", {})[symbol] = pos
        self.save()
        return pos

    def close_position(self, symbol, price, reason=""):
        cached = self.state.get("positions", {}).get(symbol, {})
        side = cached.get("side", "long")
        amount = float(cached.get("qty") or 0)
        if amount <= 0:
            amount = float(self.ex.fetch_balance().get(symbol.split("/")[0], {}).get("free", 0) or 0)
        order_side = "sell" if side == "long" else "buy"
        order = self.ex.create_order(symbol, "market", order_side, amount)
        fill = float(order.get("average") or order.get("price") or price)
        self.state.get("positions", {}).pop(symbol, None)
        self.save()
        return {"symbol": symbol, "side": side, "exit": fill, "reason": reason, "order_id": order.get("id")}

    def check_liquidation(self, symbol: str, price: float) -> Optional[dict]:
        """ccxt live trading: liquidation is handled by the exchange. We just detect it."""
        pos = self.state.get("positions", {}).get(symbol)
        if not pos:
            return None
        liq = pos.get("liquidation_price")
        if liq is None:
            return None
        side = pos.get("side", "long")
        hit = (side == "long" and price <= liq) or (side == "short" and price >= liq)
        if hit:
            return self.close_position(symbol, liq, f"LIQUIDATION @ {liq:.6f}")
        return None