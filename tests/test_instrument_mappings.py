from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.import_perpdexlist import build_catalog
from src.adapters.normalized import NormalizedAdapter, load_catalog
from src.core.executor import Executor
from src.core.funding_accounting import observe_leg
from src.core.scanner import FundingScanner
from src.models import (
    BotMode,
    FeeRate,
    FundingRate,
    FundingSpread,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderSide,
    utc_now,
)


@pytest.mark.asyncio
async def test_legacy_alias_prevents_duplicate_open_and_cooldown_bypass(settings):
    settings.bot_mode = BotMode.PAPER
    _, normalized = adapter()
    db = MagicMock()
    executor = Executor({"a": normalized}, None, db, None, settings)
    executor._open_pair = AsyncMock()
    signal = FundingSpread("PEPE", "a", "b", D("0"), D("0.01"), D("0.01"), D("0.07"))
    executor.open_positions["old"] = MagicMock(symbol="kPEPE")
    await executor._handle_opportunity(signal)
    executor._open_pair.assert_not_awaited()
    executor.open_positions.clear()

    async def last_closed(symbol, mode):
        return utc_now() if symbol == "kPEPE" else None

    db.get_last_closed_at = AsyncMock(side_effect=last_closed)
    await executor._handle_opportunity(signal)
    executor._open_pair.assert_not_awaited()
    assert any(call.args[0] == "kPEPE" for call in db.get_last_closed_at.await_args_list)


def adapter(name="a", native="kPEPE", factor="1000", asset="PEPE"):
    raw = MagicMock()
    raw.name = name
    raw.is_spot = False
    raw.get_symbols = AsyncMock(return_value=[native])
    raw.get_orderbook = AsyncMock(
        return_value=Orderbook(
            name,
            native,
            [OrderbookLevel(D("0.004"), D("100"))],
            [OrderbookLevel(D("0.005"), D("200"))],
        )
    )
    raw.get_mark_price = AsyncMock(return_value=D("0.0045"))
    raw.get_funding_price = AsyncMock(return_value=D("0.0045"))
    raw.get_funding_indices = AsyncMock(return_value=(D("0.0001"), D("0.0002")))
    raw.get_fee_rate = AsyncMock(return_value=FeeRate(name, native, D("0"), D("0.001")))
    rate = FundingRate(name, native, D("0.001"), 1, None)
    raw.get_funding_rates = AsyncMock(return_value=[rate])
    raw.get_funding_rate = AsyncMock(return_value=rate)
    raw.get_funding_history = AsyncMock(return_value=[rate])
    return raw, NormalizedAdapter(raw, [native], {(name, native): (asset, D(factor))})


def test_catalog_keeps_three_gold_assets_distinct():
    catalog = load_catalog()
    assert catalog["polymarket_perps", "GOLD"] == ("XAU", D("1"))
    assert catalog["extended", "XAUT"] == ("XAUT", D("1"))
    assert catalog["hyperliquid", "PAXG"] == ("PAXG", D("1"))
    assert not any(ex == "hyperliquid_spot" for ex, _ in catalog)


def test_catalog_contains_current_cross_venue_aliases_and_collision_guards():
    catalog = load_catalog()
    assert catalog["entropy", "TCNT"] == ("TENCENT", D("1"))
    assert catalog["hibachi", "EUR"] == ("EURUSD", D("1"))
    assert catalog["hibachi", "GBP"] == ("GBPUSD", D("1"))
    assert catalog["hibachi", "XAG"] == ("XAG", D("1"))
    # Same-looking tickers that the source identifies as different assets must
    # not silently enter the same cross-venue comparison group.
    assert catalog["aster", "CT"] == ("CT_ASTERDEX", D("1"))
    assert catalog["lighter_robinhood", "USELESS"] == ("USELESS_LIGHTER_RH", D("1"))


