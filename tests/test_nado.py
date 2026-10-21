"""Contract-shaped Nado HTTP fixtures; no fabricated execution responses."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses
from eth_account import Account
from eth_account.messages import encode_typed_data

from src.adapters.nado import NadoAdapter
from src.config import Settings
from src.models import BotMode, Orderbook, OrderbookLevel, OrderRequest, OrderSide

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


def test_instance_index_selects_credentials_and_name():
    settings = Settings(
        _env_file=None,
        nado_wallets="wallet-1, wallet-2",
        nado_private_keys="key-1, key-2",
        nado_proxies="http://proxy-1:8080, http://proxy-2:8080",
    )
    adapter = NadoAdapter(
        settings,
        instance_index=1,
        transport_url=settings.nado_proxies[1],
    )
    assert adapter.name == "nado_account_1"
    assert adapter._wallet_address == "wallet-2"
    assert adapter._private_key == "key-2"
    assert adapter._transport_url == "http://proxy-2:8080"


@pytest.mark.asyncio
async def test_signed_nado_ioc_reports_only_archived_fill(monkeypatch):
    wallet = Account.create()
    settings = Settings(
        _env_file=None,
        farming_mode=BotMode.LIVE,
        nado_accounts=f"{wallet.address}:{wallet.key.hex()}:",
        max_slippage=Decimal("0.002"),
    )
    adapter = NadoAdapter(settings)
    adapter._chain_id = 763373
    adapter._subaccount = "0x" + wallet.address[2:].lower() + b"default".hex().ljust(24, "0")
    adapter._products = {"BTC": {
        "product_id": 2, "size_increment": "10000000000000",
        "price_increment_x18": "1000000000000000000",
        "min_size": "100000000000000000000", "isolated_only": False,
    }}
    monkeypatch.setattr(adapter, "_load_products", AsyncMock(return_value=adapter._products))
    monkeypatch.setattr(adapter, "_query", AsyncMock(return_value={"orders": []}))
    monkeypatch.setattr(adapter, "get_orderbook", AsyncMock(return_value=Orderbook(
        adapter.name, "BTC",
        [OrderbookLevel(Decimal("49990"), Decimal("1"))],
        [OrderbookLevel(Decimal("50000"), Decimal("1"))],
    )))
    digest = "0x" + "ab" * 32
    execute = AsyncMock(return_value={"status": "success", "data": {"digest": digest}})
    monkeypatch.setattr(adapter, "_execute", execute)

    async def archive(_):
        sent = execute.await_args.args[0]["place_order"]["order"]
        return {"orders": [{
            "digest": digest, "subaccount": adapter._subaccount,
            "product_id": 2, "amount": sent["amount"], "nonce": sent["nonce"],
            "appendix": sent["appendix"],
            "base_filled": "2000000000000000",
            "quote_filled": "-100000000000000000000",
            "fee": "50000000000000000", "builder_fee": "0",
        }]}

    monkeypatch.setattr(adapter, "_archive", archive)
    result = await adapter.place_order(OrderRequest(
        adapter.name, "BTC", OrderSide.LONG, Decimal("100"), Decimal("1"),
        quantity=Decimal("0.002"), client_id="nado-test",
    ))
    assert result.success and not result.uncertain
    assert result.filled_size == Decimal("0.002")
    assert result.avg_price == Decimal("50000")
    assert result.fee_paid == Decimal("0.05")
    submitted = execute.await_args.args[0]["place_order"]
    assert int(submitted["order"]["appendix"]) == 513
    message = {key: int(value) if key != "sender" else value
               for key, value in submitted["order"].items()}
    signed = encode_typed_data(
        domain_data={
            "name": "Nado", "version": "0.0.1", "chainId": 763373,
            "verifyingContract": "0x0000000000000000000000000000000000000002",
        },
        message_types={"Order": [
            {"name": "sender", "type": "bytes32"},
            {"name": "priceX18", "type": "int128"},
            {"name": "amount", "type": "int128"},
            {"name": "expiration", "type": "uint64"},
            {"name": "nonce", "type": "uint64"},
            {"name": "appendix", "type": "uint128"},
        ]},
        message_data=message,
    )
    assert Account.recover_message(signed, signature=submitted["signature"]) == wallet.address


@pytest.mark.asyncio
async def test_nado_unknown_submission_result_is_uncertain(monkeypatch):
    wallet = Account.create()
    settings = Settings(
        _env_file=None,
        farming_mode=BotMode.LIVE,
        nado_accounts=f"{wallet.address}:{wallet.key.hex()}:",
    )
    adapter = NadoAdapter(settings)
    adapter._chain_id = 763373
    adapter._subaccount = "0x" + wallet.address[2:].lower() + b"default".hex().ljust(24, "0")
    adapter._products = {"BTC": {
        "product_id": 2, "size_increment": "10000000000000",
        "price_increment_x18": "1000000000000000000",
        "min_size": "100000000000000000000", "isolated_only": False,
    }}
    monkeypatch.setattr(adapter, "_load_products", AsyncMock(return_value=adapter._products))
    monkeypatch.setattr(adapter, "_query", AsyncMock(return_value={"orders": []}))
    monkeypatch.setattr(adapter, "get_orderbook", AsyncMock(return_value=Orderbook(
        adapter.name, "BTC",
        [OrderbookLevel(Decimal("49990"), Decimal("1"))],
        [OrderbookLevel(Decimal("50000"), Decimal("1"))],
    )))
    monkeypatch.setattr(adapter, "_execute", AsyncMock(side_effect=TimeoutError("timeout")))
    result = await adapter.place_order(OrderRequest(
        adapter.name, "BTC", OrderSide.LONG, Decimal("100"), Decimal("1"),
        quantity=Decimal("0.002"), client_id="nado-timeout",
    ))
    assert not result.success and result.uncertain


@pytest.mark.asyncio
async def test_nado_live_connect_rejects_mismatched_wallet_before_network(monkeypatch):
    wallet = Account.create()
    other = Account.create()
    settings = Settings(
        _env_file=None,
        farming_mode=BotMode.LIVE,
        nado_accounts=f"{other.address}:{wallet.key.hex()}:",
    )
    adapter = NadoAdapter(settings)
    query = AsyncMock()
    monkeypatch.setattr(adapter, "_query", query)
    with pytest.raises(ConnectionError, match="Could not connect"):
        await adapter.connect()
    query.assert_not_awaited()
    assert adapter._session is None
