"""Тесты PnlTracker — расчёт funding, delta, условия закрытия."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.pnl_tracker import PnlTracker
from src.models import ArbitragePosition, FundingRate, OrderResult, OrderSide, PositionStatus


def make_funding_rate(exchange: str, symbol: str, rate: str, interval: int = 8) -> FundingRate:
    return FundingRate(
        exchange=exchange,
        symbol=symbol,
        rate=Decimal(rate),
        interval_hours=interval,
        next_funding_ts=datetime.now(UTC),
    )


def make_open_position(symbol: str = "BTC", hold_hours: float = 1.0) -> ArbitragePosition:
    opened = datetime.now(UTC) - timedelta(hours=hold_hours)
    return ArbitragePosition(
        id="test_pos",
        symbol=symbol,
        long_exchange="hyperliquid",
        short_exchange="nado",
        notional_usd=Decimal("1000"),
        leverage=Decimal("1.0"),
        entry_spread=Decimal("0.0005"),
        status=PositionStatus.OPEN,
        opened_at=opened,
        long_order=OrderResult(
            "hyperliquid",
            symbol,
            "l",
            OrderSide.LONG,
            Decimal("0.01"),
            Decimal("100000"),
            Decimal("0"),
            True,
        ),
        short_order=OrderResult(
            "nado",
            symbol,
            "s",
            OrderSide.SHORT,
            Decimal("0.01"),
            Decimal("100000"),
            Decimal("0"),
            True,
        ),
        funding_state={"long": {"index": "0"}, "short": {"index": "0"}},
    )


@pytest.fixture
def mock_adapters():
    hl = MagicMock()
    hl.name = "hyperliquid"
    hl.get_mark_price = AsyncMock(return_value=Decimal("100000"))
    hl.get_funding_rates = AsyncMock(
        return_value=[
            make_funding_rate("hyperliquid", "BTC", "0.0001", interval=1),
        ]
    )
    nado = MagicMock()
    nado.name = "nado"
    nado.get_mark_price = AsyncMock(return_value=Decimal("100000"))
    nado.get_funding_rates = AsyncMock(
        return_value=[
            make_funding_rate("nado", "BTC", "0.0006", interval=8),
        ]
    )
    for adapter, index in ((hl, "10"), (nado, "20")):
        adapter.funding_model = "index"
        adapter.get_funding_indices = AsyncMock(return_value=(Decimal(index), Decimal(index)))
    return {"hyperliquid": hl, "nado": nado}


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.save_position = AsyncMock()
    db.record_funding = AsyncMock()
    db.log_funding = AsyncMock()
    db.save_pnl_snapshot = AsyncMock()
    db.get_all_positions = AsyncMock(return_value=[])
    return db


@pytest.fixture
def tracker(mock_adapters, mock_db, settings):
    from src.core.risk import RiskManager

    # monitor_interval_sec=3600 (1h) — упрощает расчёты: 1 цикл = 1 час
    settings.monitor_interval_sec = 3600
    risk = RiskManager(settings)
    return PnlTracker(
        adapters=mock_adapters,
        risk=risk,
        db=mock_db,
        settings=settings,
    )


@pytest.mark.asyncio
async def test_accrue_funding_positive_spread(tracker):
    """Short на высоком funding получает, long на низком платит (per-hour нормализация)."""
    pos = make_open_position()
    rates = {
        # HL: 0.0001/h → per 1h monitor = 0.0001
        "hyperliquid": {"BTC": make_funding_rate("hyperliquid", "BTC", "0.0001", 1)},
        # Nado: 0.0016 per 8h = 0.0002/h → per 1h monitor = 0.0002
        "nado": {"BTC": make_funding_rate("nado", "BTC", "0.0016", 8)},
    }
    earned = await tracker._accrue_funding(pos, rates)
    # long платит:  -1000 × 0.0001 = -0.1
    # short получает: 1000 × 0.0002 = 0.2
    # net = 0.1
    assert abs(earned - Decimal("0.1")) < Decimal("0.00001")


@pytest.mark.asyncio
async def test_accrue_funding_negative_rate(tracker):
    """Если long на отрицательном funding — лонг получает (per-hour нормализация)."""
    pos = make_open_position()
    tracker._adapters["hyperliquid"].get_funding_indices.return_value = (
        Decimal("-20"),
        Decimal("-20"),
    )
    tracker._adapters["nado"].get_funding_indices.return_value = (Decimal("30"), Decimal("30"))
    rates = {
        # HL: -0.0002/h → long получает 0.0002/h
        "hyperliquid": {"BTC": make_funding_rate("hyperliquid", "BTC", "-0.0002", 1)},
        # Nado: 0.0024 per 8h = 0.0003/h → short получает 0.0003/h
        "nado": {"BTC": make_funding_rate("nado", "BTC", "0.0024", 8)},
    }
    earned = await tracker._accrue_funding(pos, rates)
    # long получает: -(-0.0002) × 1000 = 0.2
    # short получает: 0.0003 × 1000 = 0.3
    # net = 0.5
    assert abs(earned - Decimal("0.5")) < Decimal("0.00001")


@pytest.mark.asyncio
async def test_accrue_funding_missing_index_does_not_change_balance(tracker):
    pos = make_open_position(symbol="UNKNOWN")
    rates = {"hyperliquid": {}, "nado": {}}
    tracker._adapters["nado"].get_funding_indices.side_effect = ValueError("unavailable")
    with pytest.raises(ValueError, match="unavailable"):
        await tracker._accrue_funding(pos, rates)
    assert pos.funding_received == 0


@pytest.mark.asyncio
async def test_check_position_should_close_spread_collapsed(tracker, settings):
    """Позиция закрывается когда спред схлопнулся (cooldown=1 для простоты теста)."""
    settings.exit_funding_spread = Decimal("0.001")  # высокий порог
    settings.exit_spread_cooldown_intervals = 1  # без cooldown для теста
    settings.negative_funding_exit_sec = 0

    pos = make_open_position()
    rates = {
        # HL: 0.0002/h, Nado: 0.0003/8h = 0.0000375/h → spread < 0 → ниже exit_threshold
        "hyperliquid": {"BTC": make_funding_rate("hyperliquid", "BTC", "0.0002", 1)},
        "nado": {"BTC": make_funding_rate("nado", "BTC", "0.0003", 8)},
    }
    should_close, reason = await tracker._check_position(pos, rates)
    assert should_close is True
    assert reason == "sustained_negative_funding"


@pytest.mark.asyncio
async def test_check_position_no_close_when_spread_ok(tracker, settings):
    """Позиция остаётся открытой если спред выше порога."""
    settings.exit_funding_spread = Decimal("0.000001")  # очень низкий порог

    pos = make_open_position()
    rates = {
        "hyperliquid": {"BTC": make_funding_rate("hyperliquid", "BTC", "0.0001", 1)},
        "nado": {"BTC": make_funding_rate("nado", "BTC", "0.0016", 8)},
    }
    should_close, _ = await tracker._check_position(pos, rates)
    assert should_close is False


@pytest.mark.asyncio
async def test_check_position_closes_on_max_hold(tracker, settings):
    """Позиция закрывается по истечении MAX_HOLD_HOURS независимо от спреда."""
    settings.max_hold_hours = 1
    pos = make_open_position(hold_hours=2.0)  # уже 2 часа, лимит 1

    rates = {
        "hyperliquid": {"BTC": make_funding_rate("hyperliquid", "BTC", "0.0001", 1)},
        "nado": {"BTC": make_funding_rate("nado", "BTC", "0.0016", 8)},
    }
    should_close, reason = await tracker._check_position(pos, rates)
    assert should_close is True
    assert "max_hold" in reason.lower()
