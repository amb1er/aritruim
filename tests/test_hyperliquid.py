"""Тесты HyperliquidAdapter с моками HTTP."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.hyperliquid import HyperliquidAdapter

# Мок ответ от /info?type=metaAndAssetCtxs
MOCK_META_AND_CTXS = [
    {
        "universe": [
            {"name": "BTC", "szDecimals": 5},
            {"name": "ETH", "szDecimals": 4},
            {"name": "SOL", "szDecimals": 2},
            {"name": "AI", "szDecimals": 1, "isDelisted": True},
        ]
    },
    [
        {"funding": "0.0000125", "openInterest": "1000"},
        {"funding": "-0.000005", "openInterest": "500"},
        {"funding": "0.00003", "openInterest": "200"},
        {"funding": "0.0", "openInterest": "0"},
    ],
]

MOCK_L2_BOOK = {
    "levels": [
        [{"px": "99990", "sz": "1.5"}, {"px": "99980", "sz": "2.0"}],  # bids
        [{"px": "100010", "sz": "1.2"}, {"px": "100020", "sz": "3.0"}],  # asks
    ]
}


@pytest.fixture
def adapter(settings):
    return HyperliquidAdapter(settings)


@pytest.mark.asyncio
async def test_fee_defaults_match_base_tier(adapter):
    fee = await adapter.get_fee_rate("BTC")
    assert fee.taker == Decimal("0.00045")
    assert fee.maker == Decimal("0.00015")


@pytest.mark.asyncio
async def test_fee_parser_uses_documented_user_fields(adapter, settings):
    settings.hyperliquid_wallet_address = "0x" + "0" * 40
    with patch.object(
        adapter,
        "_post",
        new=AsyncMock(
            return_value={
                "userCrossRate": "0.000315",
                "userAddRate": "0.000105",
            }
        ),
    ):
        fee = await adapter.get_fee_rate("BTC")
    assert fee.taker == Decimal("0.000315")
    assert fee.maker == Decimal("0.000105")


@pytest.mark.asyncio
async def test_get_funding_rates_parses_correctly(adapter, settings):
    """Проверяем что funding rates корректно парсятся из ответа API."""
    async with adapter:
        with patch.object(adapter, "_post", new=AsyncMock(return_value=MOCK_META_AND_CTXS)):
            rates = await adapter.get_funding_rates()

    assert len(rates) == 3
    btc = next(r for r in rates if r.symbol == "BTC")
    assert btc.exchange == "hyperliquid"
    assert btc.rate == Decimal("0.0000125")
    assert btc.interval_hours == 1

    eth = next(r for r in rates if r.symbol == "ETH")
    assert eth.rate == Decimal("-0.000005")


@pytest.mark.asyncio
async def test_get_symbols_returns_list(adapter):
    mock_meta = {
        "universe": [
            {"name": "BTC"},
            {"name": "ETH"},
            {"name": "AI", "isDelisted": True},
        ]
    }
    async with adapter:
        with patch.object(adapter, "_post", new=AsyncMock(return_value=mock_meta)):
            symbols = await adapter.get_symbols()

    assert "BTC" in symbols
    assert "ETH" in symbols
    assert "AI" not in symbols


@pytest.mark.asyncio
async def test_get_orderbook_parses_levels(adapter):
    async with adapter:
        with patch.object(adapter, "_post", new=AsyncMock(return_value=MOCK_L2_BOOK)):
            ob = await adapter.get_orderbook("BTC")

    assert len(ob.bids) == 2
    assert len(ob.asks) == 2
    assert ob.bids[0].price == Decimal("99990")
    assert ob.asks[0].price == Decimal("100010")
    assert ob.exchange == "hyperliquid"
    assert ob.symbol == "BTC"


@pytest.mark.asyncio
async def test_get_balance_no_wallet_returns_zero(adapter):
    async with adapter:
        balance = await adapter.get_balance()
    assert balance.total_usd == Decimal("0")
    assert balance.available_usd == Decimal("0")


@pytest.mark.asyncio
async def test_annualized_rate(adapter):
    """FundingRate.annualized корректно считает годовую ставку."""
    async with adapter:
        with patch.object(adapter, "_post", new=AsyncMock(return_value=MOCK_META_AND_CTXS)):
            rates = await adapter.get_funding_rates()
    btc = next(r for r in rates if r.symbol == "BTC")
    # 0.0000125 per hour × 8760 hours = 0.1095 = ~10.95% годовых
    assert abs(btc.annualized - Decimal("0.1095")) < Decimal("0.001")
