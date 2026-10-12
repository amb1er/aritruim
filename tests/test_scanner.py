"""Тесты FundingScanner — логика нахождения спредов."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.scanner import FundingScanner
from src.models import FeeRate, FundingRate


def make_rate(exchange: str, symbol: str, rate: str, interval: int = 8) -> FundingRate:
    return FundingRate(
        exchange=exchange,
        symbol=symbol,
        rate=Decimal(rate),
        interval_hours=interval,
        next_funding_ts=datetime.now(UTC),
    )


@pytest.fixture
def mock_adapters():
    hl = MagicMock()
    hl.name = "hyperliquid"
    hl.get_funding_rates = AsyncMock(
        return_value=[
            # BTC: 0.0001/h (interval=1)
            make_rate("hyperliquid", "BTC", "0.0001", interval=1),
            make_rate("hyperliquid", "ETH", "-0.00005", interval=1),
            make_rate("hyperliquid", "SOL", "0.00002", interval=1),
        ]
    )
    hl.get_fee_rate = AsyncMock(
        return_value=FeeRate("hyperliquid", "BTC", Decimal("0"), Decimal("0.00035"))
    )

    nado = MagicMock()
    nado.name = "nado"
    nado.get_funding_rates = AsyncMock(
        return_value=[
            # BTC: 0.02/8h = 0.0025/h → per-hour rate >> HL → profitable to short Nado
            make_rate("nado", "BTC", "0.02", interval=8),
            make_rate("nado", "ETH", "0.0002", interval=8),
            make_rate("nado", "SOL", "0.00001", interval=8),
        ]
    )
    nado.get_fee_rate = AsyncMock(
        return_value=FeeRate("nado", "BTC", Decimal("0"), Decimal("0.0005"))
    )

    return [hl, nado]


@pytest.mark.asyncio
async def test_scanner_finds_btc_spread(mock_adapters, settings):
    scanner = FundingScanner(mock_adapters, settings)
    await scanner._scan_once()

    # BTC имеет самый большой спред — должен быть найден
    assert not scanner.opportunities.empty()
    spread = await scanner.opportunities.get()
    assert spread.symbol == "BTC"
    assert spread.spread > Decimal("0")


@pytest.mark.asyncio
async def test_scanner_long_on_low_funding(mock_adapters, settings):
    """Лонг всегда на бирже с более низким funding."""
    scanner = FundingScanner(mock_adapters, settings)
    await scanner._scan_once()

    while not scanner.opportunities.empty():
        spread = await scanner.opportunities.get()
        if spread.symbol == "BTC":
            # Hyperliquid BTC funding (per hour) < Nado BTC (per hour)
            assert spread.long_exchange == "hyperliquid"
            assert spread.short_exchange == "nado"


@pytest.mark.asyncio
async def test_scanner_respects_symbol_whitelist(mock_adapters, settings):
    """Если whitelist задан — только эти символы."""
    settings.symbols = "ETH"
    scanner = FundingScanner(mock_adapters, settings)
    await scanner._scan_once()

    while not scanner.opportunities.empty():
        spread = await scanner.opportunities.get()
        assert spread.symbol == "ETH"


@pytest.mark.asyncio
async def test_scanner_skips_below_threshold(mock_adapters, settings):
    """Если спред ниже min_funding_spread — не добавляем в очередь."""
    settings.min_funding_spread = Decimal("1.0")  # невозможный порог
    scanner = FundingScanner(mock_adapters, settings)
    await scanner._scan_once()
    assert scanner.opportunities.empty()


@pytest.mark.asyncio
async def test_spike_check_normalizes_current_and_history_to_hourly(settings):
    lighter = MagicMock()
    lighter.name = "lighter_robinhood"
    lighter.get_funding_history = AsyncMock(
        return_value=[
            make_rate("lighter_robinhood", "BTC", "0.000032", interval=8),
            make_rate("lighter_robinhood", "BTC", "0.000004", interval=1),
        ]
    )
    scanner = FundingScanner([lighter], settings)

    assert not await scanner._is_spike("lighter_robinhood", "BTC", Decimal("0.000004"))
