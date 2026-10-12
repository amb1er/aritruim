import re
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses

from scripts.import_perpdexlist import build_catalog
from src.adapters.aster import AsterAdapter
from src.adapters.normalized import NormalizedAdapter, load_catalog
from src.models import utc_now


def market(base, quote="USDT", status="TRADING", contract="PERPETUAL", kind=0):
    return {
        "symbol": base + quote,
        "baseAsset": base,
        "quoteAsset": quote,
        "marginAsset": quote,
        "status": status,
        "contractType": contract,
        "symbolType": kind,
    }


def premium(symbol, rate="0.0008"):
    now = utc_now()
    return {
        "symbol": symbol,
        "lastFundingRate": rate,
        "markPrice": "100",
        "nextFundingTime": int((now + timedelta(hours=1)).timestamp() * 1000),
        "time": int(now.timestamp() * 1000),
    }


@pytest.fixture
async def adapter(settings):
    instance = AsterAdapter(settings)
    rows = [
        market("BTC"),
        market("1000PEPE"),
        market("XAU", kind=1),
        market("SKHYNIX", kind=1),
        market("1000NEX"),
        market("MISSING"),
        market("BTC", "USD1"),
        market("OLD", status="SETTLING"),
        market("FUTURE", contract="CURRENT_QUARTER"),
    ]
    configs = [
        {"symbol": r["symbol"], "fundingIntervalHours": 4 if r["baseAsset"] == "1000PEPE" else 8}
        for r in rows
        if r["baseAsset"] != "MISSING"
    ]
    with aioresponses() as http:
        http.get(instance._base_url + "/fapi/v3/exchangeInfo", payload={"symbols": rows})
        http.get(instance._base_url + "/fapi/v3/fundingInfo", payload=configs)
        await instance.connect()
    yield instance
    await instance.disconnect()


@pytest.mark.asyncio
async def test_trading_usdt_perpetuals_only(adapter):
    assert await adapter.get_symbols() == [
        "1000NEX",
        "1000PEPE",
        "BTC",
        "MISSING",
        "SKHYNIX",
        "XAU",
    ]


@pytest.mark.asyncio
async def test_actual_funding_interval_is_preserved_and_unknown_is_not_assumed(adapter):
    with patch.object(
        adapter,
        "_get",
        new=AsyncMock(
            return_value=[
                premium("BTCUSDT"),
                premium("1000PEPEUSDT"),
                premium("MISSINGUSDT"),
                premium("OLDUSDT"),
            ]
        ),
    ):
        rates = await adapter.get_funding_rates()
    assert [(r.symbol, r.interval_hours) for r in rates] == [("BTC", 8), ("1000PEPE", 4)]
    assert rates[0].rate / rates[0].interval_hours == D("0.0001")
    with (
        patch.object(adapter, "_get", new=AsyncMock(return_value=premium("MISSINGUSDT"))),
        pytest.raises(ValueError, match="Unknown Aster funding interval"),
    ):
        await adapter.get_funding_rate("MISSING")


@pytest.mark.asyncio
async def test_premium_selects_requested_symbol_and_rejects_wrong_market(adapter):
    with patch.object(
        adapter, "_get", new=AsyncMock(return_value=[premium("OLDUSDT", "0.5"), premium("BTCUSDT")])
    ):
        assert (await adapter.get_funding_rate("BTC")).rate == D("0.0008")
        assert await adapter.get_mark_price("BTC") == D("100")
    with (
        patch.object(adapter, "_get", new=AsyncMock(return_value=premium("OLDUSDT"))),
        pytest.raises(ValueError, match="mismatch"),
    ):
        await adapter.get_mark_price("BTC")


@pytest.mark.asyncio
async def test_fee_categories_and_conservative_overlap(adapter):
    assert (await adapter.get_fee_rate("BTC")).taker == D("0.0004")
    assert (await adapter.get_fee_rate("XAU")).taker == D("0.000125")
    assert (await adapter.get_fee_rate("1000NEX")).taker == D("0.001")
    assert (await adapter.get_fee_rate("SKHYNIX")).taker == D("0.001")
    assert (await adapter.get_fee_rate("BTC")).maker == 0


@pytest.mark.asyncio
async def test_depth_timestamp_order_and_zero_levels(adapter):
    ts = int(utc_now().timestamp() * 1000)
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/fapi/v3/depth?symbol=BTCUSDT&limit=20",
            payload={
                "E": ts,
                "bids": [["99", "1"], ["100", "2"], ["101", "0"]],
                "asks": [["103", "2"], ["102", "1"]],
            },
        )
        book = await adapter.get_orderbook("BTC")
    assert book.bids[0].price == D("100")
    assert book.asks[0].price == D("102")
    assert int(book.timestamp.timestamp() * 1000) == ts


@pytest.mark.asyncio
async def test_history_records_applied_period_rate_not_hourly_fraction(adapter):
    ts = int((utc_now() - timedelta(hours=1)).timestamp() * 1000)
    with aioresponses() as http:
        http.get(
            re.compile(re.escape(adapter._base_url + "/fapi/v3/fundingRate") + r"\?.*"),
            payload=[{"symbol": "BTCUSDT", "fundingTime": ts, "fundingRate": "-0.0008"}],
        )
        rows = await adapter.get_funding_history("BTC")
    assert len(rows) == 1 and rows[0].rate == D("-0.0008")
    assert int(rows[0].timestamp.timestamp() * 1000) == ts
    with (
        patch.object(adapter, "_get", new=AsyncMock(return_value=[{"symbol": "OTHER"}])),
        pytest.raises(ValueError, match="mismatch"),
    ):
        await adapter.get_funding_history("BTC")


@pytest.mark.asyncio
async def test_api_error_and_live_writes_are_rejected(adapter):
    with aioresponses() as http:
        http.get(
            adapter._base_url + "/fapi/v3/premiumIndex?symbol=BTCUSDT", payload={"code": -1121}
        )
        with pytest.raises(ValueError, match="API error"):
            await adapter.get_mark_price("BTC")
    for coroutine in (
        adapter.place_order(None),
        adapter.cancel_order("BTC", "id"),
        adapter.close_position("BTC", "long"),
    ):
        with pytest.raises(NotImplementedError):
            await coroutine


@pytest.mark.asyncio
async def test_aster_is_normalized_with_existing_perps(adapter):
    wrapper = NormalizedAdapter(adapter, await adapter.get_symbols(), load_catalog())
    assert "PEPE" in await wrapper.get_symbols()
    with patch.object(adapter, "_get", new=AsyncMock(return_value=premium("1000PEPEUSDT"))):
        assert (await wrapper.get_funding_rate("PEPE")).symbol == "PEPE"
        assert await wrapper.get_mark_price("PEPE") == D("0.1")


def test_import_aster_usdt_only_and_large_bundle():
    rows = build_catalog(
        {
            "markets": [
                {"exchange": "asterdex", "symbol": "1000000MOGUSDT", "asset": "MOG"},
                {"exchange": "asterdex", "symbol": "BTCUSDT", "asset": "BTC"},
                {"exchange": "asterdex", "symbol": "BTCUSD1", "asset": "BTC"},
            ]
        }
    )["markets"]
    assert len(rows) == 2
    assert rows[0]["base_units"] == "1000000"
    assert rows[1]["native_symbol"] == "BTC"
