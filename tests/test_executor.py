"""Тесты Executor — открытие, закрытие, rollback."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.executor import Executor
from src.models import (
    Balance,
    BotMode,
    FeeRate,
    FundingRate,
    FundingSpread,
    Orderbook,
    OrderbookLevel,
    OrderSide,
    PositionStatus,
)


def make_orderbook(bid: str, ask: str, exchange: str = "test", symbol: str = "BTC") -> Orderbook:
    return Orderbook(
        exchange=exchange,
        symbol=symbol,
        bids=[OrderbookLevel(price=Decimal(bid), size=Decimal("10"))],
        asks=[OrderbookLevel(price=Decimal(ask), size=Decimal("10"))],
    )


def make_spread(
    symbol: str = "BTC",
    long_ex: str = "hyperliquid",
    short_ex: str = "nado",
    spread: str = "0.0005",
) -> FundingSpread:
    return FundingSpread(
        symbol=symbol,
        long_exchange=long_ex,
        short_exchange=short_ex,
        long_rate=Decimal("0.0001"),
        short_rate=Decimal("0.0006"),
        spread=Decimal(spread),
        estimated_net_pnl=Decimal("0.003"),
    )


@pytest.fixture
def mock_adapters():
    hl = MagicMock()
    hl.name = "hyperliquid"
    hl.get_balance = AsyncMock(
        return_value=Balance("hyperliquid", Decimal("10000"), Decimal("10000"))
    )
    hl.get_orderbook = AsyncMock(return_value=make_orderbook("99990", "100010", "hyperliquid"))
    hl.close_position = AsyncMock()
    hl.get_mark_price = AsyncMock(return_value=Decimal("100000"))

    nado = MagicMock()
    nado.name = "nado"
    nado.get_balance = AsyncMock(return_value=Balance("nado", Decimal("10000"), Decimal("10000")))
    nado.get_orderbook = AsyncMock(return_value=make_orderbook("99980", "100020", "nado"))
    nado.close_position = AsyncMock()
    nado.get_mark_price = AsyncMock(return_value=Decimal("100000"))

    for adapter, rate in ((hl, "0.0001"), (nado, "0.0006")):
        adapter.funding_model = "index"
        adapter.get_funding_indices = AsyncMock(return_value=(Decimal("0"), Decimal("0")))
        adapter.get_funding_rate = AsyncMock(
            return_value=FundingRate(adapter.name, "BTC", Decimal(rate), 1, None)
        )
        adapter.get_fee_rate = AsyncMock(
            return_value=FeeRate(adapter.name, "BTC", Decimal("0"), Decimal("0.00035"))
        )
    return {"hyperliquid": hl, "nado": nado}


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_last_closed_at = AsyncMock(return_value=None)
    db.pair_loss_cooldown_until = AsyncMock(return_value=None)
    db.save_position = AsyncMock()
    db.save_trade = AsyncMock()
    db.record_fill = AsyncMock()
    db.get_ledger_totals = AsyncMock(return_value={"net_pnl": Decimal("0")})
    db.get_daily_pnl = AsyncMock(return_value={"net_pnl": Decimal("0")})
    db.log_funding = AsyncMock()
    db.get_open_positions = AsyncMock(return_value=[])
    db.get_all_positions = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_notifier():
    n = MagicMock()
    n.on_position_opened = AsyncMock()
    n.on_position_closed = AsyncMock()
    n.on_error = AsyncMock()
    return n


@pytest.fixture
def mock_risk(settings):
    from src.core.risk import RiskManager

    risk = RiskManager(settings)
    return risk


@pytest.fixture
def executor(mock_adapters, mock_risk, mock_db, mock_notifier, settings):
    settings.bot_mode = BotMode.PAPER
    return Executor(
        adapters=mock_adapters,
        risk=mock_risk,
        db=mock_db,
        notifier=mock_notifier,
        settings=settings,
    )


@pytest.mark.asyncio
async def test_open_pair_paper_mode(executor, mock_db, mock_notifier):
    """Открытие пары в paper режиме создаёт позицию и сохраняет в БД."""
    spread = make_spread()
    await executor._handle_opportunity(spread)

    assert len(executor.open_positions) == 1
    pos = list(executor.open_positions.values())[0]
    assert pos.symbol == "BTC"
    assert pos.status == PositionStatus.OPEN
    assert pos.long_exchange == "hyperliquid"
    assert pos.short_exchange == "nado"
    assert pos.fees_paid > Decimal("0")

    mock_db.save_position.assert_called()
    mock_notifier.on_position_opened.assert_called_once()


@pytest.mark.asyncio
async def test_daily_loss_limit_blocks_new_entry(executor, mock_db, mock_adapters):
    mock_db.get_daily_pnl.return_value = {"net_pnl": Decimal("-100")}
    await executor._handle_opportunity(make_spread())
    assert not executor.open_positions
    for adapter in mock_adapters.values():
        adapter.get_orderbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_liquidity_rejects_start_short_pair_cooldown(
    executor, mock_adapters
):
    executor._cfg.liquidity_reject_limit = 2
    executor._cfg.liquidity_cooldown_sec = 120
    executor._risk.assess_open = AsyncMock(
        return_value=(False, "слишком большое проскальзывание: 0.2%", Decimal("0"))
    )
    spread = make_spread()

    await executor._handle_opportunity(spread)
    await executor._handle_opportunity(spread)
    counts = {name: adapter.get_orderbook.await_count for name, adapter in mock_adapters.items()}
    await executor._handle_opportunity(spread)

    assert executor._risk.assess_open.await_count == 2
    assert {
        name: adapter.get_orderbook.await_count for name, adapter in mock_adapters.items()
    } == counts


@pytest.mark.asyncio
async def test_skip_duplicate_symbol(executor):
    """Не открываем вторую пару на тот же символ."""
    spread = make_spread()
    await executor._handle_opportunity(spread)
    assert len(executor.open_positions) == 1

    # Вторая попытка — должна быть пропущена
    await executor._handle_opportunity(spread)
    assert len(executor.open_positions) == 1


@pytest.mark.asyncio
async def test_close_position(executor, mock_db, mock_notifier):
    """Закрытие позиции обновляет статус и считает net_pnl."""
    spread = make_spread()
    await executor._handle_opportunity(spread)

    pos = list(executor.open_positions.values())[0]
    pos.funding_received = Decimal("0.001")  # симулируем накопленный funding

    await executor.close_position(pos, "spread_collapsed")

    assert len(executor.open_positions) == 0
    assert pos.status == PositionStatus.CLOSED
    assert pos.closed_at is not None
    assert pos.realized_price_pnl < 0
    assert pos.net_pnl == pos.funding_received + pos.realized_price_pnl - pos.fees_paid
    mock_notifier.on_position_closed.assert_called_once()


@pytest.mark.asyncio
async def test_close_all(executor, mock_notifier):
    """close_all закрывает все открытые позиции."""
    await executor._handle_opportunity(make_spread(symbol="BTC"))
    await executor._handle_opportunity(make_spread(symbol="ETH"))
    assert len(executor.open_positions) == 2

    await executor.close_all("shutdown")
    assert len(executor.open_positions) == 0
    assert mock_notifier.on_position_closed.call_count == 2


@pytest.mark.asyncio
async def test_simulate_order_uses_ask_for_long(executor):
    """Лонг исполняется по ask цене."""
    from src.models import OrderRequest

    ob = make_orderbook("99990", "100010")
    req = OrderRequest(
        exchange="hyperliquid",
        symbol="BTC",
        side=OrderSide.LONG,
        notional_usd=Decimal("1000"),
        leverage=Decimal("1.0"),
    )
    result = await executor._simulate_order(req, ob)
    assert result.avg_price == Decimal("100010")
    assert result.success is True
    assert result.fee_paid > Decimal("0")


@pytest.mark.asyncio
async def test_simulate_order_uses_bid_for_short(executor):
    """Шорт исполняется по bid цене."""
    from src.models import OrderRequest

    ob = make_orderbook("99990", "100010")
    req = OrderRequest(
        exchange="nado",
        symbol="BTC",
        side=OrderSide.SHORT,
        notional_usd=Decimal("1000"),
        leverage=Decimal("1.0"),
    )
    result = await executor._simulate_order(req, ob)
    assert abs(result.avg_price - Decimal("99990")) < Decimal("0.00000001")
    assert result.success is True
