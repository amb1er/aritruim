import sqlite3
from decimal import Decimal
from unittest.mock import AsyncMock

from src.adapters.arcus import ArcusAdapter
from src.adapters.entropy import EntropyAdapter
from src.adapters.lighter_robinhood import LighterRobinhoodAdapter
from src.adapters.normalized import NormalizedAdapter
from src.adapters.polymarket_perps import PolymarketPerpsAdapter
from src.adapters.txflow import TxFlowAdapter
from src.config import Settings
from src.farming.engine import FarmingEngine
from src.models import FundingRate, OrderRequest, OrderResult, OrderSide


def test_account_csv_preserves_transport_url_colons_and_empty_transport():
    settings = Settings(
        _env_file=None,
        hyperliquid_accounts=(
            "0xAAA:secret-a:http://user:pass@proxy.example:8080,"
            "0xBBB:secret-b:"
        ),
    )
    accounts = settings.resolved_hyperliquid_accounts()
    assert accounts[0].address == "0xAAA"
    assert accounts[0].credential == "secret-a"
    assert accounts[0].transport_url == "http://user:pass@proxy.example:8080"
    assert accounts[1].transport_url is None


def test_new_account_format_has_priority_over_legacy_lists():
    settings = Settings(
        _env_file=None,
        nado_accounts="new-address:new-key:",
        nado_wallets="old-address",
        nado_private_keys="old-key",
        nado_proxies="http://old-proxy:8080",
    )
    account = settings.resolved_nado_accounts()[0]
    assert account.address == "new-address"
    assert account.credential == "new-key"
    assert account.transport_url is None


def test_farming_venue_profiles_are_isolated_by_wallet_index():
    settings = Settings(
        _env_file=None,
        farming_instance_venues=(
            "entropy|polymarket|arcus;"
            "nado|txflow|lighter"
        ),
    )

    assert settings.farming_venues_for_instance(0) == {
        "entropy",
        "polymarket_perps",
        "arcus",
    }
    assert settings.farming_venues_for_instance(1) == {
        "nado",
        "txflow",
        "lighter_robinhood",
    }


def test_farming_venue_profile_requires_two_known_venues():
    one = Settings(_env_file=None, farming_instance_venues="arcus")
    unknown = Settings(_env_file=None, farming_instance_venues="arcus|unknown")

    try:
        one.farming_venues_for_instance(0)
    except ValueError as exc:
        assert "at least two" in str(exc)
    else:
        raise AssertionError("single-venue farming profile was accepted")

    try:
        unknown.farming_venues_for_instance(0)
    except ValueError as exc:
        assert "Unknown farming venue" in str(exc)
    else:
        raise AssertionError("unknown farming venue was accepted")


async def test_account_adapter_name_uses_base_venue_catalog_mapping():
    adapter = AsyncMock()
    adapter.name = "nado_account_1"
    adapter.get_funding_rates.return_value = [
        FundingRate("nado_account_1", "NATIVE", Decimal("0.001"), 1, None)
    ]
    normalized = NormalizedAdapter(
        adapter,
        ["NATIVE"],
        {("nado", "NATIVE"): ("CANONICAL", Decimal("1000"))},
    )
    assert await normalized.get_symbols() == ["CANONICAL"]
    assert (await normalized.get_funding_rates())[0].symbol == "CANONICAL"


async def test_normalized_live_order_converts_native_units():
    adapter = AsyncMock()
    adapter.name = "venue_account_0"
    adapter.supports_live = True
    adapter.place_order.return_value = OrderResult(
        exchange=adapter.name,
        symbol="NATIVE",
        order_id="1",
        side=OrderSide.LONG,
        filled_size=Decimal("2"),
        avg_price=Decimal("100"),
        fee_paid=Decimal("0.01"),
        success=True,
    )
    normalized = NormalizedAdapter(
        adapter,
        ["NATIVE"],
        {("venue", "NATIVE"): ("CANONICAL", Decimal("1000"))},
    )
    order = OrderRequest(
        exchange=normalized.name,
        symbol="CANONICAL",
        side=OrderSide.LONG,
        notional_usd=Decimal("200"),
        leverage=Decimal("1"),
        quantity=Decimal("2000"),
    )

    result = await normalized.place_order(order)

    assert normalized.supports_live is True
    native_order = adapter.place_order.await_args.args[0]
    assert native_order.symbol == "NATIVE"
    assert native_order.quantity == Decimal("2")
    assert result.symbol == "CANONICAL"
    assert result.filled_size == Decimal("2000")
    assert result.avg_price == Decimal("0.1")


def test_all_farming_venue_accounts_use_the_same_csv_format():
    settings = Settings(
        _env_file=None,
        entropy_accounts="entropy-address:entropy-key:http://entropy-proxy:8080",
        polymarket_perps_accounts="poly-address:poly-key:http://poly-proxy:8080",
        txflow_accounts="tx-address:tx-key:",
        lighter_robinhood_accounts="lit-address:lit-key:http://lit-proxy:8080",
        arcus_accounts="arcus-address:arcus-key:http://arcus-proxy:8080",
    )

    entropy = settings.resolved_entropy_accounts()[0]
    polymarket = settings.resolved_polymarket_perps_accounts()[0]
    txflow = settings.resolved_txflow_accounts()[0]
    lighter = settings.resolved_lighter_robinhood_accounts()[0]
    arcus = settings.resolved_arcus_accounts()[0]

    assert (entropy.address, entropy.credential, entropy.transport_url) == (
        "entropy-address",
        "entropy-key",
        "http://entropy-proxy:8080",
    )
    assert (polymarket.address, polymarket.credential, polymarket.transport_url) == (
        "poly-address",
        "poly-key",
        "http://poly-proxy:8080",
    )
    assert (txflow.address, txflow.credential, txflow.transport_url) == (
        "tx-address",
        "tx-key",
        None,
    )
    assert (lighter.address, lighter.credential, lighter.transport_url) == (
        "lit-address",
        "lit-key",
        "http://lit-proxy:8080",
    )
    assert (arcus.address, arcus.credential, arcus.transport_url) == (
        "arcus-address",
        "arcus-key",
        "http://arcus-proxy:8080",
    )


def test_public_farming_adapters_are_isolated_by_instance():
    settings = Settings(
        _env_file=None,
        polymarket_perps_accounts="poly-address:poly-key:http://poly-proxy:8080",
        txflow_accounts="tx-address:tx-key:http://tx-proxy:8080",
        lighter_robinhood_accounts="lit-address:lit-key:http://lit-proxy:8080",
        arcus_accounts="arcus-address:arcus-key:http://arcus-proxy:8080",
    )

    polymarket = PolymarketPerpsAdapter(settings, instance_index=0)
    txflow = TxFlowAdapter(settings, instance_index=0)
    lighter = LighterRobinhoodAdapter(settings, instance_index=0)
    arcus = ArcusAdapter(settings, instance_index=0)

    assert polymarket.name == "polymarket_perps_account_0"
    assert polymarket._transport_url == "http://poly-proxy:8080"
    assert txflow.name == "txflow_account_0"
    assert txflow._transport_url == "http://tx-proxy:8080"
    assert lighter.name == "lighter_robinhood_account_0"
    assert lighter._transport_url == "http://lit-proxy:8080"
    assert arcus.name == "arcus_account_0"
    assert arcus._transport_url == "http://arcus-proxy:8080"


def test_farming_leverage_scales_position_notional():
    settings = Settings(
        _env_file=None,
        farming_notional_usd="50",
        farming_leverage="3",
    )

    engine = FarmingEngine({}, settings)

    assert engine.optimizer.notional_usd == Decimal("150")


def test_entropy_accounts_do_not_depend_on_matching_hyperliquid_count():
    settings = Settings(
        _env_file=None,
        hyperliquid_accounts="hl-address:hl-key:",
        entropy_accounts="entropy-0:key-0:,entropy-1:key-1:http://entropy-proxy:8080",
    )

    entropy = EntropyAdapter(settings, instance_index=1)

    assert entropy.name == "entropy_account_1"
    assert entropy._wallet_address == "entropy-1"
    assert entropy._private_key == "key-1"
    assert entropy._transport_url == "http://entropy-proxy:8080"


async def test_farming_restores_all_legacy_venue_names(tmp_path):
    db_path = tmp_path / "farming.db"
    settings = Settings(_env_file=None, farming_sqlite_path=db_path)
    adapters = {
        f"{name}_account_0": AsyncMock()
        for name in (
            "nado",
            "entropy",
            "polymarket_perps",
            "txflow",
            "lighter_robinhood",
            "arcus",
        )
    }
    engine = FarmingEngine(adapters, settings, instance_index=0)
    await engine.connect()
    await engine.disconnect()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO farming_positions "
            "(id,symbol,long_venue,short_venue,quantity,notional_usd,long_entry,"
            "short_entry,entry_fees,opened_at,expected_cash_pnl,closed_at,close_reason,"
            "net_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                "BTC",
                "polymarket_perps",
                "txflow",
                "1",
                "50",
                "100",
                "101",
                "0.01",
                "2026-10-13T00:00:00+00:00",
                "0.10",
                None,
                None,
                None,
            ),
        )
    restored = FarmingEngine(adapters, settings, instance_index=0)
    await restored.connect()
    try:
        position = restored.positions["legacy"]
        assert position.long_venue == "polymarket_perps_account_0"
        assert position.short_venue == "txflow_account_0"
    finally:
        await restored.disconnect()
