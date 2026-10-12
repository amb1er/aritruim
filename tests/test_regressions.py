"""Regression tests for cash accounting and incomplete execution using real SQLite."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.core.executor import Executor
from src.core.pnl_tracker import PnlTracker
from src.core.risk import RiskManager
from src.core.scanner import FundingScanner
from src.db import Database
from src.models import (
    ArbitragePosition,
    BotMode,
    FeeRate,
    FundingRate,
    FundingSpread,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderResult,
    OrderSide,
    PositionStatus,
    utc_now,
)


def book(exchange="a", bid="99.99", ask="100.01", size="1000"):
    return Orderbook(
        exchange, "BTC", [OrderbookLevel(D(bid), D(size))], [OrderbookLevel(D(ask), D(size))]
    )


def fill(exchange, size="1", price="100", success=True, fee="0.05", uncertain=False):
    return OrderResult(
        exchange,
        "BTC",
        "fill-id",
        OrderSide.SHORT,
        D(size),
        D(price),
        D(fee),
        success,
        "" if success else "rejected",
        uncertain=uncertain,
    )


def spread():
    return FundingSpread("BTC", "a", "b", D("0.0001"), D("0.0011"), D("0.001"), D("0.006"))


@pytest.fixture
async def system(tmp_path):
    cfg = Settings(
        _env_file=None,
        bot_mode=BotMode.PAPER,
        funding_spike_lookback_min=0,
        funding_history_min_hours=D("0"),
        telegram_token="",
        telegram_chat_id="",
    )
    adapters = {}
    for name in ("a", "b"):
        adapter = MagicMock()
        adapter.name = name
        adapter.funding_model = "index"
        adapter.get_funding_indices = AsyncMock(return_value=(D("0"), D("0")))
        adapter.get_funding_rate = AsyncMock(
            return_value=FundingRate(
                name, "BTC", D("0.0001") if name == "a" else D("0.0011"), 1, None
            )
        )
        adapter.get_orderbook = AsyncMock(return_value=book(name))
        adapter.get_mark_price = AsyncMock(return_value=D("100"))
        adapter.get_fee_rate = AsyncMock(return_value=FeeRate(name, "BTC", D("0"), D("0.0005")))
        adapter.close_position = AsyncMock()
        adapter.place_order = AsyncMock()
        adapters[name] = adapter
    async with Database(tmp_path / "regression.db") as db:
        notifier = MagicMock()
        notifier.on_position_opened = AsyncMock()
        notifier.on_position_closed = AsyncMock()
        notifier.on_error = AsyncMock()
        risk = RiskManager(cfg)
        executor = Executor(adapters, risk, db, notifier, cfg)
        tracker = PnlTracker(adapters, risk, db, cfg)
        tracker.set_executor(executor)
        yield cfg, adapters, db, executor, tracker


async def open_paper(system):
    await system[3]._handle_opportunity(spread())
    return next(iter(system[3].open_positions.values()))


@pytest.mark.asyncio
async def test_missing_funding_waits_but_has_bounded_exit(system):
    pos = await open_paper(system)
    tracker = system[4]
    with patch("src.core.pnl_tracker.time.monotonic", return_value=100):
        assert (await tracker._check_position(pos, {}))[0] is False
    with patch("src.core.pnl_tracker.time.monotonic", return_value=281):
        assert await tracker._check_position(pos, {}) == (True, "funding_data_unavailable")


@pytest.mark.asyncio
async def test_actual_price_stop_is_not_delayed_by_missing_funding(system):
    pos = await open_paper(system)
    system[1]["b"].get_mark_price.return_value = D("110")
    close, reason = await system[4]._check_position(pos, {})
    assert close and "delta stop-loss" in reason


@pytest.mark.asyncio
async def test_entry_requires_consecutive_profitable_scans(system):
    cfg, adapters, _, _, _ = system
    cfg.entry_confirmation_scans = 3
    cfg.symbols = "BTC"
    for name, adapter in adapters.items():
        adapter.get_funding_rates = AsyncMock(
            return_value=[FundingRate(name, "BTC", D("0") if name == "a" else D("0.001"), 1, None)]
        )
    scanner = FundingScanner(list(adapters.values()), cfg)
    await scanner._scan_once()
    await scanner._scan_once()
    assert scanner.opportunities.empty()
    adapters["b"].get_funding_rates.return_value = []
    await scanner._scan_once()
    adapters["b"].get_funding_rates.return_value = [FundingRate("b", "BTC", D("0.001"), 1, None)]
    await scanner._scan_once()
    await scanner._scan_once()
    assert scanner.opportunities.empty()
    await scanner._scan_once()
    assert scanner.opportunities.qsize() == 1


@pytest.mark.asyncio
async def test_changed_funding_is_revalidated_before_any_order(system):
    cfg, adapters, _, executor, _ = system
    adapters["b"].get_funding_rate.return_value = FundingRate("b", "BTC", D("-0.001"), 1, None)
    await executor._handle_opportunity(spread())
    assert not executor.open_positions
    for adapter in adapters.values():
        adapter.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_fees_cannot_authorize_entry(system):
    system[1]["b"].get_fee_rate.side_effect = RuntimeError("fees unavailable")
    await system[3]._handle_opportunity(spread())
    assert not system[3].open_positions


async def live_position(system):
    cfg, adapters, db, executor, _ = system
    cfg.bot_mode = BotMode.LIVE
    pos = ArbitragePosition(
        "live",
        "BTC",
        "a",
        "b",
        D("100"),
        D("1"),
        long_order=fill("a"),
        short_order=fill("b"),
        status=PositionStatus.OPEN,
        mode=BotMode.LIVE,
        long_remaining=D("1"),
        short_remaining=D("1"),
    )
    executor.open_positions[pos.id] = pos
    await db.save_position(pos)
    return pos


def test_slippage_includes_spread_and_rejects_insufficient_depth():
    ob = book(bid="90", ask="110", size="1")
    assert ob.estimate_slippage(OrderSide.LONG, D("100")) == D("0.1")
    assert ob.estimate_slippage(OrderSide.LONG, D("1000")) == 1
    ob.asks.append(OrderbookLevel(D("150"), D("10")))
    assert ob.estimate_slippage(OrderSide.LONG, D("260")) == D("0.3")


@pytest.mark.asyncio
async def test_failed_closes_remain_tracked(system):
    pos = await live_position(system)
    for a in system[1].values():
        a.close_position.return_value = fill(a.name, size="0", price="0", success=False, fee="0")
    await system[3].close_position(pos, "test")
    assert pos.status == PositionStatus.CLOSING
    assert pos.id in system[3].open_positions
    assert len(await system[2].get_open_positions()) == 1
    assert pos.closed_at is None


@pytest.mark.asyncio
async def test_partial_close_retries_only_remaining_leg_and_accounts_once(system):
    pos = await live_position(system)
    adapters, executor, db = system[1], system[3], system[2]
    adapters["a"].close_position.return_value = fill("a", price="110")
    adapters["b"].close_position.side_effect = [
        fill("b", size="0.4", price="105"),
        fill("b", size="0.6", price="105"),
    ]
    await executor.close_position(pos, "test")
    assert pos.long_remaining == 0 and pos.short_remaining == D("0.6")
    assert pos.status == PositionStatus.CLOSING
    await executor.close_position(pos, "retry")
    assert pos.status == PositionStatus.CLOSED
    assert adapters["a"].close_position.await_count == 1
    assert pos.realized_price_pnl == D("5")
    assert pos.fees_paid == D("0.15")
    assert (await db.get_daily_pnl())["net_pnl"] == D("4.85")


@pytest.mark.asyncio
async def test_exception_during_close_does_not_mark_closed(system):
    pos = await live_position(system)
    system[1]["a"].close_position.side_effect = TimeoutError("unknown response")
    system[1]["b"].close_position.return_value = fill("b")
    await system[3].close_position(pos, "test")
    assert pos.needs_reconciliation
    assert pos.id in system[3].open_positions
    count = system[1]["a"].close_position.await_count
    await system[3].close_position(pos, "retry")
    assert system[1]["a"].close_position.await_count == count


@pytest.mark.asyncio
async def test_fill_persistence_failure_requires_reconciliation(system):
    pos = await live_position(system)
    for adapter in system[1].values():
        adapter.close_position.return_value = fill(adapter.name)
    with (
        patch.object(system[2], "record_fill", new=AsyncMock(side_effect=OSError("disk full"))),
        pytest.raises(OSError, match="disk full"),
    ):
        await system[3].close_position(pos, "test")
    assert pos.needs_reconciliation
    assert pos.id in system[3].open_positions
    assert pos.status != PositionStatus.CLOSED


@pytest.mark.asyncio
async def test_parallel_close_calls_do_not_duplicate_orders(system):
    pos = await live_position(system)
    for a in system[1].values():
        a.close_position.return_value = fill(a.name)
    await asyncio.gather(*(system[3].close_position(pos, "test") for _ in range(3)))
    for a in system[1].values():
        a.close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_open_is_not_retried(system):
    adapter = system[1]["a"]
    adapter.place_order.side_effect = TimeoutError("unknown")
    req = OrderRequest("a", "BTC", OrderSide.LONG, D("100"), D("1"))
    result = await system[3]._place_with_retry(adapter, req)
    assert result.uncertain and not result.success
    adapter.place_order.assert_awaited_once()
    assert req.client_id


@pytest.mark.asyncio
async def test_unequal_fills_roll_back_instead_of_claiming_hedge(system):
    cfg, adapters, db, executor, _ = system
    cfg.bot_mode = BotMode.LIVE
    for name, adapter in adapters.items():
        from src.models import Balance

        adapter.get_balance = AsyncMock(return_value=Balance(name, D("10000"), D("10000")))
    adapters["a"].place_order.return_value = fill("a", size="10")
    adapters["b"].place_order.return_value = fill("b", size="6")
    adapters["a"].close_position.return_value = fill("a", size="10")
    adapters["b"].close_position.return_value = fill("b", size="6")
    await executor._handle_opportunity(spread())
    assert not executor.open_positions
    (pos,) = await db.get_all_positions()
    assert pos.status == PositionStatus.CLOSED
    assert pos.fees_paid == D("0.2")


@pytest.mark.asyncio
async def test_paper_balance_and_quantity_hedge_survive_restart(system):
    cfg, adapters, db, executor, _ = system
    before = await executor._paper_balance("a")
    pos = await open_paper(system)
    assert pos.long_order.filled_size == pos.short_order.filled_size
    after = await executor._paper_balance("a")
    assert after.available_usd < before.available_usd - D("900")
    restored = Executor(adapters, RiskManager(cfg), db, executor._notifier, cfg)
    await restored.restore()
    assert (await restored._paper_balance("a")).available_usd == after.available_usd
    await restored.close_all()
    ledger = await db.get_ledger_totals(exchange="a", mode=BotMode.PAPER)
    assert (await restored._paper_balance("a")).available_usd == cfg.paper_balance_usd + ledger[
        "net_pnl"
    ]
    assert ledger["net_pnl"] < 0


@pytest.mark.asyncio
async def test_paper_partial_close_restores_remaining_quantities(system):
    cfg, adapters, db, executor, _ = system
    pos = await open_paper(system)
    quantity = pos.long_remaining
    adapters["a"].get_orderbook.return_value = book("a", size=str(quantity / 2))
    await executor.close_position(pos, "partial")
    assert pos.long_remaining > 0 and pos.short_remaining == 0
    restored = Executor(adapters, RiskManager(cfg), db, executor._notifier, cfg)
    await restored.restore()
    saved = restored.open_positions[pos.id]
    adapters["b"].get_orderbook.reset_mock()
    adapters["a"].get_orderbook.return_value = book("a")
    await restored.close_position(saved, "retry")
    assert saved.status == PositionStatus.CLOSED
    adapters["b"].get_orderbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_funding_uses_index_change_and_does_not_double_accrue(system):
    _, adapters, db, _, tracker = system
    pos = await open_paper(system)
    now = utc_now()
    pos.last_funding_at = now - timedelta(minutes=30)
    rates = {
        "a": {"BTC": FundingRate("a", "BTC", D("0.0001"), 1, now)},
        "b": {"BTC": FundingRate("b", "BTC", D("0.0048"), 24, now)},
    }
    for name, index in (("a", "0.01"), ("b", "0.02")):
        adapters[name].funding_model = "index"
        adapters[name].get_funding_indices = AsyncMock(return_value=(D(index), D(index)))
    pos.funding_state = {leg: {"index": "0"} for leg in ("long", "short")}
    expected = pos.short_order.filled_size * D("0.02") - pos.long_order.filled_size * D("0.01")
    with patch("src.core.pnl_tracker.utc_now", return_value=now):
        assert await tracker._accrue_funding(pos, rates) == expected
        assert await tracker._accrue_funding(pos, rates) == 0
    assert await db.get_total_funding(pos.id) == expected
    (saved,) = await db.get_open_positions()
    assert saved.last_funding_at == now
    assert saved.funding_received == expected


@pytest.mark.asyncio
async def test_legacy_naive_dates_are_normalized(system):
    pos = ArbitragePosition(
        "old", "BTC", "a", "b", D("100"), D("1"), opened_at=datetime(2020, 1, 1)
    )
    await system[2].save_position(pos)
    (saved,) = await system[2].get_open_positions()
    assert (utc_now() - saved.opened_at).total_seconds() > 0
    assert saved.opened_at.tzinfo == UTC


@pytest.mark.asyncio
async def test_snapshots_do_not_multiply_daily_cash_flow(system):
    _, _, db, executor, _ = system
    pos = await open_paper(system)
    await db.log_funding(pos.id, "a", "BTC", "long", D("0.01"), D("2"))
    await executor.close_position(pos, "test")
    expected = await db.get_daily_pnl()
    for _ in range(10):
        await db.save_pnl_snapshot(0, 1, D("2"), D("1"), D("1"))
    assert await db.get_daily_pnl() == expected
    assert expected["funding_received"] == D("2")
    assert expected["realized_price_pnl"] < 0
    assert await db.get_daily_pnl("2000-01-01") == {
        "date": "2000-01-01",
        "funding_received": D("0"),
        "fees_paid": D("0"),
        "realized_price_pnl": D("0"),
        "net_pnl": D("0"),
    }


@pytest.mark.asyncio
async def test_funding_transaction_rolls_back_if_state_save_fails(system):
    _, _, db, _, tracker = system
    pos = await open_paper(system)
    pos.last_funding_at = utc_now() - timedelta(hours=1)
    old_time = pos.last_funding_at
    rates = {
        name: {"BTC": FundingRate(name, "BTC", D("0.001"), 1, utc_now())} for name in ("a", "b")
    }
    with (
        patch.object(db, "save_position", new=AsyncMock(side_effect=RuntimeError("disk error"))),
        pytest.raises(RuntimeError, match="disk error"),
    ):
        await tracker._accrue_funding(pos, rates)
    assert await db.get_total_funding(pos.id) == 0
    assert pos.last_funding_at == old_time
    assert pos.funding_received == 0


@pytest.mark.asyncio
async def test_live_funding_cannot_use_paper_estimates(system):
    system[0].bot_mode = BotMode.LIVE
    pos = await live_position(system)
    with pytest.raises(RuntimeError, match="confirmed exchange payments"):
        await system[4]._accrue_funding(pos, {})


@pytest.mark.asyncio
async def test_mixed_funding_intervals_have_correct_direction_and_magnitude(system):
    cfg, adapters, _, _, _ = system
    cfg.min_funding_spread = D("0.000001")
    scanner = FundingScanner(list(adapters.values()), cfg)
    now = utc_now()
    found = await scanner._find_spreads(
        {"BTC"},
        {"BTC": FundingRate("a", "BTC", D("0.0001"), 1, now)},
        {"BTC": FundingRate("b", "BTC", D("0.0006"), 8, now)},
    )
    (signal,) = found
    assert signal.long_exchange == "b"
    assert signal.short_exchange == "a"
    assert signal.spread == D("0.000025")
    assert signal.estimated_net_pnl == D("0.000025") * cfg.expected_hold_hours - D("0.002")


@pytest.mark.asyncio
async def test_zero_interval_is_ignored(system):
    scanner = FundingScanner(list(system[1].values()), system[0])
    rate = FundingRate("a", "BTC", D("1"), 0, utc_now())
    assert await scanner._find_spreads({"BTC"}, {"BTC": rate}, {"BTC": rate}) == []


@pytest.mark.asyncio
async def test_dry_run_never_submits_and_stale_signals_are_ignored(system):
    cfg, adapters, _, executor, _ = system
    cfg.bot_mode = BotMode.DRY_RUN
    await executor._handle_opportunity(spread())
    cfg.bot_mode = BotMode.PAPER
    from dataclasses import replace

    stale = replace(spread(), timestamp=utc_now() - timedelta(hours=1))
    await executor._handle_opportunity(stale)
    assert not executor.open_positions
    for a in adapters.values():
        a.place_order.assert_not_awaited()
        a.get_orderbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_prices_trigger_exit_instead_of_zero_risk(system):
    pos = await open_paper(system)
    system[1]["a"].get_mark_price.side_effect = RuntimeError("offline")
    assert await system[4]._calc_delta(pos, {}) is None


@pytest.mark.asyncio
async def test_live_start_fails_before_connecting_or_sending_orders(system):
    from src.main import run

    system[0].bot_mode = BotMode.LIVE
    with (
        patch("src.main.get_settings", return_value=system[0]),
        patch("src.main.HyperliquidAdapter") as hl,
    ):
        with pytest.raises(RuntimeError, match="disabled"):
            await run()
        hl.assert_not_called()


@pytest.mark.asyncio
async def test_reentry_cooldown_survives_restart_and_expires(system):
    cfg, adapters, db, executor, _ = system
    pos = await open_paper(system)
    await executor.close_position(pos, "test_exit")
    assert (await db.get_all_positions())[0].close_reason == "test_exit"
    restored = Executor(adapters, RiskManager(cfg), db, executor._notifier, cfg)
    await restored.restore()
    await restored._handle_opportunity(spread())
    assert not restored.open_positions
    assert len(await db.get_all_positions()) == 1
    assert await db.get_last_closed_at("BTC", BotMode.LIVE) is None
    assert await db.get_last_closed_at("ETH", BotMode.PAPER) is None
    expiry = pos.closed_at + timedelta(seconds=cfg.reentry_cooldown_sec)
    signal = spread()
    signal = FundingSpread(
        signal.symbol,
        signal.long_exchange,
        signal.short_exchange,
        signal.long_rate,
        signal.short_rate,
        signal.spread,
        signal.estimated_net_pnl,
        timestamp=expiry,
    )
    with patch("src.core.executor.utc_now", return_value=expiry):
        await restored._handle_opportunity(signal)
    assert len(restored.open_positions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_mark", [D("103"), D("NaN"), D("0")])
async def test_entry_rejects_monitor_stop_condition(system, bad_mark):
    _, adapters, db, executor, _ = system
    adapters["b"].get_mark_price.return_value = bad_mark
    await executor._handle_opportunity(spread())
    assert not executor.open_positions
    assert not await db.get_all_positions()
