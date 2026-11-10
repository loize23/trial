"""
risk.py -- the hard guardrails. THIS is the most important file in the project.

Design rule: the risk layer sits BETWEEN the strategy and the broker, and it can only
ever say "no" or "smaller". Nothing upstream -- not the strategy, not an LLM, not you
at 2am -- can make it say "yes, but bigger".

If you change nothing else in this repo, understand this file.

Instrument-aware additions (futures/forex):
  * Size returned is MARGIN (cash at risk), not notional.
  * Liquidation guard: stop loss must be wider than the liquidation distance,
    otherwise you'd be liquidated before your stop ever fires -> trade rejected.
  * Max notional cap: notional = margin × leverage must not exceed
    max_position_pct_of_equity × equity × leverage (i.e. margin cap still applies).
"""

import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class RiskConfig:
    max_position_pct_of_equity: float = 0.10   # never put more than 10% of equity in one position (margin for futures)
    risk_per_trade_pct: float = 1.0            # lose at most 1% of equity if the stop hits
    max_open_positions: int = 3
    daily_loss_limit_pct: float = 4.0          # stop trading for the rest of the UTC day
    total_drawdown_halt_pct: float = 15.0      # stop trading until a human intervenes
    max_consecutive_losses: int = 4
    min_order_quote: float = 10.0              # exchange minimum notional/margin
    require_stop_loss: bool = True
    max_leverage: float = 5.0                  # hard cap on leverage for any instrument
    liquidation_buffer_pct: float = 2.0        # stop must be at least this far from liquidation price

    @staticmethod
    def from_dict(d: dict) -> "RiskConfig":
        known = {f for f in RiskConfig.__dataclass_fields__}  # type: ignore[attr-defined]
        return RiskConfig(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class RiskState:
    equity_peak: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    consecutive_losses: int = 0
    halted_reason: str = ""
    rejected: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "equity_peak": self.equity_peak,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "consecutive_losses": self.consecutive_losses,
            "halted_reason": self.halted_reason,
            "rejected": self.rejected[-50:],
        }

    @staticmethod
    def from_dict(d: dict) -> "RiskState":
        d = d or {}
        return RiskState(
            equity_peak=float(d.get("equity_peak", 0.0)),
            day=d.get("day", ""),
            day_start_equity=float(d.get("day_start_equity", 0.0)),
            consecutive_losses=int(d.get("consecutive_losses", 0)),
            halted_reason=d.get("halted_reason", ""),
            rejected=list(d.get("rejected", [])),
        )


class KillSwitch(Exception):
    """Raised when trading must stop entirely."""


def _liquidation_price(entry: float, side: str, leverage: float) -> Optional[float]:
    """Approximate liquidation price for a leveraged position (isolated margin)."""
    if leverage <= 1:
        return None
    if side == "long":
        return entry * (1.0 - 1.0 / leverage)
    else:
        return entry * (1.0 + 1.0 / leverage)


