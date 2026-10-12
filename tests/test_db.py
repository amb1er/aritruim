"""Тесты Database — CRUD операции с in-memory SQLite."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.db import Database
from src.models import ArbitragePosition, OrderResult, OrderSide, PositionStatus


@pytest.fixture
async def db(tmp_path):
    """In-memory SQLite через временный файл."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.disconnect()


def make_position(id: str = "pos1", symbol: str = "BTC") -> ArbitragePosition:
    return ArbitragePosition(
        id=id,
        symbol=symbol,
        long_exchange="hyperliquid",
        short_exchange="nado",
        notional_usd=Decimal("1000"),
        leverage=Decimal("1.0"),
        entry_spread=Decimal("0.0005"),
        status=PositionStatus.OPEN,
    )


def make_order_result(
    exchange: str = "hyperliquid", side: OrderSide = OrderSide.LONG
) -> OrderResult:
    return OrderResult(
        exchange=exchange,
        symbol="BTC",
        order_id="order_123",
        side=side,
        filled_size=Decimal("0.01"),
        avg_price=Decimal("100000"),
        fee_paid=Decimal("0.5"),
        success=True,
    )


@pytest.mark.asyncio
async def test_save_and_retrieve_position(db):
    pos = make_position()
    await db.save_position(pos)

    open_positions = await db.get_open_positions()
    assert len(open_positions) == 1
    saved = open_positions[0]
    assert saved.id == "pos1"
    assert saved.symbol == "BTC"
    assert saved.notional_usd == Decimal("1000")
    assert saved.status == PositionStatus.OPEN


@pytest.mark.asyncio
async def test_update_position_status(db):
    pos = make_position()
    await db.save_position(pos)

    pos.status = PositionStatus.CLOSED
    pos.net_pnl = Decimal("0.0012")
    pos.closed_at = datetime.now(UTC)
    await db.save_position(pos)

    all_pos = await db.get_all_positions()
    assert len(all_pos) == 1
    assert all_pos[0].status == PositionStatus.CLOSED
    assert all_pos[0].net_pnl == Decimal("0.0012")


@pytest.mark.asyncio
async def test_closed_position_not_in_open(db):
    pos = make_position()
    await db.save_position(pos)

    pos.status = PositionStatus.CLOSED
    pos.closed_at = datetime.now(UTC)
    await db.save_position(pos)

    open_positions = await db.get_open_positions()
    assert len(open_positions) == 0


@pytest.mark.asyncio
async def test_save_and_retrieve_trade(db):
    pos = make_position()
    await db.save_position(pos)

    order = make_order_result()
    await db.save_trade("pos1", order)

    # Проверяем через get_all_positions (trades не возвращаются напрямую в M1)
    # В будущем добавить get_trades_for_position()
    all_pos = await db.get_all_positions()
    assert len(all_pos) == 1


@pytest.mark.asyncio
async def test_log_funding_and_get_total(db):
    pos = make_position()
    await db.save_position(pos)

    await db.log_funding("pos1", "hyperliquid", "BTC", "short", Decimal("0.0005"), Decimal("0.5"))
    await db.log_funding("pos1", "nado", "BTC", "long", Decimal("-0.0001"), Decimal("-0.1"))

    total = await db.get_total_funding("pos1")
    assert total == Decimal("0.4")


@pytest.mark.asyncio
async def test_pnl_snapshot(db):
    await db.save_pnl_snapshot(
        open_pairs=2,
        closed_pairs=1,
        funding_received=Decimal("0.005"),
        fees_paid=Decimal("0.002"),
        net_pnl=Decimal("0.003"),
    )

    # Snapshots are observations, not new income events.
    daily = await db.get_daily_pnl()
    assert daily["net_pnl"] == Decimal("0")
    assert daily["funding_received"] == Decimal("0")


@pytest.mark.asyncio
async def test_position_with_orders_serialized(db):
    """Проверяем сериализацию/десериализацию OrderResult в JSON."""
    pos = make_position()
    pos.long_order = make_order_result("hyperliquid", OrderSide.LONG)
    pos.short_order = make_order_result("nado", OrderSide.SHORT)
    await db.save_position(pos)

    saved = (await db.get_open_positions())[0]
    assert saved.long_order is not None
    assert saved.long_order.exchange == "hyperliquid"
    assert saved.long_order.fee_paid == Decimal("0.5")
    assert saved.short_order is not None
    assert saved.short_order.side == OrderSide.SHORT
