"""
datafeed.py -- market data, stdlib only (urllib).

Paper mode uses PUBLIC endpoints only (no account, no keys, works from most countries).
Live mode uses ccxt if you have it installed, otherwise falls back to the exchange's
REST API directly.

Returns candles as a list of dicts:
    {"t": int_ms, "o": float, "h": float, "l": float, "c": float, "v": float}

Supported instrument types:
    spot     -- Binance spot, Bybit spot
    futures  -- Binance USDT-margined perpetuals, Bybit linear perpetuals
    forex    -- EUR/USD, GBP/USD, etc. via ccxt (live) or public forex-friendly feeds
"""

import json
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

USER_AGENT = "ph-crypto-agent/0.1 (+local paper trader)"


class DataError(Exception):
    pass


def _get(url: str, params: Optional[Dict] = None, timeout: int = 15) -> object:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        body = e.read().decode("utf-8", "replace")[:300]
        raise DataError(f"HTTP {e.code} from {url}: {body}") from e
    except Exception as e:
        raise DataError(f"network error from {url}: {e}") from e


# ---------------------------------------------------------------- public feeds

BINANCE_SPOT_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BYBIT_KLINES = "https://api.bybit.com/v5/market/kline"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"

# Bybit category mapping
BYBIT_CATEGORY = {"spot": "spot", "futures": "linear", "forex": "spot"}

# Timeframe mappings per exchange
TF_BINANCE = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}
TF_BYBIT = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}


def candles_binance(symbol: str, timeframe: str = "1h", limit: int = 300, instrument_type: str = "spot") -> List[dict]:
    base = BINANCE_SPOT_KLINES if instrument_type == "spot" else BINANCE_FUTURES_KLINES
    raw = _get(base, {"symbol": symbol.replace("/", ""), "interval": TF_BINANCE.get(timeframe, timeframe), "limit": min(limit, 1000)})
    out = []
    for k in raw:  # type: ignore[index]
        out.append(
            {
                "t": int(k[0]),
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
            }
        )
    return out


def candles_bybit(symbol: str, timeframe: str = "60", limit: int = 300, instrument_type: str = "spot") -> List[dict]:
    raw = _get(BYBIT_KLINES, {"category": BYBIT_CATEGORY.get(instrument_type, "spot"), "symbol": symbol.replace("/", ""), "interval": timeframe, "limit": min(limit, 1000)})
    rows = raw.get("result", {}).get("list", [])  # type: ignore[union-attr]
    out = []
    for k in rows:
        out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
    out.reverse()  # Bybit returns newest-first
    return out


def instrument_type_for(symbol: str, cfg: dict) -> str:
    """Resolve the instrument type for a symbol from config."""
    sc = cfg.get("symbol_config", {})
    entry = sc.get(symbol, cfg.get("default_instrument", {"type": "spot"}))
    if isinstance(entry, str):
        return entry
    return entry.get("type", "spot")


def fetch_candles(symbol: str, timeframe: str = "1h", limit: int = 300, source: str = "binance", instrument_type: str = "spot") -> List[dict]:
    """Public candle fetch with an automatic fallback chain.

    instrument_type selects spot vs futures endpoints (Binance fapi vs api).
    """
    errors = []
    tf_bybit = TF_BYBIT.get(timeframe, "60")
    chain = [source, "binance", "bybit"]
    seen = set()
    for src in chain:
        if src in seen:
            continue
        seen.add(src)
        try:
            if src == "binance":
                data = candles_binance(symbol, timeframe, limit, instrument_type)
            elif src == "bybit":
                data = candles_bybit(symbol, tf_bybit, limit, instrument_type)
            else:
                continue
            if data:
                return data
            errors.append(f"{src}: empty response")
        except Exception as e:
            errors.append(f"{src}: {e}")
    raise DataError("all data sources failed -> " + " | ".join(errors))


def last_price(symbol: str, source: str = "binance", instrument_type: str = "spot") -> float:
    c = fetch_candles(symbol, "1m", 2, source, instrument_type)
    return float(c[-1]["c"])


def retry(fn, tries: int = 3, delay: float = 2.0, backoff: float = 2.0):
    """Simple retry wrapper -- exchange APIs hiccup, your bot should not crash."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay * (backoff**i))
    raise last


# ------------------------------------------------------------------ ccxt (live)

def ccxt_exchange(exchange_id: str, api_key: str = "", api_secret: str = "", testnet: bool = False, default_type: str = "spot"):
    """
    Optional live/paper connector via ccxt (`pip install ccxt`).
    ccxt normalises 100+ exchanges into one interface -- the single best starting point
    if you want to build your own bot from scratch.

    default_type: "spot" | "swap" (futures/perpetuals) | "future"
    """
    try:
        import ccxt  # type: ignore
    except ImportError as e:
        raise DataError("ccxt is not installed. Run: pip install ccxt") from e
    klass = getattr(ccxt, exchange_id, None)
    if klass is None:
        raise DataError(f"ccxt does not support exchange '{exchange_id}'")
    ex = klass({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True, "options": {"defaultType": default_type}})
    if testnet:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex
