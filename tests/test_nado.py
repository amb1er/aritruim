"""Contract-shaped Nado HTTP fixtures; no fabricated execution responses."""

from decimal import Decimal

import pytest
from aioresponses import aioresponses

from src.adapters.nado import NadoAdapter

PRODUCTS = [
    {
        "type": "perp",
        "product_id": 2,
        "symbol": "BTC-PERP",
        "trading_status": "live",
        "maker_fee_rate_x18": "-100000000000000",
        "taker_fee_rate_x18": "500000000000000",
    },
    {"type": "spot", "product_id": 1, "symbol": "WBTC", "trading_status": "live"},
    {"type": "perp", "product_id": 4, "symbol": "ETH-PERP", "trading_status": "not_tradable"},
]


@pytest.fixture
async def adapter(settings):
    async with NadoAdapter(settings) as instance:
        yield instance


@pytest.mark.asyncio
async def test_get_symbols_filters_spot_and_disabled_markets(adapter):
    with aioresponses() as http:
        http.get(adapter._rest_url + "/symbols", payload=PRODUCTS)
        assert await adapter.get_symbols() == ["BTC"]
        assert await adapter.get_symbols() == ["BTC"]  # cached


@pytest.mark.asyncio
async def test_get_funding_rates_uses_24h_x18_archive_rate(adapter):
    with aioresponses() as http:
        http.get(adapter._rest_url + "/symbols", payload=PRODUCTS)
        http.post(
            adapter._settings.nado_archive_url,
            payload={
                "2": {
                    "product_id": 2,
                    "funding_rate_x18": "2400000000000000",
                    "update_time": "1700000000",
                }
            },
        )
        (rate,) = await adapter.get_funding_rates()
        assert rate.rate == Decimal("0.0024")
        assert rate.interval_hours == 24
        assert rate.rate / rate.interval_hours == Decimal("0.0001")
        assert rate.timestamp.tzinfo is not None
        calls = [
            call
            for (method, url), values in http.requests.items()
            if method == "POST"
            for call in values
        ]
        assert calls[0].kwargs["json"] == {"funding_rates": {"product_ids": [2]}}


@pytest.mark.asyncio
async def test_get_orderbook_unwraps_gateway_and_scales_levels(adapter):
    with aioresponses() as http:
        http.get(adapter._rest_url + "/symbols", payload=PRODUCTS)
        http.get(
            adapter._rest_url + "/query?type=market_liquidity&product_id=2&depth=20",
            payload={
                "status": "success",
                "data": {
                    "bids": [["50000000000000000000000", "1000000000000000000"]],
                    "asks": [["50010000000000000000000", "800000000000000000"]],
                    "timestamp": "1700000000000000000",
                },
            },
        )
        book = await adapter.get_orderbook("BTC")
        assert book.bids[0].price == Decimal("50000")
        assert book.asks[0].price == Decimal("50010")
        assert book.asks[0].size == Decimal("0.8")


@pytest.mark.asyncio
async def test_fee_rate_from_symbols(adapter):
    with aioresponses() as http:
        http.get(adapter._rest_url + "/symbols", payload=PRODUCTS)
        fee = await adapter.get_fee_rate("BTC")
        assert fee.taker == Decimal("0.0005")
        assert fee.maker == Decimal("-0.0001")


@pytest.mark.asyncio
async def test_get_balance_no_wallet_returns_zero(adapter):
    balance = await adapter.get_balance()
    assert balance.total_usd == 0


@pytest.mark.asyncio
async def test_gateway_failure_is_not_an_empty_success(adapter):
    with aioresponses() as http:
        http.get(
            adapter._rest_url + "/query?type=contracts",
            payload={"status": "failure", "error_code": 1000},
        )
        with pytest.raises(ValueError, match="Nado query failed"):
            await adapter._query("contracts")
