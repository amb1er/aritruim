"""Nado public market data using the documented gateway and archive protocols.

References:
https://docs.nado.xyz/developer-resources/api/symbols
https://docs.nado.xyz/developer-resources/api/gateway/queries/market-liquidity
https://docs.nado.xyz/developer-resources/api/archive-indexer/funding-rate
Farming live orders use signed IOC orders and archive fill confirmation.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import aiohttp
import structlog
from eth_account import Account

from src.adapters.base import ExchangeAdapter
from src.models import (
    Balance,
    BotMode,
    FeeRate,
    FundingRate,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderResult,
    OrderSide,
    Position,
    utc_now,
)

X18 = Decimal("1000000000000000000")
FUNDING_INTERVAL_HOURS = 24  # API returns a 24-hour rate, not an 8-hour rate.
LIVE_UNAVAILABLE = "Nado live trading is disabled in the main arbitrage process"
log = structlog.get_logger(__name__)


class NadoAdapter(ExchangeAdapter):
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

    def __init__(
        self,
        settings,
        instance_index: int = 0,
        transport_url: str | None = None,
    ):
        self._settings = settings
        self._instance_index = instance_index
        self._transport_url = transport_url
        self._adapter_name = f"nado_account_{instance_index}"
        account = settings.resolved_nado_accounts()[instance_index]
        self._wallet_address = account.address
        self._private_key = account.credential
        self._log = log.bind(instance_index=instance_index)
        self._session = None
        self._rest_url = settings.nado_rest_url.rstrip("/")
        self._products = {}
        self._products_at = 0.0
        self._products_lock = asyncio.Lock()
        self._ws_task = None
        self._chain_id = None
        self._subaccount = None

    @property
    def name(self):
        return self._adapter_name

    @property
    def supports_live(self):
        return bool(self._wallet_address and self._private_key)

    async def connect(self):
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
                proxy=self._transport_url,
            )
            if self.supports_live and self._settings.farming_mode == BotMode.LIVE:
                wallet = Account.from_key(self._private_key)
                if wallet.address.lower() != self._wallet_address.lower():
                    raise ValueError("Nado private key does not match configured wallet")
                self._subaccount = (
                    "0x" + self._wallet_address[2:].lower()
                    + b"default".hex().ljust(24, "0")
                )
                contracts = await self._query("contracts")
                self._chain_id = int(contracts["chain_id"])
                await self._account_info()
                products = await self._load_products()
                product_ids = [int(item["product_id"]) for item in products.values()]
                for start in range(0, len(product_ids), 8):
                    results = await asyncio.gather(*(
                        self._query("subaccount_orders", {
                            "sender": self._subaccount, "product_id": product_id,
                        })
                        for product_id in product_ids[start:start + 8]
                    ))
                    if any(result["orders"] for result in results):
                        raise RuntimeError("Nado account has resting orders; reconcile before live")
                    if start + 8 < len(product_ids):
                        await asyncio.sleep(0.25)
        except Exception as exc:
            if self._session:
                await self._session.close()
                self._session = None
            self._log.error(
                "nado.connection_failed",
                exchange=self.name,
                transport_configured=bool(self._transport_url),
                error_type=type(exc).__name__,
            )
            raise ConnectionError(f"Could not connect {self.name}") from exc
        self._log.info("nado.connected", exchange=self.name, url=self._rest_url)

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
        if not self.supports_live:
            return Balance(self.name, Decimal("0"), Decimal("0"))
        info = await self._account_info()
        return Balance(
            self.name,
            Decimal(info["healths"][2]["health"]) / X18,
            Decimal(info["healths"][0]["health"]) / X18,
        )

    async def get_positions(self):
        info = await self._account_info()
        products = await self._load_products()
        symbols = {int(product["product_id"]): symbol for symbol, product in products.items()}
        positions = []
        for item in info["perp_balances"]:
            amount = Decimal(item["balance"]["amount"]) / X18
            if amount == 0:
                continue
            product_id = int(item["product_id"])
            if product_id not in symbols:
                raise RuntimeError(f"Nado open position has unknown product {product_id}")
            quote = Decimal(item["balance"]["v_quote_balance"]) / X18
            positions.append(Position(
                self.name, symbols[product_id],
                OrderSide.LONG if amount > 0 else OrderSide.SHORT,
                abs(amount), abs(quote / amount), Decimal("0"),
            ))
        return positions

    async def _account_info(self):
        if self._subaccount is None:
            raise RuntimeError("Nado live subaccount is not connected")
        info = await self._query("subaccount_info", {"subaccount": self._subaccount})
        if not info.get("exists") or info.get("subaccount", "").lower() != self._subaccount:
            raise RuntimeError("Nado subaccount is unavailable or mismatched")
        return info

    async def _execute(self, payload):
        if self._session is None:
            raise RuntimeError("Nado is not connected")
        async with self._session.post(f"{self._rest_url}/execute", json=payload) as response:
            response.raise_for_status()
            return await response.json()

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if self._settings.farming_mode != BotMode.LIVE or self._chain_id is None:
            return self._error_result(order, "Nado live trading disabled: signer is not connected")
        if order.exchange != self.name:
            return self._error_result(order, "Nado order exchange does not match account")
        submitted = False
        digest = order.client_id
        try:
            product = (await self._load_products())[order.symbol]
            product_id = int(product["product_id"])
            if product.get("isolated_only") and not order.is_reduce_only:
                raise ValueError("Nado market requires isolated margin")
            active = await self._query("subaccount_orders", {
                "sender": self._subaccount, "product_id": product_id,
            })
            if active["orders"]:
                raise RuntimeError("Nado account has active orders in this market")
            size_step = int(product["size_increment"])
            price_step = int(product["price_increment_x18"])
            if size_step <= 0 or price_step <= 0:
                raise ValueError("Nado market precision is invalid")
            requested = order.quantity or Decimal("0")
            if not requested.is_finite() or requested <= 0:
                raise ValueError("Nado order quantity must be positive")
            amount = int((requested * X18).to_integral_value(rounding=ROUND_FLOOR))
            amount = amount // size_step * size_step
            if amount <= 0:
                raise ValueError("Nado order quantity is below market precision")
            book = await self.get_orderbook(order.symbol, 100)
            average = book.execution_price(order.side, Decimal(amount) / X18)
            if average is None:
                raise ValueError("Nado orderbook cannot fill requested quantity")
            slip = self._settings.max_slippage
            if not slip.is_finite() or not 0 <= slip < 1:
                raise ValueError("Nado max_slippage must be between zero and one")
            is_sell = order.side == OrderSide.SHORT
            worst = average * (1 - slip if is_sell else 1 + slip)
            raw_price = worst * X18 / price_step
            price = int(raw_price.to_integral_value(
                rounding=ROUND_FLOOR if is_sell else ROUND_CEILING
            )) * price_step
            if price <= 0 or Decimal(amount * price) / X18 < int(product["min_size"]):
                raise ValueError("Nado order is below market minimum or has invalid price")
            if is_sell:
                amount = -amount
            expires = int(time.time()) + 30
            nonce = ((int(time.time() * 1000) + 30_000) << 20) | secrets.randbits(20)
            appendix = 1 | (1 << 9) | ((1 << 11) if order.is_reduce_only else 0)
            message = {
                "sender": self._subaccount,
                "priceX18": price,
                "amount": amount,
                "expiration": expires,
                "nonce": nonce,
                "appendix": appendix,
            }
            signature = Account.sign_typed_data(
                self._private_key,
                domain_data={
                    "name": "Nado", "version": "0.0.1", "chainId": self._chain_id,
                    "verifyingContract": f"0x{product_id:040x}",
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
            ).signature.hex()
            payload = {"place_order": {
                "product_id": product_id,
                "order": {key: str(value) for key, value in message.items()},
                "signature": "0x" + signature,
            }}
            submitted = True
            response = await self._execute(payload)
            if response.get("status") == "failure":
                return self._error_result(order, response.get("error", "Nado rejected order"))
            if response.get("status") != "success":
                raise RuntimeError("Nado order submission has an unknown response")
            digest = response["data"]["digest"]
            for _ in range(20):
                archived = await self._archive({"orders": {"digests": [digest], "limit": 1}})
                rows = archived.get("orders", [])
                if rows:
                    row = rows[0]
                    if (
                        row["digest"].lower() != digest.lower()
                        or row["subaccount"].lower() != self._subaccount
                        or int(row["product_id"]) != product_id
                        or int(row["amount"]) != amount
                        or int(row["nonce"]) != nonce
                        or int(row["appendix"]) != appendix
                    ):
                        raise RuntimeError("Nado archived fill does not match signed order")
                    filled = abs(Decimal(row["base_filled"]) / X18)
                    quote = abs(Decimal(row["quote_filled"]) / X18)
                    fee = Decimal(row["fee"]) / X18 + Decimal(row.get("builder_fee", 0)) / X18
                    if (
                        filled <= 0 or filled > abs(Decimal(amount) / X18)
                        or quote <= 0 or fee < 0
                        or (Decimal(row["base_filled"]) > 0) == is_sell
                        or (Decimal(row["quote_filled"]) > 0) != is_sell
                    ):
                        raise RuntimeError("Nado archived fill is invalid")
                    return OrderResult(
                        self.name, order.symbol, digest, order.side,
                        filled, quote / filled, fee, True,
                    )
                await asyncio.sleep(0.5)
            raise TimeoutError("Nado order fill was not confirmed in archive")
        except Exception as exc:
            return OrderResult(
                self.name, order.symbol, digest, order.side,
                Decimal("0"), Decimal("0"), Decimal("0"), False,
                str(exc), uncertain=submitted,
            )

    async def close_position(self, symbol, side):
        if self._chain_id is None:
            raise NotImplementedError("Nado live trading is disabled: signer is not connected")
        positions = [item for item in await self.get_positions()
                     if item.symbol == symbol and item.side == side]
        if len(positions) != 1:
            raise ValueError("Expected exactly one Nado position to close")
        close_side = OrderSide.SHORT if side == OrderSide.LONG else OrderSide.LONG
        return await self.place_order(OrderRequest(
            self.name, symbol, close_side, Decimal("0"), Decimal("1"),
            is_reduce_only=True, client_id=f"close:{symbol}:{utc_now().timestamp()}",
            quantity=positions[0].size,
        ))

    async def cancel_order(self, symbol, order_id):
        raise NotImplementedError("Nado IOC orders have no resting order to cancel")

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
