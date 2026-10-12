from decimal import Decimal

import pytest
from aioresponses import aioresponses

from src.adapters.hibachi import HibachiAdapter

EXCHANGE_INFO = {
    "feeConfig": {"tradeMakerFeeRate": "0", "tradeTakerFeeRate": "0.00045"},
    "futureContracts": [
        {
            "symbol": "BTC/USDT-P",
            "underlyingSymbol": "BTC",
            "status": "LIVE",
            "symbolStatus": "OPEN",
            "tickSize": "0.1",
            "orderbookGranularities": ["0.1", "1"],
        },
        {
            "symbol": "OLD/USDT-P",
            "underlyingSymbol": "OLD",
            "status": "LIVE",
            "symbolStatus": "CLOSED",
            "tickSize": "0.1",
            "orderbookGranularities": ["0.1"],
        },
    ],
}


@pytest.fixture
async def adapter(settings):
    instance = HibachiAdapter(settings)
    with aioresponses() as http:
        http.get(instance._base_url + "/market/exchange-info", payload=EXCHANGE_INFO)
        await instance.connect()
    yield instance
    await instance.disconnect()


@pytest.mark.asyncio
async def test_symbols_and_published_fees(adapter):
    assert await adapter.get_symbols() == ["BTC"]
    fee = await adapter.get_fee_rate("BTC")
    assert fee.maker == 0
    assert fee.taker == Decimal("0.00045")


@pytest.mark.asyncio
async def test_hourly_funding(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/market/data/prices?symbol=BTC%2FUSDT-P",
            payload={
                "symbol": "BTC/USDT-P",
                "markPrice": "65000",
                "timestampMs": 1700000000000,
                "fundingRateEstimation": {
                    "estimatedFundingRate": "0.00001",
                    "nextFundingTimestamp": 1700003600,
                },
            },
        )
        (rate,) = await adapter.get_funding_rates()
    assert rate.rate == Decimal("0.00001")
    assert rate.interval_hours == 1
    assert rate.next_funding_ts is not None


@pytest.mark.asyncio
async def test_funding_keeps_successful_markets_when_one_request_fails(settings):
    instance = HibachiAdapter(settings)
    info = {
        **EXCHANGE_INFO,
        "futureContracts": [
            EXCHANGE_INFO["futureContracts"][0],
            {
                **EXCHANGE_INFO["futureContracts"][0],
                "symbol": "ETH/USDT-P",
                "underlyingSymbol": "ETH",
            },
        ],
    }
    with aioresponses() as http:
        http.get(instance._base_url + "/market/exchange-info", payload=info)
        http.get(
            instance._base_url + "/market/data/prices?symbol=BTC%2FUSDT-P",
            payload={
                "timestampMs": 1700000000000,
                "fundingRateEstimation": {
                    "estimatedFundingRate": "0.00001",
                    "nextFundingTimestamp": 1700003600,
                },
            },
        )
        for _ in range(3):
            http.get(
                instance._base_url + "/market/data/prices?symbol=ETH%2FUSDT-P",
                status=429,
            )
        await instance.connect()
        rates = await instance.get_funding_rates()
    await instance.disconnect()

    assert [rate.symbol for rate in rates] == ["BTC"]


@pytest.mark.asyncio
async def test_orderbook_uses_contract_granularity(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url
            + "/market/data/orderbook?symbol=BTC%2FUSDT-P&depth=20&granularity=0.1",
            payload={
                "bid": {"levels": [{"price": "65000", "quantity": "0.2"}]},
                "ask": {"levels": [{"price": "65001", "quantity": "0.3"}]},
            },
        )
        book = await adapter.get_orderbook("BTC")
    assert book.bids[0].price == Decimal("65000")
    assert book.asks[0].size == Decimal("0.3")


@pytest.mark.asyncio
async def test_live_writes_disabled(adapter):
    with pytest.raises(NotImplementedError):
        await adapter.cancel_order("BTC", "1")
