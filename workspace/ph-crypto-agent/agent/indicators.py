"""
indicators.py -- pure-stdlib technical indicators.

No numpy/pandas/TA-Lib needed. Everything takes and returns plain Python lists of
floats (or None where the window is not yet warm).
"""

from typing import List, Optional


def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = calc(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = calc(avg_gain, avg_loss)
    return out


def atr(high: List[float], low: List[float], close: List[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's Average True Range -- used for volatility-scaled stops."""
    n = len(close)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs = [None]  # index 0 has no previous close
    for i in range(1, n):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        trs.append(tr)
    first = sum(t for t in trs[1 : period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def slope_pct(values: List[Optional[float]], lookback: int = 5) -> Optional[float]:
    """Percent change over `lookback` bars, ignoring None gaps. Used for trend confirmation."""
    clean = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(clean) < lookback + 1:
        return None
    (_, older), (_, newer) = clean[-lookback - 1], clean[-1]
    if older == 0:
        return None
    return (newer - older) / older * 100.0
