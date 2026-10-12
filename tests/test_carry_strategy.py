"""Economics regressions: fee churn, causal history and settlement accounting."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.core.funding_accounting import observe_leg, paid_hours_within
from src.core.funding_history import FundingHistory
from src.core.pnl_tracker import PnlTracker
from src.core.risk import RiskManager
from src.db import Database
from src.models import (
    ArbitragePosition,
    Balance,
    FeeRate,
    FundingRate,
    FundingSpread,
    Orderbook,
    OrderbookLevel,
    OrderResult,
    OrderSide,
    PositionStatus,
    utc_now,
)


def position():
    p = ArbitragePosition("p", "BTC", "a", "b", D("1000"), D("1"), status=PositionStatus.OPEN)
    p.long_order = OrderResult("a", "BTC", "l", OrderSide.LONG, D("10"), D("100"), D("0.5"), True)
    p.short_order = OrderResult("b", "BTC", "s", OrderSide.SHORT, D("10"), D("100"), D("0.5"), True)
    p.fees_paid = D("1")
    return p


def book(name="a", bid="99.99", ask="100.01", size="100"):
    return Orderbook(
        name, "BTC", [OrderbookLevel(D(bid), D(size))], [OrderbookLevel(D(ask), D(size))]
    )


def observe(history, start, n, short="0.001", long="0"):
    for index in range(n):
        now = start + timedelta(minutes=5 * index)
        history.observe(
            {
                name: {"BTC": FundingRate(name, "BTC", D(value), 1, None, now)}
                for name, value in (("a", long), ("b", short))
            },
            now,
        )
    return now


def test_scan_count_cannot_replace_elapsed_history():
    history = FundingHistory(Settings(_env_file=None))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(100):
        observe(history, now, 1)
    assert history.forecast("BTC", "a", "b", D("0.001"), now)[3] == "history_warmup"


def test_warmup_does_not_shorten_planned_holding_period():
    history = FundingHistory(Settings(_env_file=None))
    now = observe(history, datetime(2026, 1, 1, tzinfo=UTC), 13)
    forecast, horizon, hours, reason = history.forecast("BTC", "a", "b", D("0.005"), now)
    assert (forecast, horizon, hours, reason) == (D("0.00075"), D("12"), D("1"), "")


def test_signed_history_rejects_direction_flip():
    history = FundingHistory(Settings(_env_file=None))
    now = observe(history, datetime(2026, 1, 1, tzinfo=UTC), 13)
    assert history.forecast("BTC", "b", "a", D("0.001"), now)[3] == "funding_direction_unstable"


def test_missing_bucket_resets_usable_history():
    history = FundingHistory(Settings(_env_file=None))
    now = observe(history, datetime(2026, 1, 1, tzinfo=UTC), 13)
    now = observe(history, now + timedelta(minutes=10), 1)
    assert history.forecast("BTC", "a", "b", D("0.001"), now)[3] == "history_warmup"


def test_old_direction_flip_does_not_poison_a_new_stable_period():
    history = FundingHistory(Settings(_env_file=None))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observe(history, start, 2, short="-0.001")
    now = observe(history, start + timedelta(minutes=10), 13)
    forecast, horizon, hours, reason = history.forecast("BTC", "a", "b", D("0.001"), now)
    assert reason == ""
    assert forecast == D("0.00075")
    assert horizon == D("12")
    assert hours == D("1")


def test_sustained_recent_rate_replaces_old_low_regime():
    history = FundingHistory(Settings(_env_file=None))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observe(history, start, 120, short="0.00001")
    now = observe(history, start + timedelta(hours=10), 25, short="0.001")
    assert history.forecast("BTC", "a", "b", D("0.001"), now)[0] == D("0.00075")
    # A sudden fall is reflected immediately, even with a high recent quartile.
    assert history.forecast("BTC", "a", "b", D("0.00001"), now)[0] == D("0.0000075")


def test_one_spike_cannot_raise_forecast_and_max_hold_caps_horizon():
    history = FundingHistory(Settings(_env_file=None, max_hold_hours=4))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    now = observe(history, start, 25, short="0.00001")
    now = observe(history, now + timedelta(minutes=5), 1, short="0.01")
    forecast, horizon, _, reason = history.forecast("BTC", "a", "b", D("0.01"), now)
    assert (forecast, horizon, reason) == (D("0.0000075"), D("4"), "")


def test_recent_flip_still_requires_full_positive_warmup():
    history = FundingHistory(Settings(_env_file=None))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observe(history, start, 13)
    observe(history, start + timedelta(minutes=65), 1, short="0")
    now = observe(history, start + timedelta(minutes=70), 12)
    assert history.forecast("BTC", "a", "b", D("0.001"), now)[3] == "funding_direction_unstable"


def test_unknown_or_stale_rates_never_count_as_history():
    cfg = Settings(_env_file=None)
    history = FundingHistory(cfg)
    now = utc_now()
    stale = now - timedelta(seconds=cfg.max_funding_age_sec + 1)
    rows = history.observe({"a": {"BTC": FundingRate("a", "BTC", D("1"), 1, None, stale)}}, now)
    assert rows == []


def test_forecast_counts_payment_schedule_not_quote_normalization():
    now = datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    rate = FundingRate("b", "BTC", D("0.008"), 8, now + timedelta(hours=6))
    assert paid_hours_within(rate, "period_history", D("1"), now) == 0
    assert paid_hours_within(rate, "period_history", D("8"), now) == 8
    assert paid_hours_within(rate, "hourly_history", D("1"), now) == 1
    assert paid_hours_within(rate, "index", D("0.5"), now) == D("0.5")


@pytest.mark.asyncio
async def test_profitable_rate_without_payment_in_horizon_is_rejected():
    cfg = Settings(_env_file=None)
    signal = FundingSpread("BTC", "a", "b", D("0"), D("0.01"), D("0.01"), D("0.07"))
    balance = Balance("a", D("10000"), D("10000"))
    can, reason = await RiskManager(cfg).can_open(
        signal,
        0,
        balance,
        balance,
        book(),
        book("b"),
        fees=(D("0.0005"), D("0.0005")),
        funding_hours=(D("0"), D("0")),
    )
    assert not can and "round-trip" in reason


@pytest.mark.asyncio
async def test_history_survives_database_reopen(tmp_path):
    cfg = Settings(_env_file=None)
    history = FundingHistory(cfg)
    now = utc_now()
    rows = history.observe({"a": {"BTC": FundingRate("a", "BTC", D("0.0024"), 24, None)}}, now)
    async with Database(tmp_path / "history.db") as db:
        await db.save_funding_observations(rows, cfg)
    async with Database(tmp_path / "history.db") as db:
        assert await db.load_funding_observations() == history.samples


@pytest.mark.asyncio
async def test_entry_rejects_nominal_profit_without_cost_safety_margin():
    cfg = Settings(_env_file=None, entry_cost_coverage=D("1.5"))
    risk = RiskManager(cfg)
    signal = FundingSpread(
        "BTC",
        "a",
        "b",
        D("0"),
        D("0.00032"),
        D("0.00032"),
        D("0.00056"),
        forecast_spread=D("0.00032"),
        forecast_hours=D("8"),
    )
    balance = Balance("a", D("10000"), D("10000"))
    can, reason = await risk.can_open(
        signal, 0, balance, balance, book(), book("b"), fees=(D("0.0005"), D("0.0005"))
    )
    # About $2.56 forecast vs $2.40 costs: positive net, insufficient 1.5x cover.
    assert not can and "round-trip" in reason


@pytest.mark.asyncio
async def test_default_entry_buffer_rejects_six_percent_cost_margin():
    cfg = Settings(_env_file=None)
    signal = FundingSpread(
        "BTC",
        "a",
        "b",
        D("0"),
        D("0.00032"),
        D("0.00032"),
        D("0.00056"),
        forecast_spread=D("0.00032"),
        forecast_hours=D("8"),
    )
    balance = Balance("a", D("10000"), D("10000"))
    can, reason = await RiskManager(cfg).can_open(
        signal, 0, balance, balance, book(), book("b"), fees=(D("0.0005"), D("0.0005"))
    )
    assert not can and "round-trip" in reason


@pytest.mark.asyncio
async def test_entry_rejects_payback_too_close_to_forecast_horizon():
    cfg = Settings(
        _env_file=None,
        entry_cost_coverage=D("1"),
        min_net_pnl_after_fees=D("0"),
        basis_risk_buffer_pct=D("0"),
        max_entry_payback_fraction=D("0.4"),
        max_slippage=D("1"),
    )
    signal = FundingSpread(
        "BTC", "a", "b", D("0"), D("0.0001"), D("0.0001"), D("0.0004"),
        forecast_spread=D("0.0001"), forecast_hours=D("8")
    )
    balance = Balance("a", D("10000"), D("10000"))
    can, reason = await RiskManager(cfg).can_open(
        signal, 0, balance, balance, book(), book("b"), fees=(D("0"), D("0"))
    )
    assert not can and "round-trip" in reason


@pytest.mark.asyncio
async def test_basis_risk_buffer_blocks_thin_expected_edge():
    cfg = Settings(
        _env_file=None,
        entry_cost_coverage=D("1"),
        min_net_pnl_after_fees=D("0"),
        basis_risk_buffer_pct=D("0.0005"),
        max_entry_payback_fraction=D("1"),
        max_slippage=D("1"),
    )
    signal = FundingSpread(
        "BTC", "a", "b", D("0"), D("0.0001"), D("0.0001"), D("0.0004"),
        forecast_spread=D("0.0001"), forecast_hours=D("8")
    )
    balance = Balance("a", D("10000"), D("10000"))
    can, reason = await RiskManager(cfg).can_open(
        signal, 0, balance, balance, book(), book("b"), fees=(D("0"), D("0"))
    )
    assert not can and "round-trip" in reason


@pytest.mark.asyncio
async def test_entry_size_is_reduced_to_available_low_slippage_depth():
    cfg = Settings(
        _env_file=None,
        max_slippage=D("0.001"),
        min_position_usd=D("50"),
        min_net_pnl_after_fees=D("0"),
    )
    thin = Orderbook(
        "a",
        "BTC",
        [OrderbookLevel(D("99.99"), D("2")), OrderbookLevel(D("99"), D("100"))],
        [OrderbookLevel(D("100.01"), D("2")), OrderbookLevel(D("101"), D("100"))],
    )
    signal = FundingSpread(
        "BTC",
        "a",
        "b",
        D("0"),
        D("0.01"),
        D("0.01"),
        D("0.08"),
        forecast_spread=D("0.01"),
        forecast_hours=D("8"),
    )
    balance = Balance("a", D("10000"), D("10000"))
    allowed, reason, notional = await RiskManager(cfg).assess_open(
        signal,
        0,
        balance,
        Balance("b", D("10000"), D("10000")),
        thin,
        Orderbook("b", "BTC", thin.bids, thin.asks),
        fees=(D("0"), D("0")),
    )
    assert allowed, reason
    assert D("50") <= notional < D("1000")
    assert (
        thin.estimate_slippage(OrderSide.LONG, notional)
        + thin.estimate_slippage(OrderSide.SHORT, notional)
        <= cfg.max_slippage
    )


@pytest.mark.asyncio
async def test_entry_rejected_when_minimum_position_is_too_large_for_book():
    cfg = Settings(_env_file=None, min_position_usd=D("500"), max_slippage=D("0.001"))
    thin = Orderbook(
        "a",
        "BTC",
        [OrderbookLevel(D("99"), D("1"))],
        [OrderbookLevel(D("101"), D("1"))],
    )
    signal = FundingSpread(
        "BTC", "a", "b", D("0"), D("0.01"), D("0.01"), D("0.08"),
        forecast_spread=D("0.01"), forecast_hours=D("8")
    )
    balance = Balance("a", D("10000"), D("10000"))
    allowed, _, notional = await RiskManager(cfg).assess_open(
        signal, 0, balance, Balance("b", D("10000"), D("10000")), thin, thin,
        fees=(D("0"), D("0")),
    )
    assert not allowed
    assert notional == 0


@pytest.mark.asyncio
async def test_shallow_exit_side_blocks_entry():
    cfg = Settings(_env_file=None, max_slippage=D("1"))
    signal = FundingSpread("BTC", "a", "b", D("0"), D("0.01"), D("0.01"), D("0.07"))
    balance = Balance("a", D("10000"), D("10000"))
    long = book()
    long.bids[0] = OrderbookLevel(D("99.99"), D("0.1"))
    can, reason = await RiskManager(cfg).can_open(
        signal, 0, balance, balance, long, book("b"), fees=(D("0.0005"), D("0.0005"))
    )
    assert not can and reason == "insufficient_round_trip_liquidity"


@pytest.mark.asyncio
async def test_entry_requires_reserve_exit_depth():
    cfg = Settings(
        _env_file=None,
        max_slippage=D("1"),
        min_position_usd=D("500"),
        exit_liquidity_multiplier=D("2"),
    )
    # The requested quantity can close now, but there is no reserve if half of
    # the visible close-side liquidity disappears.
    thin = book(size="6")
    signal = FundingSpread(
        "BTC", "a", "b", D("0"), D("0.01"), D("0.01"), D("0.08"),
        forecast_spread=D("0.01"), forecast_hours=D("8")
    )
    balance = Balance("a", D("10000"), D("10000"))
    allowed, reason, notional = await RiskManager(cfg).assess_open(
        signal, 0, balance, Balance("b", D("10000"), D("10000")), thin, thin,
        fees=(D("0"), D("0")),
    )
    assert not allowed
    assert reason == "insufficient_stressed_exit_liquidity"
    assert notional == 0


def test_low_positive_carry_does_not_force_fee_loss_in_three_minutes():
    risk = RiskManager(Settings(_env_file=None))
    p = position()
    for _ in range(10):
        close, _ = risk.should_close(p, D("0.00004"), D("0"), 0.05, D("-0.2"))
        assert not close


def test_low_positive_carry_can_recover_costs():
    risk = RiskManager(Settings(_env_file=None))
    p = position()
    for _ in range(3):
        close, _ = risk.should_close(p, D("0.00004"), D("0"), 2, D("-0.1"))
    assert not close


def test_no_indefinite_wait_for_zero():
    risk = RiskManager(Settings(_env_file=None))
    p = position()
    for _ in range(3):
        close, reason = risk.should_close(p, D("0.000001"), D("0"), 2, D("-2"))
    assert close and reason == "carry_cannot_recover_costs"


def test_soft_exit_requires_profit_after_closing_fees():
    risk = RiskManager(Settings(_env_file=None))
    p = position()
    for _ in range(3):
        close, reason = risk.should_close(p, D("0.00004"), D("0"), 2, D("0.2"))
    assert close and reason == "low_funding_profitable_exit"


def test_negative_carry_timer_survives_serialization_and_overrides_min_hold():
    risk = RiskManager(Settings(_env_file=None, min_soft_hold_hours=D("12")))
    p = position()
    now = utc_now()
    with patch("src.core.risk.utc_now", return_value=now):
        assert not risk.should_close(p, D("-0.0001"), D("0"), 0.1, D("-1"))[0]
    with patch("src.core.risk.utc_now", return_value=now + timedelta(seconds=901)):
        assert risk.should_close(p, D("-0.0001"), D("0"), 0.4, D("-1")) == (
            True,
            "sustained_negative_funding",
        )


def test_net_loss_stop_overrides_positive_funding_and_min_hold():
    risk = RiskManager(Settings(_env_file=None))
    assert risk.should_close(position(), D("0.01"), D("0"), 0.01, D("-11")) == (
        True,
        "net_loss_stop",
    )


def test_net_take_profit_uses_closable_pnl_after_all_costs():
    risk = RiskManager(Settings(
        _env_file=None,
        take_profit_pct=D("0.005"),
        take_profit_confirmation_intervals=1,
    ))
    p = position()
    assert not risk.should_close(p, D("0.01"), D("0"), 0.1, D("4.99"))[0]
    assert risk.should_close(p, D("0.01"), D("0"), 0.1, D("5")) == (
        True,
        "net_take_profit",
    )


def test_default_take_profit_is_reachable_for_small_carry_position():
    risk = RiskManager(Settings(_env_file=None))
    p = position()
    p.notional_usd = D("200")
    assert not risk.should_close(p, D("0.01"), D("0"), 0.1, D("0.079"))[0]
    assert not risk.should_close(p, D("0.01"), D("0"), 0.1, D("0.08"))[0]
    assert risk.should_close(p, D("0.01"), D("0"), 0.2, D("0.08")) == (
        True,
        "net_take_profit",
    )


def test_one_tick_take_profit_spike_is_not_executed():
    risk = RiskManager(Settings(_env_file=None))
    p = position()
    assert not risk.should_close(p, D("0.01"), D("0"), 0.1, D("1"))[0]
    assert not risk.should_close(p, D("0.01"), D("0"), 0.2, D("-0.1"))[0]
    assert not risk.should_close(p, D("0.01"), D("0"), 0.3, D("1"))[0]


def test_trailing_profit_exit_locks_most_of_peak_gain():
    risk = RiskManager(
        Settings(
            _env_file=None,
            take_profit_pct=D("0"),
            trailing_profit_activation_pct=D("0.003"),
            trailing_profit_giveback_pct=D("0.30"),
        )
    )
    p = position()
    assert not risk.should_close(p, D("0.01"), D("0"), 0.1, D("4"))[0]
    assert p.peak_close_pnl == D("4")
    assert not risk.should_close(p, D("0.01"), D("0"), 0.2, D("3"))[0]
    assert risk.should_close(p, D("0.01"), D("0"), 0.3, D("2.7")) == (
        True,
        "trailing_profit_exit",
    )


@pytest.mark.asyncio
async def test_close_quote_uses_full_depth_and_does_not_double_charge_slippage():
    adapters = {}
    for name in ("a", "b"):
        adapter = MagicMock()
        adapter.get_orderbook = AsyncMock(return_value=book(name, "99", "101"))
        adapter.get_fee_rate = AsyncMock(return_value=FeeRate(name, "BTC", D("0"), D("0.001")))
        adapters[name] = adapter
    p = position()
    p.funding_received = D("3")
    tracker = PnlTracker(adapters, None, None, Settings(_env_file=None))
    assert await tracker._estimate_close_pnl(p) == D("-20")  # 3 - 1 - 20 - 2
    adapters["b"].get_orderbook.return_value = book("b", size="1")
    assert await tracker._estimate_close_pnl(p) is None


def test_exit_funding_filter_ignores_single_upward_spike_but_keeps_negative_flip():
    tracker = PnlTracker(
        {}, None, None, Settings(_env_file=None, exit_funding_spike_multiplier=D("5"))
    )
    p = position()
    for _ in range(3):
        assert tracker._filter_exit_spike(p, D("0.0004")) == D("0.0004")
    assert tracker._filter_exit_spike(p, D("0.016")) == D("0.0004")
    assert tracker._filter_exit_spike(p, D("-0.01")) == D("-0.01")


@pytest.mark.asyncio
async def test_favorable_basis_is_not_a_stop():
    adapters = {
        name: MagicMock(get_mark_price=AsyncMock(return_value=D(value)))
        for name, value in (("a", "100"), ("b", "105"))
    }
    p = position()
    p.short_order.avg_price = D("110")
    tracker = PnlTracker(adapters, None, None, Settings(_env_file=None))
    assert await tracker._calc_delta(p, {}) == 0
    assert p.unrealized_price_pnl == D("50")


@pytest.mark.asyncio
async def test_hourly_funding_not_accrued_before_settlement_and_not_duplicated():
    start = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    adapter = MagicMock(name="a")
    adapter.funding_model = "hourly_history"
    adapter.get_funding_price = AsyncMock(return_value=D("100"))
    adapter.get_funding_history = AsyncMock(return_value=[])
    _, payment, state = await observe_leg(adapter, "BTC", "short", D("10"), {}, start)
    assert payment == 0
    _, payment, state = await observe_leg(
        adapter, "BTC", "short", D("10"), state, start + timedelta(minutes=29)
    )
    assert payment == 0
    settlement = start + timedelta(minutes=30)
    adapter.get_funding_history.return_value = [
        FundingRate("a", "BTC", D("0.001"), 1, None, settlement)
    ]
    _, payment, state = await observe_leg(adapter, "BTC", "short", D("10"), state, settlement)
    assert payment == D("1")
    _, payment, _ = await observe_leg(adapter, "BTC", "short", D("10"), state, settlement)
    assert payment == 0


@pytest.mark.asyncio
async def test_scheduled_estimate_uses_previous_rate_not_new_next_period_rate():
    start = datetime(2026, 1, 1, 10, 59, tzinfo=UTC)
    event = start + timedelta(minutes=1)
    adapter = MagicMock()
    adapter.funding_model = "scheduled_estimate"
    adapter.get_funding_price = AsyncMock(return_value=D("100"))
    adapter.get_funding_rate = AsyncMock(return_value=FundingRate("a", "BTC", D("0.001"), 1, event))
    _, _, state = await observe_leg(adapter, "BTC", "long", D("10"), {}, start)
    adapter.get_funding_rate.return_value = FundingRate(
        "a", "BTC", D("0.9"), 1, event + timedelta(hours=1)
    )
    _, payment, _ = await observe_leg(adapter, "BTC", "long", D("10"), state, event)
    assert payment == D("-1")


@pytest.mark.asyncio
async def test_missing_historical_mark_is_not_replaced_with_entry_price():
    start = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    adapter = MagicMock()
    adapter.funding_model = "hourly_history"
    adapter.get_funding_price = AsyncMock(return_value=D("100"))
    _, _, state = await observe_leg(adapter, "BTC", "short", D("10"), {}, start)
    adapter.get_funding_history = AsyncMock(
        return_value=[FundingRate("a", "BTC", D("0.001"), 1, None, start + timedelta(minutes=30))]
    )
    with pytest.raises(ValueError, match="historical funding mark"):
        await observe_leg(adapter, "BTC", "short", D("10"), state, start + timedelta(hours=1))


@pytest.mark.asyncio
async def test_pair_loss_cooldown_persists_and_does_not_block_other_venues(tmp_path):
    cfg = Settings(_env_file=None)
    async with Database(tmp_path / "losses.db") as db:
        for i in range(2):
            p = position()
            p.id = str(i)
            p.net_pnl = D("-1")
            p.closed_at = utc_now()
            p.status = PositionStatus.CLOSED
            p.negative_funding_since = utc_now()
            p.peak_close_pnl = D("4.2")
            p.funding_state = {"long": {"index": "12"}}
            await db.save_position(p)
        assert await db.pair_loss_cooldown_until("BTC", "a", "b", p.mode, cfg) > utc_now()
        assert await db.pair_loss_cooldown_until("BTC", "a", "c", p.mode, cfg) is None
        restored = (await db.get_all_positions())[0]
        assert restored.negative_funding_since is not None
        assert restored.peak_close_pnl == D("4.2")
        assert restored.funding_state == p.funding_state
