"""
strategies.py -- pluggable strategy framework.

Each strategy implements:
    evaluate(symbol, candles, cfg) -> Signal

All strategies are deterministic and backtestable. No LLM involvement.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .indicators import atr, ema, rsi, slope_pct


@dataclass
class Signal:
    symbol: str
    action: str = "hold"          # "buy" | "sell" | "hold"
    side: str = "long"            # "long" | "short" -- position direction (futures/forex only)
    price: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    indicators: dict = field(default_factory=dict)
    strength: float = 0.0         # 0..1, used for optional position scaling


# ============================================================================
# Base class
# ============================================================================

class BaseStrategy:
    """All strategies inherit from this. Subclasses must implement evaluate()."""

    name = "base"
    display_name = "Base Strategy"
    description = "Override this."

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _instrument_type(self, symbol: str) -> str:
        sc = self.cfg.get("symbol_config", {})
        entry = sc.get(symbol, self.cfg.get("default_instrument", {"type": "spot"}))
        if isinstance(entry, str):
            return entry
        return entry.get("type", "spot")

    def evaluate(self, symbol: str, candles: List[dict], cfg: dict = None) -> Signal:
        raise NotImplementedError


# ============================================================================
# Strategy 1: TrendPullback (existing, long-only spot / long-short futures)
# ============================================================================

class TrendPullback(BaseStrategy):
    """
    EMA trend + RSI filter + ATR stops.

    Long entry : fast EMA > slow EMA, slope rising, RSI < rsi_max_entry
    Short entry: fast EMA < slow EMA, slope falling, RSI > rsi_min_entry (futures/forex only)
    Exit       : stop loss / take profit / trend flip
    """

    name = "trend_pullback"
    display_name = "Trend Pullback"
    description = "EMA trend + RSI filter + ATR stops. Long-only on spot, long/short on futures/forex."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        s = cfg.get("strategy", {})
        self.fast = int(s.get("fast_ema", 12))
        self.slow = int(s.get("slow_ema", 26))
        self.rsi_period = int(s.get("rsi_period", 14))
        self.rsi_max = float(s.get("rsi_max_entry", 70))
        self.rsi_min = float(s.get("rsi_min_entry", 30))
        self.atr_period = int(s.get("atr_period", 14))
        self.stop_mult = float(s.get("atr_stop_mult", 2.0))
        self.target_mult = float(s.get("atr_target_mult", 3.0))

    def evaluate(self, symbol: str, candles: List[dict], cfg: dict = None) -> Signal:
        if len(candles) < max(self.slow, self.atr_period) + 5:
            return Signal(symbol=symbol, reason=f"not enough candles ({len(candles)})")

        close = [c["c"] for c in candles]
        high = [c["h"] for c in candles]
        low = [c["l"] for c in candles]
        price = close[-1]

        f = ema(close, self.fast)
        s_ema = ema(close, self.slow)
        r = rsi(close, self.rsi_period)
        a = atr(high, low, close, self.atr_period)

        fast_now, slow_now = f[-1], s_ema[-1]
        rsi_now, atr_now = r[-1], a[-1]
        if None in (fast_now, slow_now, rsi_now, atr_now) or atr_now == 0:
            return Signal(symbol=symbol, price=price, reason="indicators still warming up")

        trend_up = fast_now > slow_now
        trend_down = fast_now < slow_now
        rising = (slope_pct(f, 5) or 0.0) > 0
        falling = (slope_pct(f, 5) or 0.0) < 0
        not_ext_long = rsi_now < self.rsi_max
        not_ext_short = rsi_now > self.rsi_min

        can_short = self._instrument_type(symbol) in ("futures", "forex")

        sig = Signal(
            symbol=symbol,
            price=price,
            indicators={
                "ema_fast": round(fast_now, 6),
                "ema_slow": round(slow_now, 6),
                "rsi": round(rsi_now, 2),
                "atr": round(atr_now, 6),
                "trend_up": trend_up,
                "trend_down": trend_down,
                "slope5_pct": round(slope_pct(f, 5) or 0.0, 3),
            },
        )

        if trend_up and rising and not_ext_long:
            spread_pct = (fast_now - slow_now) / slow_now * 100.0
            sig.strength = max(0.0, min(1.0, spread_pct / 2.0)) * max(0.0, (self.rsi_max - rsi_now) / self.rsi_max)
            sig.action = "buy"
            sig.side = "long"
            sig.stop_loss = round(price - self.stop_mult * atr_now, 8)
            sig.take_profit = round(price + self.target_mult * atr_now, 8)
            sig.reason = (
                f"uptrend (EMA{self.fast}>{self.slow}, slope +{sig.indicators['slope5_pct']}%), "
                f"RSI {rsi_now:.1f} < {self.rsi_max:.0f}"
            )
        elif can_short and trend_down and falling and not_ext_short:
            spread_pct = (slow_now - fast_now) / slow_now * 100.0
            sig.strength = max(0.0, min(1.0, spread_pct / 2.0)) * max(0.0, (rsi_now - self.rsi_min) / (100.0 - self.rsi_min))
            sig.action = "sell"
            sig.side = "short"
            sig.stop_loss = round(price + self.stop_mult * atr_now, 8)
            sig.take_profit = round(price - self.target_mult * atr_now, 8)
            sig.reason = (
                f"downtrend (EMA{self.fast}<EMA{self.slow}, slope {sig.indicators['slope5_pct']}%), "
                f"RSI {rsi_now:.1f} > {self.rsi_min:.0f}"
            )
        elif not trend_up:
            sig.action = "sell"
            sig.side = "short" if can_short else "long"
            sig.reason = f"trend flipped (EMA{self.fast} < EMA{self.slow})"
        else:
            sig.action = "hold"
            sig.reason = f"trend up but RSI {rsi_now:.1f} >= {self.rsi_max:.0f} (extended) or slope flat"
        return sig


# ============================================================================
# Strategy 2: RSI Mean Reversion
# ============================================================================

class RSIMeanReversion(BaseStrategy):
    """
    Buy when RSI is oversold (below rsi_buy), sell when RSI is overbought (above rsi_sell).

    Works best on ranging markets. Long-only on spot, long/short on futures/forex.
    """

    name = "rsi_mean_reversion"
    display_name = "RSI Mean Reversion"
    description = "Buy oversold, sell overbought. Best for sideways/ranging markets."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        s = cfg.get("strategy", {})
        self.rsi_period = int(s.get("rsi_period", 14))
        self.rsi_buy = float(s.get("rsi_buy", 30))      # buy below this
        self.rsi_sell = float(s.get("rsi_sell", 70))    # sell above this
        self.atr_period = int(s.get("atr_period", 14))
        self.stop_mult = float(s.get("atr_stop_mult", 2.0))
        self.target_mult = float(s.get("atr_target_mult", 3.0))

    def evaluate(self, symbol: str, candles: List[dict], cfg: dict = None) -> Signal:
        if len(candles) < self.rsi_period + self.atr_period + 5:
            return Signal(symbol=symbol, reason=f"not enough candles ({len(candles)})")

        close = [c["c"] for c in candles]
        high = [c["h"] for c in candles]
        low = [c["l"] for c in candles]
        price = close[-1]

        r = rsi(close, self.rsi_period)
        a = atr(high, low, close, self.atr_period)

        rsi_now, atr_now = r[-1], a[-1]
        if None in (rsi_now, atr_now) or atr_now == 0:
            return Signal(symbol=symbol, price=price, reason="indicators still warming up")

        can_short = self._instrument_type(symbol) in ("futures", "forex")

        sig = Signal(
            symbol=symbol,
            price=price,
            indicators={
                "rsi": round(rsi_now, 2),
                "atr": round(atr_now, 6),
                "rsi_buy": self.rsi_buy,
                "rsi_sell": self.rsi_sell,
            },
        )

        if rsi_now <= self.rsi_buy:
            sig.action = "buy"
            sig.side = "long"
            sig.stop_loss = round(price - self.stop_mult * atr_now, 8)
            sig.take_profit = round(price + self.target_mult * atr_now, 8)
            sig.strength = max(0.0, min(1.0, (self.rsi_buy - rsi_now) / self.rsi_buy))
            sig.reason = f"RSI oversold: {rsi_now:.1f} <= {self.rsi_buy:.0f}"
        elif can_short and rsi_now >= self.rsi_sell:
            sig.action = "sell"
            sig.side = "short"
            sig.stop_loss = round(price + self.stop_mult * atr_now, 8)
            sig.take_profit = round(price - self.target_mult * atr_now, 8)
            sig.strength = max(0.0, min(1.0, (rsi_now - self.rsi_sell) / (100.0 - self.rsi_sell)))
            sig.reason = f"RSI overbought: {rsi_now:.1f} >= {self.rsi_sell:.0f}"
        elif not can_short and rsi_now >= self.rsi_sell:
            sig.action = "sell"
            sig.side = "long"
            sig.reason = f"RSI overbought: {rsi_now:.1f} >= {self.rsi_sell:.0f} (exit long)"
        else:
            sig.action = "hold"
            sig.reason = f"RSI {rsi_now:.1f} in neutral zone ({self.rsi_buy}–{self.rsi_sell})"
        return sig


# ============================================================================
# Strategy 3: Bollinger Band Mean Reversion
# ============================================================================

class BollingerBands(BaseStrategy):
    """
    Buy at lower Bollinger Band, sell at upper band. Middle band = SMA(20).

    Long-only on spot, long/short on futures/forex.
    """

    name = "bollinger_bands"
    display_name = "Bollinger Bands"
    description = "Buy at lower band, sell at upper band. Best for ranging markets."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        s = cfg.get("strategy", {})
        self.period = int(s.get("bb_period", 20))
        self.std_dev = float(s.get("bb_std", 2.0))
        self.atr_period = int(s.get("atr_period", 14))
        self.stop_mult = float(s.get("atr_stop_mult", 2.0))
        self.target_mult = float(s.get("atr_target_mult", 3.0))

    def _sma(self, values: List[float], period: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if period <= 0 or len(values) < period:
            return out
        running = sum(values[:period])
        out[period - 1] = running / period
        for i in range(period, len(values)):
            running += values[i] - values[i - period]
            out[i] = running / period
        return out

    def evaluate(self, symbol: str, candles: List[dict], cfg: dict = None) -> Signal:
        if len(candles) < self.period + self.atr_period + 5:
            return Signal(symbol=symbol, reason=f"not enough candles ({len(candles)})")

        close = [c["c"] for c in candles]
        high = [c["h"] for c in candles]
        low = [c["l"] for c in candles]
        price = close[-1]

        sma = self._sma(close, self.period)
        a = atr(high, low, close, self.atr_period)

        # Standard deviation of close over period
        std = [None] * len(close)
        for i in range(self.period - 1, len(close)):
            window = close[i - self.period + 1 : i + 1]
            mean = sum(window) / self.period
            var = sum((x - mean) ** 2 for x in window) / self.period
            std[i] = var ** 0.5

        mid = sma[-1]
        std_now = std[-1]
        atr_now = a[-1]
        if None in (mid, std_now, atr_now) or atr_now == 0:
            return Signal(symbol=symbol, price=price, reason="indicators still warming up")

        upper = mid + self.std_dev * std_now
        lower = mid - self.std_dev * std_now
        can_short = self._instrument_type(symbol) in ("futures", "forex")

        sig = Signal(
            symbol=symbol,
            price=price,
            indicators={
                "bb_mid": round(mid, 6),
                "bb_upper": round(upper, 6),
                "bb_lower": round(lower, 6),
                "atr": round(atr_now, 6),
                "price_vs_mid_pct": round((price - mid) / mid * 100.0, 2),
            },
        )

        if price <= lower:
            sig.action = "buy"
            sig.side = "long"
            sig.stop_loss = round(price - self.stop_mult * atr_now, 8)
            sig.take_profit = round(mid, 8)
            sig.strength = max(0.0, min(1.0, (lower - price) / lower * 100.0))
            sig.reason = f"price at/below lower BB ({price:.6f} <= {lower:.6f})"
        elif can_short and price >= upper:
            sig.action = "sell"
            sig.side = "short"
            sig.stop_loss = round(price + self.stop_mult * atr_now, 8)
            sig.take_profit = round(mid, 8)
            sig.strength = max(0.0, min(1.0, (price - upper) / upper * 100.0))
            sig.reason = f"price at/above upper BB ({price:.6f} >= {upper:.6f})"
        elif not can_short and price >= upper:
            sig.action = "sell"
            sig.side = "long"
            sig.reason = f"price at upper BB ({price:.6f} >= {upper:.6f}) (exit long)"
        else:
            sig.action = "hold"
            sig.reason = f"price between bands ({lower:.6f} – {upper:.6f})"
        return sig


# ============================================================================
# Strategy 4: MACD Crossover
# ============================================================================

class MACDCrossover(BaseStrategy):
    """
    Buy when MACD line crosses above signal line, sell when it crosses below.

    Uses EMA(12), EMA(26), signal EMA(9). Long-only on spot, long/short on futures/forex.
    """

    name = "macd_crossover"
    display_name = "MACD Crossover"
    description = "Trend-following via MACD line/signal line crossovers."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        s = cfg.get("strategy", {})
        self.fast = int(s.get("fast_ema", 12))
        self.slow = int(s.get("slow_ema", 26))
        self.signal_period = int(s.get("macd_signal", 9))
        self.atr_period = int(s.get("atr_period", 14))
        self.stop_mult = float(s.get("atr_stop_mult", 2.0))
        self.target_mult = float(s.get("atr_target_mult", 3.0))

    def evaluate(self, symbol: str, candles: List[dict], cfg: dict = None) -> Signal:
        if len(candles) < self.slow + self.signal_period + self.atr_period + 5:
            return Signal(symbol=symbol, reason=f"not enough candles ({len(candles)})")

        close = [c["c"] for c in candles]
        high = [c["h"] for c in candles]
        low = [c["l"] for c in candles]
        price = close[-1]

        ema_fast = ema(close, self.fast)
        ema_slow = ema(close, self.slow)
        a = atr(high, low, close, self.atr_period)

        # MACD line = EMA(fast) - EMA(slow)
        macd_line = [None] * len(close)
        for i in range(len(close)):
            if ema_fast[i] is not None and ema_slow[i] is not None:
                macd_line[i] = ema_fast[i] - ema_slow[i]

        # Signal line = EMA(signal_period) of MACD line
        macd_signal = [None] * len(close)
        valid_macd = [m for m in macd_line if m is not None]
        if len(valid_macd) < self.signal_period:
            return Signal(symbol=symbol, price=price, reason="MACD still warming up")

        # Compute EMA of MACD line starting from first valid point
        start_idx = macd_line.index(valid_macd[0])
        k = 2.0 / (self.signal_period + 1.0)
        seed = sum(valid_macd[: self.signal_period]) / self.signal_period
        sig_idx = start_idx + self.signal_period - 1
        macd_signal[sig_idx] = seed
        prev = seed
        for i in range(sig_idx + 1, len(close)):
            if macd_line[i] is not None:
                prev = macd_line[i] * k + prev * (1 - k)
                macd_signal[i] = prev

        macd_now = macd_line[-1]
        signal_now = macd_signal[-1]
        macd_prev = macd_line[-2]
        signal_prev = macd_signal[-2]
        atr_now = a[-1]

        if None in (macd_now, signal_now, macd_prev, signal_prev, atr_now) or atr_now == 0:
            return Signal(symbol=symbol, price=price, reason="indicators still warming up")

        can_short = self._instrument_type(symbol) in ("futures", "forex")

        # Crossover detection
        crossed_up = macd_prev <= signal_prev and macd_now > signal_now
        crossed_down = macd_prev >= signal_prev and macd_now < signal_now

        sig = Signal(
            symbol=symbol,
            price=price,
            indicators={
                "macd": round(macd_now, 6),
                "macd_signal": round(signal_now, 6),
                "macd_hist": round(macd_now - signal_now, 6),
                "atr": round(atr_now, 6),
            },
        )

        if crossed_up:
            sig.action = "buy"
            sig.side = "long"
            sig.stop_loss = round(price - self.stop_mult * atr_now, 8)
            sig.take_profit = round(price + self.target_mult * atr_now, 8)
            sig.strength = min(1.0, abs(macd_now - signal_now) / max(price * 0.001, 1e-6))
            sig.reason = "MACD crossed above signal (bullish)"
        elif can_short and crossed_down:
            sig.action = "sell"
            sig.side = "short"
            sig.stop_loss = round(price + self.stop_mult * atr_now, 8)
            sig.take_profit = round(price - self.target_mult * atr_now, 8)
            sig.strength = min(1.0, abs(macd_now - signal_now) / max(price * 0.001, 1e-6))
            sig.reason = "MACD crossed below signal (bearish)"
        elif not can_short and crossed_down:
            sig.action = "sell"
            sig.side = "long"
            sig.reason = "MACD crossed below signal (exit long)"
        else:
            sig.action = "hold"
            sig.reason = "no MACD crossover"
        return sig


# ============================================================================
# Strategy registry
# ============================================================================

STRATEGY_REGISTRY = {
    TrendPullback.name: TrendPullback,
    RSIMeanReversion.name: RSIMeanReversion,
    BollingerBands.name: BollingerBands,
    MACDCrossover.name: MACDCrossover,
}


def get_strategy(name: str, cfg: dict) -> BaseStrategy:
    """Instantiate a strategy by name."""
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGY_REGISTRY.keys())}")
    return STRATEGY_REGISTRY[name](cfg)


def list_strategies() -> dict:
    """Return {name: {display_name, description}} for the UI."""
    return {
        name: {
            "display_name": cls.display_name,
            "description": cls.description,
        }
        for name, cls in STRATEGY_REGISTRY.items()
    }
