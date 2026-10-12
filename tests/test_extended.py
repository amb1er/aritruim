from decimal import Decimal

import pytest
from aioresponses import aioresponses

from src.adapters.extended import ExtendedAdapter

MARKETS = {
    "status": "OK",
    "data": [
        {
            "name": "BTC-USD",
            "type": "PERPETUAL",
            "assetName": "BTC",
            "active": True,
            "status": "ACTIVE",
            "marketStats": {
                "markPrice": "65000.5",
                "fundingRate": "0.00001",
                "nextFundingRate": 1700000000000,
            },
        },
        {
            "name": "BTCSPOT",
            "type": "SPOT",
            "assetName": "BTC",
            "active": True,
            "status": "ACTIVE",
            "marketStats": {},
        },
    ],
}


@pytest.fixture
async def adapter(settings):
    instance = ExtendedAdapter(settings)
    with aioresponses() as http:
        http.get(instance._base_url + "/api/v1/info/markets", payload=MARKETS)
        await instance.connect()
    yield instance
    await instance.disconnect()


@pytest.mark.asyncio
async def test_filters_spot_and_normalizes_symbol(adapter):
    assert await adapter.get_symbols() == ["BTC"]


@pytest.mark.asyncio
async def test_hourly_funding(adapter):
    with aioresponses() as http:
        http.get(adapter._base_url + "/api/v1/info/markets", payload=MARKETS)
        (rate,) = await adapter.get_funding_rates()
    assert rate.exchange == "extended"
    assert rate.rate == Decimal("0.00001")
    assert rate.interval_hours == 1


@pytest.mark.asyncio
async def test_orderbook_and_fees(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/api/v1/info/markets/BTC-USD/orderbook",
            payload={
                "status": "OK",
                "data": {
                    "market": "BTC-USD",
                    "bid": [{"price": "65000", "qty": "0.2"}],
                    "ask": [{"price": "65001", "qty": "0.3"}],
                },
            },
        )
        book = await adapter.get_orderbook("BTC")
    assert book.bids[0].price == Decimal("65000")
    assert book.asks[0].size == Decimal("0.3")
    fee = await adapter.get_fee_rate("BTC")
    assert fee.maker == 0
    assert fee.taker == Decimal("0.00025")


@pytest.mark.asyncio
async def test_live_writes_disabled(adapter):
    with pytest.raises(NotImplementedError):
        await adapter.cancel_order("BTC", "1")
