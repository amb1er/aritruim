"""Historical performance guard for farming route selection."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.models import utc_now

ACCOUNT_SUFFIX = re.compile(r"_account_\d+$")


def base_venue(name: str) -> str:
    return ACCOUNT_SUFFIX.sub("", name)


@dataclass
class PerformanceStats:
    trades: int = 0
    wins: int = 0
    total_return: Decimal = Decimal("0")
    last_closed_at: datetime | None = None
    worst_return: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    gross_loss: Decimal = Decimal("0")

    def add(self, net_pnl: Decimal, notional_usd: Decimal, closed_at: datetime) -> None:
        if notional_usd <= 0:
            return
        trade_return = net_pnl / notional_usd
        self.trades += 1
        self.wins += int(net_pnl > 0)
        self.total_return += trade_return
        self.worst_return = min(self.worst_return, trade_return)
        if trade_return > 0:
            self.gross_profit += trade_return
        elif trade_return < 0:
            self.gross_loss += -trade_return
        if self.last_closed_at is None or closed_at > self.last_closed_at:
            self.last_closed_at = closed_at

    @property
    def win_rate(self) -> Decimal:
        return Decimal(self.wins) / Decimal(self.trades) if self.trades else Decimal("0")

    @property
    def average_return(self) -> Decimal:
        return self.total_return / Decimal(self.trades) if self.trades else Decimal("0")

    @property
    def profit_factor(self) -> Decimal:
        if self.gross_loss == 0:
            return Decimal("Infinity") if self.gross_profit > 0 else Decimal("0")
        return self.gross_profit / self.gross_loss


@dataclass(frozen=True)
class PerformanceDecision:
    allowed: bool
    reason: str
    combo: PerformanceStats
    route: PerformanceStats


class PerformanceGuard:
    """Reject recently unprofitable route patterns while allowing later probes."""

    def __init__(
        self,
        *,
        min_combo_trades: int,
        min_route_trades: int,
        block_seconds: int,
        tail_loss_return: Decimal,
    ) -> None:
        self.min_combo_trades = min_combo_trades
        self.min_route_trades = min_route_trades
        self.block_seconds = block_seconds
        self.tail_loss_return = tail_loss_return
        self.combos: dict[tuple[str, str, str], PerformanceStats] = defaultdict(PerformanceStats)
        self.routes: dict[tuple[str, str], PerformanceStats] = defaultdict(PerformanceStats)

    def add_trade(
        self,
        symbol: str,
        long_venue: str,
        short_venue: str,
        notional_usd: Decimal,
        net_pnl: Decimal,
        closed_at: datetime,
    ) -> None:
        route_key = (base_venue(long_venue), base_venue(short_venue))
        combo_key = (symbol, *route_key)
        self.combos[combo_key].add(net_pnl, notional_usd, closed_at)
        self.routes[route_key].add(net_pnl, notional_usd, closed_at)

    def decide(
        self, symbol: str, long_venue: str, short_venue: str, now: datetime | None = None
    ) -> PerformanceDecision:
        now = now or utc_now()
        route_key = (base_venue(long_venue), base_venue(short_venue))
        combo = self.combos[(symbol, *route_key)]
        route = self.routes[route_key]

        # A sufficiently profitable exact combination overrides a weak broad route.
        if combo.trades >= 3 and combo.total_return > 0 and combo.win_rate >= Decimal("0.66"):
            return PerformanceDecision(True, "profitable_combo", combo, route)
        if self._active(combo, now):
            if combo.trades >= self.min_combo_trades and combo.total_return < 0:
                return PerformanceDecision(False, "negative_symbol_route_history", combo, route)
            if combo.trades and combo.worst_return <= -self.tail_loss_return:
                return PerformanceDecision(False, "recent_tail_loss", combo, route)
        if (
            self._active(route, now)
            and route.trades >= self.min_route_trades
            and route.total_return < 0
        ):
            return PerformanceDecision(False, "negative_route_expectancy", combo, route)
        return PerformanceDecision(True, "insufficient_or_positive_history", combo, route)

    def _active(self, stats: PerformanceStats, now: datetime) -> bool:
        return bool(
            stats.last_closed_at
            and (now - stats.last_closed_at).total_seconds() < self.block_seconds
        )