@pytest.mark.asyncio
async def test_conversion_preserves_notional_and_funding_rate():
    raw, normalized = adapter()
    book = await normalized.get_orderbook("PEPE")
    assert book.bids[0] == OrderbookLevel(D("0.000004"), D("100000"))
    assert book.asks[0] == OrderbookLevel(D("0.000005"), D("200000"))
    raw_book = raw.get_orderbook.return_value
    assert book.bids[0].price * book.bids[0].size == raw_book.bids[0].price * raw_book.bids[0].size
    raw.get_orderbook.assert_awaited_once_with("kPEPE", 20)
    assert await normalized.get_mark_price("PEPE") == D("0.0000045")
    assert await normalized.get_funding_price("PEPE") == D("0.0000045")
    assert (await normalized.get_funding_rates())[0].symbol == "PEPE"
    assert (await normalized.get_funding_rate("PEPE")).rate == D("0.001")
    assert (await normalized.get_funding_history("PEPE"))[0].rate == D("0.001")
    assert (await normalized.get_fee_rate("PEPE")).taker == D("0.001")


@pytest.mark.asyncio
async def test_native_legacy_position_uses_original_quantity_units():
    raw, normalized = adapter()
    assert await normalized.get_mark_price("kPEPE") == D("0.0045")
    book = await normalized.get_orderbook("kPEPE")
    assert book.bids == raw.get_orderbook.return_value.bids
    assert normalized.native_aliases("PEPE") == {"kPEPE"}


@pytest.mark.asyncio
async def test_funding_index_scaling_preserves_dollar_payment():
    raw, normalized = adapter()
    raw.funding_model = "index"
    now = utc_now()
    _, dollars_raw, _ = await observe_leg(raw, "kPEPE", "short", D("100"), {"index": "0"}, now)
    _, dollars_normalized, _ = await observe_leg(
        normalized, "PEPE", "short", D("100000"), {"index": "0"}, now
    )
    assert dollars_raw == dollars_normalized == D("0.02")


@pytest.mark.asyncio
async def test_ambiguous_mapping_cannot_silently_pick_a_market():
    raw, _ = adapter()
    normalized = NormalizedAdapter(raw, ["kPEPE", "PEPE"], {("a", "kPEPE"): ("PEPE", D("1000"))})
    assert await normalized.get_symbols() == []
    assert await normalized.get_funding_rates() == []
    with pytest.raises(ValueError, match="Ambiguous"):
        await normalized.get_orderbook("PEPE")


@pytest.mark.asyncio
async def test_spot_and_bundle_perp_share_base_quantities(settings):
    raw, perp = adapter()
    spot_raw, spot = adapter("spot", "PEPE", "1", "PEPE")
    spot_raw.is_spot = True
    spot_raw.get_funding_rates.return_value = [FundingRate("spot", "PEPE", D("0"), 1, None)]
    spot_raw.get_orderbook.return_value = Orderbook(
        "spot",
        "PEPE",
        [OrderbookLevel(D("0.000004"), D("100000"))],
        [OrderbookLevel(D("0.000005"), D("200000"))],
    )
    settings.symbols = "auto"
    scanner = FundingScanner([perp, spot], settings)
    await scanner._scan_once()
    signal = scanner.opportunities.get_nowait()
    assert signal.symbol == "PEPE" and signal.long_exchange == "spot"
    executor = Executor({"a": perp, "spot": spot}, None, None, None, settings)
    long = await executor._simulate_order(
        OrderRequest("spot", "PEPE", OrderSide.LONG, D("1"), D("1"), quantity=D("100000")),
        await spot.get_orderbook("PEPE"),
    )
    short = await executor._simulate_order(
        OrderRequest("a", "PEPE", OrderSide.SHORT, D("1"), D("1"), quantity=D("100000")),
        await perp.get_orderbook("PEPE"),
    )
    assert long.filled_size == short.filled_size == D("100000")
    assert long.avg_price == D("0.000005")
    assert short.avg_price == D("0.000004")
    assert long.fee_paid == D("0.0005")
    raw.place_order.assert_not_called()


def test_import_preserves_raw_symbols_and_does_not_guess_gold_or_spot():
    data = build_catalog(
        {
            "markets": [
                {"exchange": "extended_starknet", "symbol": "kPEPE-USD", "asset": "PEPE"},
                {"exchange": "polymarket", "symbol": "GOLD-USD", "asset": "XAU"},
                {"exchange": "nado", "symbol": "XAUT-PERP_USDT0", "asset": "XAUT"},
            ]
        }
    )
    rows = {r["native_symbol"]: r for r in data["markets"]}
    assert rows["KPEPE"]["base_units"] == "1000"
    assert rows["GOLD"]["base_units"] == "1"
    assert rows["XAUT"]["asset"] == "XAUT"
    assert rows["XAUT"]["source_symbol"] == "XAUT-PERP_USDT0"
