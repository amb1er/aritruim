from decimal import Decimal as D
from unittest.mock import AsyncMock

from src.adapters.arcus import ArcusAdapter
from src.config import Settings


def market():
    return {
        "marketDisplayName": "BTC-USD",
        "marketId": 1,
        "status": "ONLINE",
        "type": "PERPETUAL",
        "baseAsset": "BTC",
        "markPrice": "81996.2",
        "oraclePrice": "82014.8",
        "fundingRate": "0.0000125",
        "nextFundingAt": 1792101600,
    }


async def test_arcus_market_data_units_and_bbo():
    adapter = ArcusAdapter(Settings(_env_file=None))

    async def get(path, params=None):
        if path == "/v1/markets":
            return {"markets": [market()]}
        if path == "/v1/feetiers":
            return {
                "tiers": [
                    {
                        "level": 0,
                        "maker_fee_ppm": 0,
                        "taker_fee_ppm": 225,
                    }
                ]
            }
        if path == "/v1/bbo/BTC-USD":
            return {
                "bestBid": {"price": "81981.4", "size": "0.29300133"},
                "bestAsk": {"price": "81981.5", "size": "0.69779193"},
                "timestamp": 1792098010692262,
            }
        raise AssertionError((path, params))

    adapter._get = AsyncMock(side_effect=get)
    await adapter._load_markets(force=True)
    await adapter._load_fees()

    rates = await adapter.get_funding_rates()
    book = await adapter.get_orderbook("BTC")
    fee = await adapter.get_fee_rate("BTC")

    assert rates[0].symbol == "BTC"
    assert rates[0].rate == D("0.0000125")
    assert rates[0].interval_hours == 1
    assert book.bids[0].price == D("81981.4")
    assert book.asks[0].size == D("0.69779193")
    assert fee.maker == D("0")
    assert fee.taker == D("0.000225")


async def test_arcus_funding_history_uses_microsecond_timestamps():
    adapter = ArcusAdapter(Settings(_env_file=None))
    adapter._markets = {"BTC": market()}
    adapter._get = AsyncMock(
        return_value={
            "fundingRates": [
                {
                    "marketDisplayName": "BTC-USD",
                    "fundingRate": "-0.00002",
                    "time": 1792098000000000,
                }
            ]
        }
    )

    history = await adapter.get_funding_history("BTC", 24)

    assert history[0].rate == D("-0.00002")
    assert history[0].timestamp.timestamp() == 1792098000


def test_arcus_multi_instance_transport():
    settings = Settings(
        _env_file=None,
        arcus_accounts="address:key:http://proxy.example:8080",
    )
    adapter = ArcusAdapter(settings, instance_index=0)

    assert adapter.name == "arcus_account_0"
    assert adapter._transport_url == "http://proxy.example:8080"
