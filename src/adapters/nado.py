"""Nado public market data using the documented gateway and archive protocols.

References:
https://docs.nado.xyz/developer-resources/api/symbols
https://docs.nado.xyz/developer-resources/api/gateway/queries/market-liquidity
https://docs.nado.xyz/developer-resources/api/archive-indexer/funding-rate
Live writes remain disabled until signed orders, fills and cash events are reconciled.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp

from src.adapters.base import ExchangeAdapter
from src.models import Balance, FeeRate, FundingRate, Orderbook, OrderbookLevel, OrderResult

X18 = Decimal("1000000000000000000")
FUNDING_INTERVAL_HOURS = 24  # API returns a 24-hour rate, not an 8-hour rate.
LIVE_UNAVAILABLE = (
    "Nado live trading is disabled: signed orders, confirmed fills and "
    "funding settlement are not implemented"
)


class NadoAdapter(ExchangeAdapter):
    supports_live = False
    funding_model = "index"

    async def get_funding_indices(self, symbol):
        product = (await self._load_products())[symbol]
        data = await self._query("all_products")
        for item in data["perp_products"]:
            if item["product_id"] == product["product_id"]:
                state = item["state"]
                return (Decimal(state["cumulative_funding_long_x18"]) / X18,
                        Decimal(state["cumulative_funding_short_x18"]) / X18)
        raise ValueError(f"No Nado funding indices for {symbol}")

    def __init__(self, settings):
        self._settings = settings
        self._session = None
        self._rest_url = settings.nado_rest_url.rstrip("/")
        self._products = {}
        self._products_at = 0.0
        self._products_lock = asyncio.Lock()
        self._ws_task = None

    @property
    def name(self):
        return "nado"

    async def connect(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
        )

    async def disconnect(self):
        if self._ws_task:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
            self._ws_task = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _query(self, method, params=None):
        assert self._session is not None
        async with self._session.get(
            f"{self._rest_url}/query", params={"type": method, **(params or {})}
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("status") != "success":
            raise ValueError(f"Nado query failed: {payload.get('error_code', 'invalid response')}")
        return payload["data"]

    async def _archive(self, payload):
        assert self._session is not None
        async with self._session.post(self._settings.nado_archive_url, json=payload) as response:
            response.raise_for_status()
            return await response.json()

    async def _load_products(self):
        async with self._products_lock:
            if self._products and time.monotonic() - self._products_at < 300:
                return self._products
            assert self._session is not None
            async with self._session.get(f"{self._rest_url}/symbols") as response:
                response.raise_for_status()
                items = await response.json()
            self._products = {
                item["symbol"].removesuffix("-PERP"): item
                for item in items
                if item["type"] == "perp" and item.get("trading_status") == "live"
            }
            self._products_at = time.monotonic()
            return self._products

    async def get_symbols(self):
        return list(await self._load_products())

    async def get_funding_rates(self):
        products = await self._load_products()
        if not products:
            return []
        data = await self._archive(
            {"funding_rates": {"product_ids": [item["product_id"] for item in products.values()]}}
        )
        rates = []
        for symbol, product in products.items():
            item = data.get(str(product["product_id"]))
            if not item:
                continue
            timestamp = datetime.fromtimestamp(int(item["update_time"]), tz=UTC)
            rates.append(
                FundingRate(
                    self.name,
                    symbol,
                    Decimal(item["funding_rate_x18"]) / X18,
                    FUNDING_INTERVAL_HOURS,
                    None,
                    timestamp=timestamp,
                )
            )
        return rates

    async def get_funding_rate(self, symbol):
        for rate in await self.get_funding_rates():
            if rate.symbol == symbol:
                return rate
        raise ValueError(f"No Nado funding rate for {symbol}")

    async def get_orderbook(self, symbol, depth=20):
        product = (await self._load_products())[symbol]
        data = await self._query(
            "market_liquidity", {"product_id": product["product_id"], "depth": min(depth, 100)}
        )

        def levels(side):
            return [
                OrderbookLevel(Decimal(price) / X18, Decimal(size) / X18)
                for price, size in data[side][:depth]
            ]

        return Orderbook(
            self.name,
            symbol,
            levels("bids"),
            levels("asks"),
            datetime.fromtimestamp(int(data["timestamp"]) / 1_000_000_000, tz=UTC),
        )

    async def get_mark_price(self, symbol):
        book = await self.get_orderbook(symbol, depth=1)
        if not book.bids or not book.asks:
            raise ValueError(f"Incomplete Nado orderbook for {symbol}")
        return (book.bids[0].price + book.asks[0].price) / 2

    async def get_fee_rate(self, symbol):
        product = (await self._load_products())[symbol]
        return FeeRate(
            self.name,
            symbol,
            Decimal(product["maker_fee_rate_x18"]) / X18,
            Decimal(product["taker_fee_rate_x18"]) / X18,
        )

    async def get_balance(self):
        if not self._settings.nado_wallet_address:
            return Balance(self.name, Decimal("0"), Decimal("0"))
        raise NotImplementedError(LIVE_UNAVAILABLE)

    async def get_positions(self):
        raise NotImplementedError(LIVE_UNAVAILABLE)

    async def place_order(self, order):
        return self._error_result(order, LIVE_UNAVAILABLE)

    async def close_position(self, symbol, side):
        raise NotImplementedError(LIVE_UNAVAILABLE)

    async def cancel_order(self, symbol, order_id):
        raise NotImplementedError(LIVE_UNAVAILABLE)

    async def subscribe_funding(self, callback):
        # No fictitious WS stream: the documented funding endpoint is polled.
        if self._ws_task and not self._ws_task.done():
            return

        async def poll():
            while True:
                for rate in await self.get_funding_rates():
                    callback(rate)
                await asyncio.sleep(self._settings.scan_interval_sec)

        self._ws_task = asyncio.create_task(poll())

    def _parse_execute_result(self, order, response, estimated_sz=None, estimated_price=None):
        # A place_order acknowledgement contains a digest, not execution details.
        result = self._error_result(order, "Unconfirmed execution: acknowledgement is not a fill")
        result.order_id = str(response.get("data", {}).get("digest", ""))
        result.uncertain = response.get("status") == "success"
        return result

    @staticmethod
    def _error_result(order, error):
        return OrderResult(
            order.exchange,
            order.symbol,
            order.client_id,
            order.side,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            False,
            error,
        )
