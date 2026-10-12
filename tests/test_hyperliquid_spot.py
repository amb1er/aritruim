from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.adapters.hyperliquid_spot import HyperliquidSpotAdapter
from src.core.risk import RiskManager
from src.core.scanner import FundingScanner
from src.models import Balance, FundingRate, utc_now

META = {
    "tokens": [
        {"index": 0, "name": "USDC"},
        {"index": 1, "name": "HYPE"},
        {"index": 2, "name": "UBTC"},
        {"index": 3, "name": "HYPE"},
    ],
    "universe": [
        {"name": "@107", "tokens": [1, 0]},
        {"name": "@142", "tokens": [2, 0]},
        {"name": "@200", "tokens": [3, 0]},
    ],
}


@pytest.mark.asyncio
async def test_spot_excludes_ambiguous_and_remapped_names(settings):
    adapter = HyperliquidSpotAdapter(settings)
    adapter._post = AsyncMock(return_value=META)
    await adapter._load_markets()
    assert await adapter.get_symbols() == ["UBTC"]
    assert "BTC" not in await adapter.get_symbols()
    assert (await adapter.get_funding_rate("UBTC")).rate == 0
    with pytest.raises(NotImplementedError):
        await adapter.cancel_order("UBTC", "1")


@pytest.mark.asyncio
async def test_spot_is_only_long_when_perp_funding_positive(settings):
    spot = HyperliquidSpotAdapter(settings)
    spot._markets = {"HYPE": "@107"}
    perp = AsyncMock()
    perp.name = "perp"
    perp.is_spot = False
    perp.get_fee_rate.return_value.taker = Decimal("0.0003")
    settings.min_funding_spread = Decimal("0")
    settings.funding_spike_lookback_min = 0
    scanner = FundingScanner([spot, perp], settings)
    now = utc_now()

    async def find(rate):
        return await scanner._find_spreads(
            {"HYPE"},
            {"HYPE": FundingRate(spot.name, "HYPE", Decimal("0"), 1, None, now)},
            {"HYPE": FundingRate("perp", "HYPE", rate, 1, None, now)},
        )

    assert await find(Decimal("-0.001")) == []
    (signal,) = await find(Decimal("0.001"))
    assert (signal.long_exchange, signal.short_exchange) == (spot.name, "perp")
    assert signal.estimated_net_pnl == (
        Decimal("0.001") * settings.expected_hold_hours
        - 2 * (settings.hyperliquid_spot_taker_fee + Decimal("0.0003"))
    )


def test_spot_leg_does_not_borrow_when_leverage_is_above_one(settings):
    settings.leverage = Decimal("3")
    risk = RiskManager(settings)
    spot = Balance("hyperliquid_spot", Decimal("10000"), Decimal("10000"))
    perp = Balance("perp", Decimal("10000"), Decimal("10000"))
    assert risk.calc_position_size(spot, perp) == Decimal("1000")
