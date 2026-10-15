from decimal import Decimal as D
from unittest.mock import AsyncMock

import aiosqlite

from src.config import Settings
from src.farming.live_executor import LiveFarmingExecutor
from src.farming.optimizer import FarmingRoute
from src.models import Balance, OrderResult, OrderSide


def result(venue, side, *, success=True, quantity="1", uncertain=False):
    return OrderResult(
        exchange=venue,
        symbol="BTC",
        order_id="order-id",
        side=side,
        filled_size=D(quantity) if success else D("0"),
        avg_price=D("100"),
        fee_paid=D("0.01") if success else D("0"),
        success=success,
        error="" if success else "rejected",
        uncertain=uncertain,
    )


def route():
    return FarmingRoute(
        symbol="BTC",
        long_venue="entropy_account_0",
        short_venue="polymarket_perps_account_0",
        notional_usd=D("10"),
        quantity=D("1"),
        expected_funding_usd=D("0.20"),
        round_trip_cost_usd=D("0.05"),
        basis_reserve_usd=D("0.01"),
        expected_cash_pnl_usd=D("0.14"),
        farming_subsidy_usd=D("0"),
        objective_usd=D("0.14"),
    )


async def make_executor(tmp_path, *, leverage="1", acknowledge=True):
    settings = Settings(
        _env_file=None,
        farming_leverage=leverage,
        farming_live_acknowledge_risk=acknowledge,
        farming_max_open_pairs=1,
        farming_notional_usd="10",
    )
    adapters = {}
    for name in ("entropy_account_0", "polymarket_perps_account_0"):
        adapter = AsyncMock()
        adapter.name = name
        adapter.supports_live = True
        adapter.get_balance.return_value = Balance(name, D("100"), D("100"))
        adapter.get_positions.return_value = []
        adapters[name] = adapter
    db = await aiosqlite.connect(tmp_path / "live.db")
    await db.execute(
        "CREATE TABLE farming_live_orders ("
        "timestamp TEXT, position_id TEXT, phase TEXT, venue TEXT, symbol TEXT, "
        "side TEXT, requested_quantity TEXT, order_id TEXT, filled_quantity TEXT, "
        "avg_price TEXT, fee_paid TEXT, success INTEGER, error TEXT)"
    )
    return LiveFarmingExecutor(adapters, settings, db, 0), adapters, db


async def test_live_open_records_two_certain_fills(tmp_path):
    executor, adapters, db = await make_executor(tmp_path)
    adapters["entropy_account_0"].place_order.return_value = result(
        "entropy_account_0", OrderSide.LONG
    )
    adapters["polymarket_perps_account_0"].place_order.return_value = result(
        "polymarket_perps_account_0", OrderSide.SHORT
    )
    try:
        fill = await executor.open(route())
        assert fill is not None
        assert fill.quantity == D("1")
        assert executor.halted is False
        rows = await (await db.execute("SELECT phase FROM farming_live_orders")).fetchall()
        assert rows == [("open_long",), ("open_short",)]
    finally:
        await db.close()


async def test_live_open_rolls_back_first_leg_when_second_leg_fails(tmp_path):
    executor, adapters, db = await make_executor(tmp_path)
    adapters["entropy_account_0"].place_order.side_effect = [
        result("entropy_account_0", OrderSide.LONG),
        result("entropy_account_0", OrderSide.SHORT),
    ]
    adapters["polymarket_perps_account_0"].place_order.return_value = result(
        "polymarket_perps_account_0", OrderSide.SHORT, success=False
    )
    try:
        assert await executor.open(route()) is None
        rollback = adapters["entropy_account_0"].place_order.await_args_list[1].args[0]
        assert rollback.is_reduce_only is True
        assert rollback.side == OrderSide.SHORT
        assert executor.halted is False
    finally:
        await db.close()


async def test_uncertain_first_leg_halts_without_opening_second_leg(tmp_path):
    executor, adapters, db = await make_executor(tmp_path)
    adapters["entropy_account_0"].place_order.return_value = result(
        "entropy_account_0", OrderSide.LONG, success=False, uncertain=True
    )
    try:
        assert await executor.open(route()) is None
        assert executor.halted is True
        adapters["polymarket_perps_account_0"].place_order.assert_not_awaited()
    finally:
        await db.close()


async def test_uncertain_second_leg_halts_without_blind_rollback(tmp_path):
    executor, adapters, db = await make_executor(tmp_path)
    adapters["entropy_account_0"].place_order.return_value = result(
        "entropy_account_0", OrderSide.LONG
    )
    adapters["polymarket_perps_account_0"].place_order.return_value = result(
        "polymarket_perps_account_0", OrderSide.SHORT, success=False, uncertain=True
    )
    try:
        assert await executor.open(route()) is None
        assert executor.halted is True
        assert adapters["entropy_account_0"].place_order.await_count == 1
    finally:
        await db.close()


async def test_live_preflight_rejects_leverage_above_one(tmp_path):
    executor, _, db = await make_executor(tmp_path, leverage="3")
    try:
        try:
            await executor.preflight()
        except RuntimeError as exc:
            assert "FARMING_LEVERAGE=1" in str(exc)
        else:
            raise AssertionError("preflight accepted unsafe initial leverage")
    finally:
        await db.close()


async def test_live_preflight_rejects_notional_above_canary_cap(tmp_path):
    executor, _, db = await make_executor(tmp_path)
    executor.settings.farming_notional_usd = D("11")
    try:
        try:
            await executor.preflight()
        except RuntimeError as exc:
            assert "FARMING_LIVE_MAX_NOTIONAL_USD" in str(exc)
        else:
            raise AssertionError("preflight accepted notional above the canary cap")
    finally:
        await db.close()
