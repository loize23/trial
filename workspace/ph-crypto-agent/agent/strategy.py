"""
strategy.py -- backward-compatible wrapper.

Imports TrendPullback from the new strategies framework so existing imports
(`from .strategy import TrendPullback`) continue to work.
"""

from .strategies import TrendPullback, Signal, BaseStrategy, get_strategy, list_strategies, STRATEGY_REGISTRY  # noqa: F401

__all__ = ["TrendPullback", "Signal", "BaseStrategy", "get_strategy", "list_strategies", "STRATEGY_REGISTRY"]