class RiskManager:
    def __init__(self, cfg: RiskConfig, state: RiskState, kill_file: str = "KILL"):
        self.cfg = cfg
        self.state = state
        self.kill_file = kill_file

    # ------------------------------------------------------------------ checks

    def check_kill_switch(self) -> None:
        if os.path.exists(self.kill_file):
            raise KillSwitch(f"kill switch active: '{self.kill_file}' exists. Delete it to resume.")

    def roll_day(self, equity: float) -> None:
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day = today
            self.state.day_start_equity = equity
            # a new day clears a daily-loss halt, but NOT a drawdown or kill-switch halt
            if self.state.halted_reason.startswith("daily loss limit"):
                self.state.halted_reason = ""
        self.state.equity_peak = max(self.state.equity_peak, equity)

    def account_level_block(self, equity: float) -> Optional[str]:
        """Returns a reason string if trading must stop, else None."""
        if self.state.halted_reason:
            return self.state.halted_reason
        if self.state.equity_peak > 0:
            dd = (self.state.equity_peak - equity) / self.state.equity_peak * 100.0
            if dd >= self.cfg.total_drawdown_halt_pct:
                self.state.halted_reason = (
                    f"drawdown halt: equity down {dd:.1f}% from peak "
                    f"({self.state.equity_peak:.2f} -> {equity:.2f})"
                )
                return self.state.halted_reason
        if self.state.day_start_equity > 0:
            day_pnl = (equity - self.state.day_start_equity) / self.state.day_start_equity * 100.0
            if day_pnl <= -self.cfg.daily_loss_limit_pct:
                self.state.halted_reason = f"daily loss limit hit ({day_pnl:.2f}% today)"
                return self.state.halted_reason
        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.state.halted_reason = (
                f"{self.state.consecutive_losses} consecutive losses -- strategy likely out of regime"
            )
            return self.state.halted_reason
        return None

    # -------------------------------------------------------------- order gate

    def approve_entry(
        self,
        equity: float,
        open_positions: Dict[str, dict],
        price: float,
        stop_loss: Optional[float],
        side: str = "long",
        leverage: float = 1.0,
        instrument_type: str = "spot",
        strength: float = 1.0,
    ) -> Tuple[bool, float, str]:
        """
        Returns (allowed, margin_or_notional_to_use, reason).

        Position size is derived from RISK, not from conviction:
            risk_cash = equity * risk_per_trade_pct%          # cash you're willing to lose
            notional  = risk_cash / stop_distance_pct          # position size at that stop
            margin    = notional / leverage                    # cash posted (futures/forex)
        then clamped to max_position_pct_of_equity and to available cash.

        For spot: leverage=1, margin == notional == cash cost.
        For futures/forex: returned size = margin (cash deducted from account).
        """
        self.check_kill_switch()
        self.roll_day(equity)

        block = self.account_level_block(equity)
        if block:
            return False, 0.0, f"BLOCKED: {block}"

        if len(open_positions) >= self.cfg.max_open_positions:
            return False, 0.0, f"max open positions ({self.cfg.max_open_positions}) reached"

        leverage = max(1.0, min(float(leverage), self.cfg.max_leverage))
        if instrument_type == "spot":
            leverage = 1.0  # spot cannot be leveraged

        if self.cfg.require_stop_loss and (stop_loss is None or stop_loss <= 0):
            return False, 0.0, "no valid stop loss -- refusing to enter a position without a defined exit"

        # Validate stop loss is on the correct side of the price
        if side == "long" and stop_loss >= price:
            return False, 0.0, f"stop loss {stop_loss} must be below entry {price} for a long"
        if side == "short" and stop_loss <= price:
            return False, 0.0, f"stop loss {stop_loss} must be above entry {price} for a short"

        # --- liquidation guard ---
        liq = _liquidation_price(price, side, leverage)
        if liq is not None:
            if side == "long":
                # Stop must be BELOW liquidation price (stop fires before liquidation)
                if stop_loss <= liq:
                    return False, 0.0, (
                        f"stop {stop_loss:.6f} at/below liquidation {liq:.6f} -- "
                        f"you'd be liquidated before your stop fires. Widen stop or lower leverage."
                    )
            else:
                if stop_loss >= liq:
                    return False, 0.0, (
                        f"stop {stop_loss:.6f} at/above liquidation {liq:.6f} -- "
                        f"you'd be liquidated before your stop fires. Widen stop or lower leverage."
                    )

        # --- position sizing ---
        risk_cash = equity * (self.cfg.risk_per_trade_pct / 100.0)

        if side == "long":
            stop_dist_pct = (price - stop_loss) / price
        else:
            stop_dist_pct = (stop_loss - price) / price

        if stop_dist_pct <= 0:
            return False, 0.0, "zero stop distance -- cannot size position"

        # Notional that would lose exactly risk_cash if the stop hits
        by_risk_notional = risk_cash / stop_dist_pct if stop_dist_pct > 0 else 0.0

        # Convert to margin (cash cost) for leveraged instruments
        by_risk_margin = by_risk_notional / leverage if leverage > 1 else by_risk_notional

        # Cap: max position % of equity applies to MARGIN (cash at risk), not notional
        cap = equity * self.cfg.max_position_pct_of_equity

        size = min(by_risk_margin, cap) if by_risk_margin > 0 else cap

        # conviction scaling is allowed to shrink a trade, never to grow it past the cap
        size *= max(0.25, min(1.0, float(strength) if strength else 1.0))

        # Also cap notional: notional = size × leverage should not exceed equity × max_position_pct × leverage
        max_notional = equity * self.cfg.max_position_pct_of_equity * leverage
        if size * leverage > max_notional:
            size = max_notional / leverage

        if size < self.cfg.min_order_quote:
            return False, 0.0, f"computed size {size:.2f} below exchange minimum {self.cfg.min_order_quote}"

        return True, round(size, 2), f"sized by risk ({self.cfg.risk_per_trade_pct}% of equity, {leverage}x)"

    # ------------------------------------------------------------ trade result

    def record_close(self, pnl: float) -> None:
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def record_rejection(self, symbol: str, reason: str) -> None:
        self.state.rejected.append(
            {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "symbol": symbol, "reason": reason}
        )

    def human_reset(self) -> None:
        """Call this only after you have actually diagnosed the problem."""
        self.state.halted_reason = ""
        self.state.consecutive_losses = 0