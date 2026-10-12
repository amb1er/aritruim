"""Select an economically bounded long/short route across farming venues."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import permutations

ZERO = Decimal("0")


@dataclass(frozen=True)
class VenueQuote:
    venue: str
    symbol: str
    funding_per_hour: Decimal
    bid: Decimal
    ask: Decimal
    taker_fee: Decimal
    bid_liquidity_usd: Decimal
    ask_liquidity_usd: Decimal
    # Maximum campaign value assigned to one filled leg. It is a budget, not
    # an assumed cash value of points or airdrops.
    farming_subsidy_usd: Decimal = ZERO


@dataclass(frozen=True)
class FarmingRoute:
    symbol: str
    long_venue: str
    short_venue: str
    notional_usd: Decimal
    quantity: Decimal
    expected_funding_usd: Decimal
    round_trip_cost_usd: Decimal
    basis_reserve_usd: Decimal
    expected_cash_pnl_usd: Decimal
    farming_subsidy_usd: Decimal
    objective_usd: Decimal


class FarmingOptimizer:
    """Rank every ordered venue pair without hiding the cash cost of farming."""

    def __init__(
        self,
        *,
        notional_usd: Decimal,
        hold_hours: Decimal,
        basis_buffer_pct: Decimal = Decimal("0.0005"),
        min_exit_depth_multiplier: Decimal = Decimal("2"),
        max_cash_loss_usd: Decimal = ZERO,
        max_cross_venue_deviation_pct: Decimal = Decimal("0.02"),
    ) -> None:
        if notional_usd <= 0 or hold_hours <= 0 or min_exit_depth_multiplier < 1:
            raise ValueError("Invalid farming optimizer limits")
        self.notional_usd = notional_usd
        self.hold_hours = hold_hours
        self.basis_buffer_pct = basis_buffer_pct
        self.min_exit_depth_multiplier = min_exit_depth_multiplier
        self.max_cash_loss_usd = max_cash_loss_usd
        self.max_cross_venue_deviation_pct = max_cross_venue_deviation_pct

    def rank(self, quotes: list[VenueQuote]) -> list[FarmingRoute]:
        routes = []
        for long_quote, short_quote in permutations(quotes, 2):
            if long_quote.symbol != short_quote.symbol:
                continue
            route = self._route(long_quote, short_quote)
            if route is not None:
                routes.append(route)
        return sorted(
            routes,
            key=lambda item: (item.objective_usd, item.expected_cash_pnl_usd),
            reverse=True,
        )

    def best(self, quotes: list[VenueQuote]) -> FarmingRoute | None:
        ranked = self.rank(quotes)
        return ranked[0] if ranked else None

    def _route(self, long_quote: VenueQuote, short_quote: VenueQuote) -> FarmingRoute | None:
        values = (
            long_quote.bid,
            long_quote.ask,
            short_quote.bid,
            short_quote.ask,
            long_quote.taker_fee,
            short_quote.taker_fee,
        )
        if any(not value.is_finite() for value in values):
            return None
        if (
            long_quote.bid <= 0
            or long_quote.ask <= 0
            or short_quote.bid <= 0
            or short_quote.ask <= 0
            or long_quote.bid > long_quote.ask
            or short_quote.bid > short_quote.ask
            or long_quote.taker_fee < 0
            or short_quote.taker_fee < 0
        ):
            return None
        required_depth = self.notional_usd * self.min_exit_depth_multiplier
        if (
            long_quote.bid_liquidity_usd < required_depth
            or long_quote.ask_liquidity_usd < self.notional_usd
            or short_quote.ask_liquidity_usd < required_depth
            or short_quote.bid_liquidity_usd < self.notional_usd
        ):
            return None

        quantity = min(
            self.notional_usd / long_quote.ask,
            self.notional_usd / short_quote.bid,
        )
        expected_funding = quantity * (
            short_quote.bid * short_quote.funding_per_hour
            - long_quote.ask * long_quote.funding_per_hour
        ) * self.hold_hours
        spread_cost = quantity * (
            long_quote.ask - long_quote.bid + short_quote.ask - short_quote.bid
        )
        fee_cost = quantity * (
            (long_quote.ask + long_quote.bid) * long_quote.taker_fee
            + (short_quote.bid + short_quote.ask) * short_quote.taker_fee
        )
        round_trip_cost = spread_cost + fee_cost
        long_mid = (long_quote.bid + long_quote.ask) / 2
        short_mid = (short_quote.bid + short_quote.ask) / 2
        reference_mid = (long_mid + short_mid) / 2
        if abs(long_mid - short_mid) / reference_mid > self.max_cross_venue_deviation_pct:
            return None
        basis_reserve = (
            max(ZERO, long_mid - short_mid) * quantity
            + self.notional_usd * self.basis_buffer_pct
        )
        cash_pnl = expected_funding - round_trip_cost - basis_reserve
        subsidy = max(ZERO, long_quote.farming_subsidy_usd) + max(
            ZERO, short_quote.farming_subsidy_usd
        )
        if cash_pnl < -self.max_cash_loss_usd:
            return None
        return FarmingRoute(
            symbol=long_quote.symbol,
            long_venue=long_quote.venue,
            short_venue=short_quote.venue,
            notional_usd=self.notional_usd,
            quantity=quantity,
            expected_funding_usd=expected_funding,
            round_trip_cost_usd=round_trip_cost,
            basis_reserve_usd=basis_reserve,
            expected_cash_pnl_usd=cash_pnl,
            farming_subsidy_usd=subsidy,
            objective_usd=cash_pnl + subsidy,
        )
