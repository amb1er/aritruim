from datetime import timedelta
from decimal import Decimal as D

from src.config import Settings
from src.farming.engine import FarmingEngine, PaperPosition
from src.farming.optimizer import FarmingRoute
from src.models import utc_now


def route() -> FarmingRoute:
    return FarmingRoute(
        symbol="BTC",
        long_venue="nado",
        short_venue="txflow",
        notional_usd=D("100"),
        quantity=D("1"),
        expected_funding_usd=D("1"),
        round_trip_cost_usd=D("0.10"),
        basis_reserve_usd=D("0.05"),
        expected_cash_pnl_usd=D("0.85"),
        farming_subsidy_usd=D("0"),
        objective_usd=D("0.85"),
    )


def test_funding_forecast_requires_persistent_positive_history():
    settings = Settings(
        _env_file=None,
        farming_funding_history_min_minutes=10,
        farming_funding_forecast_discount="0.75",
        farming_expected_hold_hours="8",
        farming_max_payback_hours="4",
    )
    engine = FarmingEngine({}, settings)
    now = utc_now()
    key = ("BTC", "nado", "txflow")
    engine._funding_history[key] = [
        (now - timedelta(minutes=10), D("0.0010")),
        (now - timedelta(minutes=5), D("0.0008")),
        (now, D("0.0012")),
    ]

    forecasted = engine._apply_funding_forecast([route()])

    assert len(forecasted) == 1
    assert forecasted[0].expected_funding_usd == D("0.48")


def test_funding_forecast_rejects_recent_direction_reversal():
    settings = Settings(_env_file=None, farming_funding_history_min_minutes=10)
    engine = FarmingEngine({}, settings)
    now = utc_now()
    engine._funding_history[("BTC", "nado", "txflow")] = [
        (now - timedelta(minutes=10), D("0.001")),
        (now, D("-0.0001")),
    ]

    assert engine._apply_funding_forecast([route()]) == []


def test_paper_funding_is_integrated_incrementally_without_repricing_past_hours():
    opened = utc_now()
    position = PaperPosition(
        id="position",
        symbol="BTC",
        long_venue="nado",
        short_venue="txflow",
        quantity=D("1"),
        notional_usd=D("100"),
        long_entry=D("100"),
        short_entry=D("100"),
        entry_fees=D("0"),
        opened_at=opened,
        expected_cash_pnl=D("1"),
        funding_updated_at=opened,
    )

    FarmingEngine._accrue_paper_funding(
        position, D("0.001"), opened + timedelta(hours=1), True
    )
    FarmingEngine._accrue_paper_funding(
        position, D("-0.0005"), opened + timedelta(hours=2), True
    )

    assert position.accrued_funding == D("0.05")


def test_missing_rate_advances_clock_without_backfilling_current_rate():
    opened = utc_now()
    position = PaperPosition(
        id="position",
        symbol="BTC",
        long_venue="nado",
        short_venue="txflow",
        quantity=D("1"),
        notional_usd=D("100"),
        long_entry=D("100"),
        short_entry=D("100"),
        entry_fees=D("0"),
        opened_at=opened,
        expected_cash_pnl=D("1"),
        funding_updated_at=opened,
    )

    FarmingEngine._accrue_paper_funding(
        position, D("0"), opened + timedelta(hours=1), False
    )
    FarmingEngine._accrue_paper_funding(
        position, D("0.001"), opened + timedelta(hours=2), True
    )

    assert position.accrued_funding == D("0.10")
