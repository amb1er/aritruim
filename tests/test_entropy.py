"""Entropy HIP-3 adapter and multi-venue scanner tests."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.entropy import EntropyAdapter
from src.core.scanner import FundingScanner
from src.models import FeeRate, FundingRate

ENTROPY_META = [
    {"universe": [{"name": "io:XYZ100", "szDecimals": 3}]},
    [{"funding": "0.0002", "markPx": "100", "openInterest": "50"}],
]


@pytest.fixture
def entropy(settings):
    return EntropyAdapter(settings)


def test_entropy_symbol_mapping(entropy):
    assert entropy.name == "entropy"
    assert entropy._api_symbol("XYZ100") == "io:XYZ100"
    assert entropy._api_symbol("io:XYZ100") == "io:XYZ100"
    assert entropy._public_symbol("io:XYZ100") == "XYZ100"


@pytest.mark.asyncio
async def test_entropy_connect_validates_dex(entropy):
    with (
        patch.object(entropy, "_post", new=AsyncMock(return_value=[None, {"name": "io"}])),
        patch.object(type(entropy).__mro__[1], "connect", new=AsyncMock()),
    ):
        await entropy.connect()


@pytest.mark.asyncio
async def test_entropy_connect_rejects_missing_dex(entropy):
    with (
        patch.object(entropy, "_post", new=AsyncMock(return_value=[None, {"name": "xyz"}])),
        patch.object(type(entropy).__mro__[1], "connect", new=AsyncMock()),
        patch.object(type(entropy).__mro__[1], "disconnect", new=AsyncMock()),
        pytest.raises(RuntimeError, match="not found"),
    ):
        await entropy.connect()


@pytest.mark.asyncio
async def test_entropy_funding_uses_dex_payload_and_plain_symbol(entropy):
    post = AsyncMock(return_value=ENTROPY_META)
    with patch.object(entropy, "_post", new=post):
        rates = await entropy.get_funding_rates()
    assert rates[0].exchange == "entropy"
    assert rates[0].symbol == "XYZ100"
    post.assert_awaited_once_with({"type": "metaAndAssetCtxs", "dex": "io"})


@pytest.mark.asyncio
async def test_entropy_orderbook_uses_prefixed_wire_symbol(entropy):
    post = AsyncMock(
        return_value={"levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "2"}]]}
    )
    with patch.object(entropy, "_post", new=post):
        book = await entropy.get_orderbook("XYZ100")
    assert book.exchange == "entropy"
    post.assert_awaited_once_with({"type": "l2Book", "coin": "io:XYZ100"})


@pytest.mark.asyncio
async def test_entropy_funding_history_uses_prefixed_wire_symbol(entropy):
    post = AsyncMock(return_value=[])
    with patch.object(entropy, "_post", new=post):
        assert await entropy.get_funding_history("XYZ100", lookback_hours=2) == []
    payload = post.await_args.args[0]
    assert payload["type"] == "fundingHistory"
    assert payload["coin"] == "io:XYZ100"


@pytest.mark.asyncio
async def test_entropy_account_queries_include_dex(entropy, settings):
    settings.hyperliquid_wallet_address = "0x" + "1" * 40
    post = AsyncMock(
        side_effect=[
            {"marginSummary": {"accountValue": "100"}, "withdrawable": "75"},
            {"assetPositions": []},
        ]
    )
    with patch.object(entropy, "_post", new=post):
        balance = await entropy.get_balance()
        positions = await entropy.get_positions()
    assert balance.exchange == "entropy"
    assert positions == []
    for call in post.await_args_list:
        assert call.args[0]["dex"] == "io"


@pytest.mark.asyncio
async def test_entropy_fees_are_explicit_configuration(entropy, settings):
    fee = await entropy.get_fee_rate("XYZ100")
    assert fee == FeeRate(
        "entropy", "XYZ100", settings.entropy_maker_fee, settings.entropy_taker_fee
    )


@pytest.mark.asyncio
async def test_scanner_uses_pairwise_symbol_intersections(settings):
    settings.symbols = "auto"
    settings.funding_spike_lookback_min = 0
    settings.min_funding_spread = Decimal("0.00001")
    settings.min_net_pnl_after_fees = Decimal("-1")
    now = datetime.now(UTC)

    def adapter(name, symbols):
        value = AsyncMock()
        value.name = name
        value.get_funding_rates.return_value = [
            FundingRate(name, symbol, rate, 1, now) for symbol, rate in symbols.items()
        ]
        value.get_fee_rate.return_value = FeeRate(name, "", Decimal("0"), Decimal("0"))
        return value

    hyperliquid = adapter("hyperliquid", {"BTC": Decimal("0.0001"), "GOLD": Decimal("0")})
    entropy = adapter("entropy", {"GOLD": Decimal("0.0002")})
    nado = adapter("nado", {"BTC": Decimal("0.0003")})
    scanner = FundingScanner([hyperliquid, entropy, nado], settings)
    await scanner._scan_once()
    found = []
    while not scanner.opportunities.empty():
        found.append(await scanner.opportunities.get())
    assert {(item.symbol, item.long_exchange, item.short_exchange) for item in found} == {
        ("GOLD", "hyperliquid", "entropy"),
        ("BTC", "hyperliquid", "nado"),
    }
