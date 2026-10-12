from decimal import Decimal

import pytest
from aioresponses import aioresponses

from src.adapters.lighter_robinhood import LighterRobinhoodAdapter

DETAILS = {
    "code": 200,
    "order_book_details": [
        {
            "symbol": "BTC",
            "market_id": 1,
            "market_type": "perp",
            "status": "active",
            "mark_price": "65000.5",
            "maker_fee": "0",
            "taker_fee": "0.0002",
        },
        {"symbol": "BTC/USDG", "market_id": 2049, "market_type": "spot", "status": "active"},
    ],
}


@pytest.fixture
async def adapter(settings):
    instance = LighterRobinhoodAdapter(settings)
    with aioresponses() as http:
        http.get(instance._base_url + "/api/v1/orderBookDetails", payload=DETAILS)
        await instance.connect()
    yield instance
    await instance.disconnect()


@pytest.mark.asyncio
async def test_symbols_mark_and_fees(adapter):
    assert await adapter.get_symbols() == ["BTC"]
    with aioresponses() as http:
        http.get(adapter._base_url + "/api/v1/orderBookDetails", payload=DETAILS)
        assert await adapter.get_mark_price("BTC") == Decimal("65000.5")
    fee = await adapter.get_fee_rate("BTC")
    assert fee.maker == 0
    assert fee.taker == Decimal("0.0002")


@pytest.mark.asyncio
async def test_funding_uses_only_lighter_and_8h_equivalent(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/api/v1/funding-rates",
            payload={
                "code": 200,
                "funding_rates": [
                    {"market_id": 1, "exchange": "lighter", "symbol": "BTC", "rate": 0.00008},
                    {"market_id": 1, "exchange": "binance", "symbol": "BTC", "rate": 0.0001},
                ],
            },
        )
        (rate,) = await adapter.get_funding_rates()
    assert rate.exchange == "lighter_robinhood"
    assert rate.rate == Decimal("0.00008")
    assert rate.interval_hours == 8
    assert rate.rate / rate.interval_hours == Decimal("0.00001")


@pytest.mark.asyncio
async def test_orderbook_parsing(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/api/v1/orderBookOrders?market_id=1&limit=20",
            payload={
                "code": 200,
                "bids": [{"price": "65000", "remaining_base_amount": "0.2"}],
                "asks": [{"price": "65001", "remaining_base_amount": "0.3"}],
            },
        )
        book = await adapter.get_orderbook("BTC")
    assert book.bids[0].price == Decimal("65000")
    assert book.asks[0].size == Decimal("0.3")


@pytest.mark.asyncio
async def test_live_writes_are_disabled(adapter):
    with pytest.raises(NotImplementedError):
        await adapter.cancel_order("BTC", "1")
