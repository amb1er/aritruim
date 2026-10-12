from decimal import Decimal as D

from src.farming.optimizer import FarmingOptimizer, VenueQuote


def quote(venue, funding, *, subsidy="0", depth="10000"):
    return VenueQuote(
        venue=venue,
        symbol="BTC",
        funding_per_hour=D(funding),
        bid=D("99.99"),
        ask=D("100.01"),
        taker_fee=D("0.0001"),
        bid_liquidity_usd=D(depth),
        ask_liquidity_usd=D(depth),
        farming_subsidy_usd=D(subsidy),
    )


def test_optimizer_selects_low_funding_long_and_high_funding_short():
    optimizer = FarmingOptimizer(
        notional_usd=D("100"), hold_hours=D("8"), max_cash_loss_usd=D("1")
    )
    route = optimizer.best(
        [
            quote("nado", "-0.0001"),
            quote("entropy", "0.0002"),
            quote("polymarket_perps", "0.0005"),
            quote("txflow", "0.0001"),
        ]
    )
    assert route is not None
    assert route.long_venue == "nado"
    assert route.short_venue == "polymarket_perps"
    assert route.expected_funding_usd > 0


def test_farming_subsidy_never_hides_cash_loss_limit():
    optimizer = FarmingOptimizer(
        notional_usd=D("100"), hold_hours=D("1"), max_cash_loss_usd=D("0.05")
    )
    # A large subjective points budget cannot authorize a route whose actual
    # expected cash loss exceeds the independently configured limit.
    assert optimizer.best(
        [quote("nado", "0", subsidy="100"), quote("txflow", "0", subsidy="100")]
    ) is None


def test_route_requires_double_exit_depth():
    optimizer = FarmingOptimizer(
        notional_usd=D("100"), hold_hours=D("8"), max_cash_loss_usd=D("10")
    )
    assert optimizer.best(
        [quote("nado", "-0.001", depth="150"), quote("txflow", "0.001")]
    ) is None
