import re
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioresponses import aioresponses

from src.adapters.polymarket_perps import PolymarketPerpsAdapter
from src.models import OrderRequest, OrderSide

INSTRUMENTS = [
    {
        "instrument_id": 6,
        "instrument_type": "perpetual",
        "category": "crypto",
        "symbol": "BTC-USD",
        "base_asset": "BTC",
        "quantity_decimals": 5,
        "funding_interval": "1h",
        "ui_live_time": 0,
    },
    {
        "instrument_id": 17,
        "instrument_type": "perpetual",
        "category": "equity",
        "symbol": "GOOG-USD",
        "base_asset": "GOOGL",
        "quantity_decimals": 4,
        "funding_interval": "1h",
        "ui_live_time": 0,
    },
]
FEES = {
    "fee_schedule": [
        {"category": "equity", "maker_fee_rate": "0.000125", "taker_fee_rate": "0.0004"}
    ]
}


@pytest.mark.asyncio
async def test_single_market_ticker_filters_api_response_by_id(adapter):
    rows = [
        {"instrument_id": 999, "mark_price": "7700", "funding_rate": "0.9"},
        {"instrument_id": 6, "mark_price": "65000", "funding_rate": "0.00001"},
    ]
    with patch.object(adapter, "_get", new=AsyncMock(return_value=rows)):
        assert await adapter.get_mark_price("BTC") == Decimal("65000")
        assert (await adapter.get_funding_rate("BTC")).rate == Decimal("0.00001")
    with (
        patch.object(adapter, "_get", new=AsyncMock(return_value=rows[:1])),
        pytest.raises(ValueError, match="No Polymarket"),
    ):
        await adapter.get_mark_price("BTC")


@pytest.mark.asyncio
async def test_orderbook_wrong_instrument_is_rejected(adapter):
    with (
        patch.object(adapter, "_get", new=AsyncMock(return_value={"instrument_id": 999})),
        pytest.raises(ValueError, match="instrument mismatch"),
    ):
        await adapter.get_orderbook("BTC")


@pytest.fixture
async def adapter(settings):
    instance = PolymarketPerpsAdapter(settings)
    with aioresponses() as http:
        http.get(instance._base_url + "/v1/info/instruments", payload=INSTRUMENTS)
        http.get(instance._base_url + "/v1/info/fees", payload=FEES)
        await instance.connect()
    yield instance
    await instance.disconnect()


@pytest.mark.asyncio
async def test_symbols_and_category_fees(adapter):
    assert await adapter.get_symbols() == ["BTC", "GOOGL"]
    equity_fee = await adapter.get_fee_rate("GOOGL")
    crypto_fee = await adapter.get_fee_rate("BTC")
    assert equity_fee.maker == Decimal("0.000125")
    assert equity_fee.taker == Decimal("0.0004")
    assert crypto_fee.taker == adapter._settings.polymarket_perps_taker_fee


@pytest.mark.asyncio
async def test_hourly_funding_uses_base_asset_alias(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/v1/info/tickers",
            payload=[
                {
                    "instrument_id": 6,
                    "funding_rate": "0.0000125",
                    "next_funding": 1790265600000,
                    "timestamp": 1790262642530,
                },
                {
                    "instrument_id": 17,
                    "funding_rate": "-0.00000625",
                    "next_funding": 1790265600000,
                    "timestamp": 1790262642530,
                },
            ],
        )
        rates = await adapter.get_funding_rates()
    assert [(rate.symbol, rate.rate) for rate in rates] == [
        ("BTC", Decimal("0.0000125")),
        ("GOOGL", Decimal("-0.00000625")),
    ]
    assert all(rate.interval_hours == 1 for rate in rates)


@pytest.mark.asyncio
async def test_orderbook_maps_supported_depth(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/v1/info/book?instrument_id=6&depth=100",
            payload={
                "instrument_id": 6,
                "bids": [["65000", "0.2"]],
                "asks": [["65001", "0.3"]],
                "timestamp": 1790262642530,
            },
        )
        book = await adapter.get_orderbook("BTC", depth=20)
    assert book.bids[0].price == Decimal("65000")
    assert book.asks[0].size == Decimal("0.3")


@pytest.mark.asyncio
async def test_funding_history_and_disconnected_live_writes_fail_closed(adapter):
    with aioresponses() as http:
        http.get(
            re.compile(re.escape(adapter._base_url + "/v1/info/funding") + r"\?.*"),
            payload={
                "data": [{"funding_rate": "0.00001", "timestamp": 1790262000093}],
                "more": False,
            },
        )
        history = await adapter.get_funding_history("BTC", lookback_hours=2)
    assert history[0].rate == Decimal("0.00001")
    assert await adapter.cancel_order("BTC", "1") is False


@pytest.mark.asyncio
async def test_live_ioc_order_uses_confirmed_fill(adapter):
    adapter._perps_session = AsyncMock()
    adapter._perps_session.place_order.return_value = SimpleNamespace(
        order=SimpleNamespace(
            id=123,
            filled_quantity=Decimal("0.001"),
            price=Decimal("65000"),
            status="filled",
        )
    )
    adapter._perps_session.fetch_orders.return_value = [
        adapter._perps_session.place_order.return_value.order
    ]
    adapter._perps_session.list_fills = Mock()
    fill_page = SimpleNamespace(items=[SimpleNamespace(
        order_id=123, quantity=Decimal("0.001"), price=Decimal("64990"),
        total_fee=Decimal("0.02"),
    )])
    adapter._perps_session.list_fills.return_value.first_page = AsyncMock(
        return_value=fill_page
    )
    with patch.object(adapter, "get_mark_price", new=AsyncMock(return_value=Decimal("65000"))):
        result = await adapter.place_order(
            OrderRequest(
                exchange=adapter.name,
                symbol="BTC",
                side=OrderSide.LONG,
                notional_usd=Decimal("65"),
                leverage=Decimal("1"),
                client_id="a" * 32,
            )
        )
    assert result.success is True
    assert result.order_id == "123"
    assert result.filled_size == Decimal("0.001")
    assert result.avg_price == Decimal("64990")
    assert result.fee_paid == Decimal("0.02")
    adapter._perps_session.place_order.assert_awaited_once_with(
        instrument_id=6,
        side="BUY",
        quantity=Decimal("0.00100"),
        time_in_force="ioc",
        reduce_only=False,
        client_order_id="a" * 32,
    )
    adapter._perps_session = None


@pytest.mark.asyncio
async def test_live_ioc_without_fill_receipt_is_uncertain(adapter):
    adapter._perps_session = AsyncMock()
    placed = SimpleNamespace(id=123, filled_quantity=Decimal("0.001"), status="filled")
    adapter._perps_session.place_order.return_value = SimpleNamespace(order=placed)
    adapter._perps_session.fetch_orders.return_value = [placed]
    adapter._perps_session.list_fills = Mock()
    adapter._perps_session.list_fills.return_value.first_page = AsyncMock(
        return_value=SimpleNamespace(items=[])
    )
    with patch.object(adapter, "get_mark_price", new=AsyncMock(return_value=Decimal("65000"))):
        result = await adapter.place_order(OrderRequest(
            exchange=adapter.name, symbol="BTC", side=OrderSide.LONG,
            notional_usd=Decimal("65"), leverage=Decimal("1"),
        ))
    assert result.success is False
    assert result.uncertain is True
    adapter._perps_session = None
