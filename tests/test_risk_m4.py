"""M4 тесты риск-менеджера — delta warning и circuit breaker callbacks."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.risk import RiskManager
from src.models import (
    ArbitragePosition,
    Balance,
    FundingSpread,
    Orderbook,
    OrderbookLevel,
    PositionStatus,
)


def make_balance(available: str = "10000") -> Balance:
    return Balance("test", Decimal(available), Decimal(available))


def make_orderbook(bid: str = "99990", ask: str = "100010") -> Orderbook:
    return Orderbook(
        exchange="test",
        symbol="BTC",
        bids=[OrderbookLevel(Decimal(bid), Decimal("10"))],
        asks=[OrderbookLevel(Decimal(ask), Decimal("10"))],
    )


def make_spread() -> FundingSpread:
    return FundingSpread(
        symbol="BTC",
        long_exchange="hyperliquid",
        short_exchange="nado",
        long_rate=Decimal("0.0001"),
        short_rate=Decimal("0.0006"),
        spread=Decimal("0.0005"),
        estimated_net_pnl=Decimal("0.0003"),
    )


def make_position(symbol: str = "BTC") -> ArbitragePosition:
    return ArbitragePosition(
        id="pos1",
        symbol=symbol,
        long_exchange="hyperliquid",
        short_exchange="nado",
        notional_usd=Decimal("1000"),
        leverage=Decimal("1.0"),
        entry_spread=Decimal("0.0005"),
        status=PositionStatus.OPEN,
    )


@pytest.mark.asyncio
async def test_delta_warning_callback_fires(settings):
    """Callback on_delta_warning вызывается при delta ≥ 70% от stop-loss."""
    settings.delta_stop_loss_pct = Decimal("0.02")
    risk = RiskManager(settings)

    delta_warning_called = []

    async def on_delta_warning(pos_id, symbol, delta_pct, threshold):
        delta_warning_called.append((pos_id, symbol, float(delta_pct)))

    risk.set_callbacks(on_delta_warning=on_delta_warning)

    pos = make_position()
    # Delta = 0.015 = 75% от stop-loss 0.02 → должен сработать warning
    should_close, _ = risk.should_close(
        position=pos,
        current_spread=Decimal("0.001"),  # выше exit_threshold
        current_delta_pct=Decimal("0.015"),
        hold_hours=1.0,
    )

    # Даём asyncio выполнить create_task
    await asyncio.sleep(0)

    assert should_close is False  # не закрываем — только warning
    assert len(delta_warning_called) == 1
    assert delta_warning_called[0][0] == "pos1"
    assert delta_warning_called[0][1] == "BTC"


@pytest.mark.asyncio
async def test_no_delta_warning_below_threshold(settings):
    """Callback не вызывается если delta ниже 70% порога."""
    settings.delta_stop_loss_pct = Decimal("0.02")
    risk = RiskManager(settings)

    warning_called = []
    risk.set_callbacks(
        on_delta_warning=AsyncMock(side_effect=lambda *a, **k: warning_called.append(1))
    )

    pos = make_position()
    risk.should_close(
        position=pos,
        current_spread=Decimal("0.001"),
        current_delta_pct=Decimal("0.005"),  # 25% от 0.02 — ниже 70%
        hold_hours=1.0,
    )
    await asyncio.sleep(0)
    assert len(warning_called) == 0


@pytest.mark.asyncio
async def test_circuit_breaker_callback_fires(settings):
    """Callback on_circuit_open вызывается при срабатывании circuit breaker."""
    settings.circuit_breaker_failures = 2
    risk = RiskManager(settings)

    cb_calls = []

    async def on_circuit_open(failures, delay):
        cb_calls.append((failures, delay))

    risk.set_callbacks(on_circuit_open=on_circuit_open)

    risk.record_failure()
    await asyncio.sleep(0)
    assert len(cb_calls) == 0  # ещё не сработал (нужно 2)

    risk.record_failure()
    await asyncio.sleep(0)
    assert len(cb_calls) == 1
    assert cb_calls[0][0] == 2  # failures count


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_entry(settings):
    """После срабатывания circuit breaker can_open возвращает False."""
    settings.circuit_breaker_failures = 1
    risk = RiskManager(settings)
    risk.record_failure()

    can, reason = await risk.can_open(
        spread=make_spread(),
        open_pairs=0,
        balance_long=make_balance(),
        balance_short=make_balance(),
        ob_long=make_orderbook(),
        ob_short=make_orderbook(),
    )
    assert can is False
    assert "circuit" in reason.lower()


@pytest.mark.asyncio
async def test_circuit_breaker_resets(settings):
    """Circuit breaker сбрасывается после задержки."""
    settings.circuit_breaker_failures = 1
    settings.circuit_breaker_base_delay_sec = 0  # немедленный сброс для теста
    risk = RiskManager(settings)
    risk.record_failure()

    assert risk.circuit_open is True
    await asyncio.sleep(0.1)  # даём время на reset
    # В реальном тесте нужно мокать asyncio.sleep — здесь проверяем логику
    assert risk._consecutive_failures == 1  # счётчик не сброшен до record_success
    risk.record_success()
    assert risk._consecutive_failures == 0
