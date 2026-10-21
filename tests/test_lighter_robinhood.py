from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses
from pydantic import ValidationError

from src.adapters.lighter_robinhood import LighterRobinhoodAdapter
from src.config import Settings
from src.models import Orderbook, OrderbookLevel, OrderRequest, OrderSide

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


def test_lighter_account_indices_are_validated():
    with pytest.raises(ValidationError, match="out of range"):
        Settings(_env_file=None, lighter_robinhood_api_key_indices="256")


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
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.cancel_order("BTC", "1")


@pytest.mark.asyncio
async def test_lighter_account_index_must_match_wallet(monkeypatch):
    settings = Settings(
        _env_file=None,
        lighter_robinhood_accounts="0xabc:api-key:",
        lighter_robinhood_account_indices="7",
        lighter_robinhood_api_key_indices="2",
    )
    adapter = LighterRobinhoodAdapter(settings, instance_index=0)
    monkeypatch.setattr(adapter, "_get", AsyncMock(return_value={
        "code": 200,
        "accounts": [{"index": 7, "l1_address": "0xdef"}],
    }))
    with pytest.raises(RuntimeError, match="does not match"):
        await adapter._account()


@pytest.mark.asyncio
async def test_lighter_active_order_blocks_new_trade(monkeypatch):
    settings = Settings(
        _env_file=None,
        lighter_robinhood_accounts="0xabc:api-key:",
        lighter_robinhood_account_indices="7",
        lighter_robinhood_api_key_indices="2",
    )
    adapter = LighterRobinhoodAdapter(settings, instance_index=0)
    adapter._signer = SimpleNamespace(create_market_order=AsyncMock())
    monkeypatch.setattr(adapter, "_active_orders", AsyncMock(return_value=[{"order_index": 5}]))
    with pytest.raises(RuntimeError, match="active orders"):
        await adapter.place_order(OrderRequest(
            adapter.name, "BTC", OrderSide.LONG, Decimal("50"), Decimal("1"),
            client_id="order-3", quantity=Decimal("0.5"),
        ))
    adapter._signer.create_market_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["filled", "canceled-too-much-slippage"])
async def test_signed_lighter_order_is_confirmed_before_reporting_fill(monkeypatch, status):
    settings = Settings(
        _env_file=None,
        lighter_robinhood_accounts="0xabc:api-key:",
        lighter_robinhood_account_indices="7",
        lighter_robinhood_api_key_indices="2",
    )
    adapter = LighterRobinhoodAdapter(settings, instance_index=0)
    adapter._markets = {
        "BTC": {
            "market_id": 1,
            "supported_size_decimals": 3,
            "supported_price_decimals": 2,
            "min_base_amount": "0.001",
            "taker_fee": "0.0002",
        }
    }
    submit = AsyncMock(return_value=(None, SimpleNamespace(code=200), None))
    adapter._signer = SimpleNamespace(
        create_market_order=submit,
        update_leverage=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)),
        CROSS_MARGIN_MODE=0,
    )
    monkeypatch.setattr(adapter, "_auth_token", lambda: "auth")
    monkeypatch.setattr(adapter, "_active_orders", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        adapter,
        "get_orderbook",
        AsyncMock(return_value=Orderbook(
            adapter.name, "BTC", [OrderbookLevel(Decimal("99"), Decimal("2"))],
            [OrderbookLevel(Decimal("100"), Decimal("2"))],
        )),
    )
    monkeypatch.setattr(
        adapter,
        "_get",
        AsyncMock(return_value={"code": 200, "orders": [{
            "client_order_index": 1, "order_index": 55,
            "owner_account_index": 7, "market_index": 1,
            "is_ask": False, "reduce_only": False,
            "status": status, "filled_base_amount": "0.5",
            "filled_quote_amount": "50",
        }]}),
    )
    monkeypatch.setattr(
        "src.adapters.lighter_robinhood.hashlib.sha256",
        lambda value: SimpleNamespace(digest=lambda: bytes.fromhex("0000000000000001")),
    )
    result = await adapter.place_order(OrderRequest(
        adapter.name, "BTC", OrderSide.LONG, Decimal("50"), Decimal("1"),
        client_id="order-1", quantity=Decimal("0.5"),
    ))
    assert result.success
    assert not result.uncertain
    assert result.filled_size == Decimal("0.5")
    assert result.avg_price == Decimal("100")
    assert result.fee_paid == Decimal("0.01")
    assert submit.await_args.kwargs["base_amount"] == 500
    assert submit.await_args.kwargs["api_key_index"] == 2
    assert adapter._signer.update_leverage.await_args.kwargs["leverage"] == 1
    assert adapter._get.await_args.kwargs["headers"] == {"authorization": "auth"}


@pytest.mark.asyncio
async def test_lighter_submission_exception_is_uncertain(monkeypatch):
    settings = Settings(
        _env_file=None,
        lighter_robinhood_accounts="0xabc:api-key:",
        lighter_robinhood_account_indices="7",
        lighter_robinhood_api_key_indices="2",
    )
    adapter = LighterRobinhoodAdapter(settings, instance_index=0)
    adapter._markets = {
        "BTC": {
            "market_id": 1, "supported_size_decimals": 3,
            "supported_price_decimals": 2, "min_base_amount": "0.001",
        }
    }
    adapter._signer = SimpleNamespace(
        create_market_order=AsyncMock(side_effect=TimeoutError("network timeout")),
        update_leverage=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)),
        CROSS_MARGIN_MODE=0,
    )
    monkeypatch.setattr(adapter, "_active_orders", AsyncMock(return_value=[]))
    monkeypatch.setattr(adapter, "get_orderbook", AsyncMock(return_value=Orderbook(
        adapter.name, "BTC", [OrderbookLevel(Decimal("99"), Decimal("2"))],
        [OrderbookLevel(Decimal("100"), Decimal("2"))],
    )))
    result = await adapter.place_order(OrderRequest(
        adapter.name, "BTC", OrderSide.LONG, Decimal("50"), Decimal("1"),
        client_id="order-2", quantity=Decimal("0.5"),
    ))
    assert not result.success
    assert result.uncertain
