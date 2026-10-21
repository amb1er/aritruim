"""Market data and signed orders for the Lighter domain on Robinhood Chain."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import aiohttp
import structlog

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


class LighterRobinhoodAdapter(ExchangeAdapter):
    """Lighter Robinhood market data and optional signed live orders."""

    funding_model = "hourly_history"
    FUNDING_INTERVAL_HOURS = 8  # /funding-rates is normalized to an 8h equivalent.

    def __init__(
        self,
        settings,
        instance_index: int | None = None,
        transport_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._instance_index = instance_index
        self._adapter_name = (
            "lighter_robinhood"
            if instance_index is None
            else f"lighter_robinhood_account_{instance_index}"
        )
        self._transport_url = transport_url
        self._address = ""
        self._api_private_key = ""
        self._account_index: int | None = None
        self._api_key_index: int | None = None
        if instance_index is not None:
            accounts = settings.resolved_lighter_robinhood_accounts()
            if accounts and instance_index >= len(accounts):
                raise IndexError(
                    f"Lighter Robinhood account index {instance_index} is not configured"
                )
            if accounts:
                account = accounts[instance_index]
                self._address = account.address
                self._api_private_key = account.credential
                self._transport_url = transport_url or account.transport_url
                if instance_index < len(settings.lighter_robinhood_account_indices):
                    self._account_index = settings.lighter_robinhood_account_indices[instance_index]
                if instance_index < len(settings.lighter_robinhood_api_key_indices):
                    self._api_key_index = settings.lighter_robinhood_api_key_indices[instance_index]
        self._log = structlog.get_logger(__name__).bind(instance_index=instance_index)
        self._base_url = settings.lighter_robinhood_rest_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._signer = None
        self._markets: dict[str, dict] = {}
        self._market_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._adapter_name

    @property
    def supports_live(self) -> bool:
        return bool(
            self._address and self._api_private_key
            and self._account_index is not None and self._api_key_index is not None
        )

    async def connect(self) -> None:
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
                proxy=self._transport_url,
            )
            await self._load_markets(force=True)
            if self.supports_live and self._settings.farming_mode == BotMode.LIVE:
                import lighter

                self._signer = lighter.SignerClient(
                    url=self._base_url,
                    account_index=self._account_index,
                    api_private_keys={self._api_key_index: self._api_private_key},
                    chain_id=466324,
                )
                self._signer.api_client.rest_client.proxy = self._transport_url
                error = self._signer.check_client()
                if error:
                    raise RuntimeError("Lighter API key validation failed")
                await self._account()
                if await self._active_orders():
                    raise RuntimeError("Lighter account has active orders; reconcile before live")
            self._log.info(
                "lighter_robinhood.connected",
                exchange=self.name,
                transport_configured=bool(self._transport_url),
            )
        except Exception as exc:
            if self._signer is not None:
                await self._signer.close()
                self._signer = None
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._log.error(
                "lighter_robinhood.connect_failed",
                exchange=self.name,
                error_type=type(exc).__name__,
            )
            raise

    async def disconnect(self) -> None:
        if self._signer is not None:
            await self._signer.close()
            self._signer = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(
        self, path: str, params: dict | None = None, headers: dict | None = None
    ) -> dict:
        if self._session is None:
            raise RuntimeError("Lighter Robinhood adapter is not connected")
        async with self._session.get(
            f"{self._base_url}{path}", params=params, headers=headers
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("code") != 200:
            raise ValueError(f"Lighter API failure: {payload}")
        return payload

    async def _load_markets(self, force: bool = False) -> dict[str, dict]:
        if self._markets and not force:
            return self._markets
        async with self._market_lock:
            if self._markets and not force:
                return self._markets
            payload = await self._get("/api/v1/orderBookDetails")
            self._markets = {
                item["symbol"].upper(): item
                for item in payload.get("order_book_details", [])
                if item.get("market_type") == "perp" and item.get("status") == "active"
            }
        return self._markets

    async def _market(self, symbol: str) -> dict:
        markets = await self._load_markets()
        try:
            return markets[symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown Lighter Robinhood market: {symbol}") from exc

    async def get_symbols(self) -> list[str]:
        return sorted(await self._load_markets())

    async def get_funding_rates(self) -> list[FundingRate]:
        markets = await self._load_markets()
        payload = await self._get("/api/v1/funding-rates")
        now = utc_now()
        return [
            FundingRate(
                exchange=self.name,
                symbol=item["symbol"].upper(),
                rate=Decimal(str(item["rate"])),
                interval_hours=self.FUNDING_INTERVAL_HOURS,
                next_funding_ts=now.replace(minute=0, second=0, microsecond=0)
                + timedelta(hours=1),
                timestamp=now,
            )
            for item in payload.get("funding_rates", [])
            if item.get("exchange") == "lighter" and item.get("symbol", "").upper() in markets
        ]

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        symbol = symbol.upper()
        for rate in await self.get_funding_rates():
            if rate.symbol == symbol:
                return rate
        raise ValueError(f"No Lighter Robinhood funding rate for {symbol}")

    async def get_funding_history(
        self, symbol: str, lookback_hours: int = 24
    ) -> list[FundingRate]:
        market = await self._market(symbol)
        end = int(utc_now().timestamp())
        start = end - max(1, lookback_hours) * 3600
        payload = await self._get(
            "/api/v1/fundings",
            {
                "market_id": market["market_id"],
                "resolution": "1h",
                "start_timestamp": start,
                "end_timestamp": end,
                "count_back": min(750, max(1, lookback_hours)),
            },
        )
        result = []
        for item in payload.get("fundings", []):
            # Historical API expresses rate in percent per hour.
            rate = Decimal(str(item["rate"])) / Decimal("100")
            if item.get("direction") == "short":
                rate = -rate
            result.append(
                FundingRate(
                    self.name,
                    symbol.upper(),
                    rate,
                    1,
                    None,
                    datetime.fromtimestamp(int(item["timestamp"]), UTC),
                )
            )
        return result

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        market = await self._market(symbol)
        payload = await self._get(
            "/api/v1/orderBookOrders",
            {"market_id": market["market_id"], "limit": min(max(depth, 1), 100)},
        )

        def levels(side: str, reverse: bool) -> list[OrderbookLevel]:
            values = [
                OrderbookLevel(
                    Decimal(str(row["price"])),
                    Decimal(str(row["remaining_base_amount"])),
                )
                for row in payload.get(side, [])
                if Decimal(str(row["price"])) > 0
                and Decimal(str(row["remaining_base_amount"])) > 0
            ]
            return sorted(values, key=lambda level: level.price, reverse=reverse)[:depth]

        return Orderbook(self.name, symbol.upper(), levels("bids", True), levels("asks", False))

    async def get_mark_price(self, symbol: str) -> Decimal:
        await self._load_markets(force=True)
        market = await self._market(symbol)
        return Decimal(str(market["mark_price"]))

    async def get_funding_price(self, symbol: str) -> Decimal:
        await self._load_markets(force=True)
        market = await self._market(symbol)
        return Decimal(str(market["index_price"]))

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        market = await self._market(symbol)
        return FeeRate(
            self.name,
            symbol.upper(),
            Decimal(str(market.get("maker_fee", "0"))),
            Decimal(str(market.get("taker_fee", "0"))),
        )

    async def get_balance(self) -> Balance:
        if not self.supports_live:
            return Balance(self.name, Decimal("0"), Decimal("0"))
        account = await self._account()
        return Balance(
            self.name,
            Decimal(str(account["collateral"])),
            Decimal(str(account["available_balance"])),
        )

    async def get_positions(self) -> list[Position]:
        if not self.supports_live:
            return []
        account = await self._account()
        return [
            Position(
                self.name,
                str(item["symbol"]).upper(),
                OrderSide.LONG if int(item["sign"]) > 0 else OrderSide.SHORT,
                abs(Decimal(str(item["position"]))),
                Decimal(str(item["avg_entry_price"])),
                Decimal(str(item["unrealized_pnl"])),
            )
            for item in account.get("positions", [])
            if Decimal(str(item["position"])) != 0
        ]

    async def _account(self) -> dict:
        if self._account_index is None:
            raise RuntimeError("Lighter account index is not configured")
        payload = await self._get(
            "/api/v1/account", {"by": "index", "value": str(self._account_index)}
        )
        accounts = payload.get("accounts", [])
        account = next(
            (item for item in accounts if int(item["index"]) == self._account_index), None
        )
        if account is None or account.get("l1_address", "").lower() != self._address.lower():
            raise RuntimeError("Lighter account index does not match configured wallet")
        return account

    def _auth_token(self) -> str:
        if self._signer is None or self._api_key_index is None:
            raise RuntimeError("Lighter signer is not connected")
        token, error = self._signer.create_auth_token_with_expiry(
            api_key_index=self._api_key_index
        )
        if error or not token:
            raise RuntimeError("Lighter account authentication failed")
        return token

    async def _active_orders(self) -> list[dict]:
        payload = await self._get(
            "/api/v1/accountActiveOrders",
            {"account_index": self._account_index},
            headers={"authorization": self._auth_token()},
        )
        return payload.get("orders", [])

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if not self.supports_live or self._signer is None:
            raise RuntimeError("Lighter live account is not configured")
        if order.exchange != self.name:
            raise ValueError("Order exchange does not match Lighter instance")
        if await self._active_orders():
            raise RuntimeError("Lighter account has active orders; reconcile before trading")
        market = await self._market(order.symbol)
        book = await self.get_orderbook(order.symbol, 100)
        price = book.execution_price(order.side, order.quantity or Decimal("0"))
        if price is None:
            raise ValueError("Lighter orderbook cannot fill requested quantity")
        size_scale = Decimal(10) ** int(market["supported_size_decimals"])
        price_scale = Decimal(10) ** int(market["supported_price_decimals"])
        size_int = int(((order.quantity or Decimal("0")) * size_scale).to_integral_value(
            rounding=ROUND_FLOOR
        ))
        if size_int <= 0:
            raise ValueError("Lighter quantity is below market precision")
        quantity = Decimal(size_int) / size_scale
        if quantity < Decimal(str(market["min_base_amount"])):
            raise ValueError("Lighter quantity is below market minimum")
        if not 0 <= self._settings.max_slippage < 1:
            raise ValueError("Lighter max_slippage must be between zero and one")
        is_ask = order.side == OrderSide.SHORT
        worst = price * (1 - self._settings.max_slippage if is_ask
                         else 1 + self._settings.max_slippage)
        price_int = int((worst * price_scale).to_integral_value(
            rounding=ROUND_FLOOR if is_ask else ROUND_CEILING
        ))
        client_index = int.from_bytes(
            hashlib.sha256(order.client_id.encode()).digest()[:8], "big"
        ) & ((1 << 63) - 1)
        if client_index == 0:
            client_index = 1
        submitted = False
        try:
            if not order.is_reduce_only:
                if order.leverage != order.leverage.to_integral_value() or order.leverage < 1:
                    raise ValueError("Lighter leverage must be a positive integer")
                _, leverage_response, leverage_error = await self._signer.update_leverage(
                    market_index=int(market["market_id"]),
                    margin_mode=self._signer.CROSS_MARGIN_MODE,
                    leverage=int(order.leverage),
                    api_key_index=self._api_key_index,
                )
                if leverage_error or leverage_response is None or leverage_response.code != 200:
                    raise RuntimeError("Lighter leverage update was not confirmed")
            submitted = True
            _, response, error = await self._signer.create_market_order(
                market_index=int(market["market_id"]),
                client_order_index=client_index,
                base_amount=size_int,
                avg_execution_price=price_int,
                is_ask=is_ask,
                reduce_only=order.is_reduce_only,
                api_key_index=self._api_key_index,
            )
            if error:
                submitted = False
                return OrderResult(self.name, order.symbol, str(client_index), order.side,
                                   Decimal("0"), Decimal("0"), Decimal("0"), False, str(error))
            if response is None or response.code != 200:
                raise RuntimeError("Lighter rejected order submission")
            submitted = True
            token = self._auth_token()
            for _ in range(12):
                payload = await self._get(
                    "/api/v1/accountOrders",
                    {"client_order_indexes": str(client_index),
                     "account_index": self._account_index},
                    headers={"authorization": token},
                )
                rows = [item for item in payload.get("orders", [])
                        if int(item["client_order_index"]) == client_index]
                if rows:
                    item = rows[0]
                    if (
                        int(item["owner_account_index"]) != self._account_index
                        or int(item["market_index"]) != int(market["market_id"])
                        or bool(item["is_ask"]) != is_ask
                        or bool(item["reduce_only"]) != order.is_reduce_only
                    ):
                        raise RuntimeError("Lighter order confirmation does not match request")
                    status = item["status"]
                    if status == "filled" or status == "canceled" or status.startswith("canceled-"):
                        filled = Decimal(str(item["filled_base_amount"]))
                        quote = Decimal(str(item["filled_quote_amount"]))
                        if (
                            filled < 0 or filled > quantity or quote < 0
                            or (filled > 0 and quote == 0)
                        ):
                            raise RuntimeError("Lighter order fill has invalid size or quote")
                        average = quote / filled if filled > 0 else Decimal("0")
                        fee = quote * Decimal(str(market["taker_fee"]))
                        return OrderResult(self.name, order.symbol,
                                           str(item["order_index"]), order.side,
                                           filled, average, fee, filled > 0)
                await asyncio.sleep(0.5)
            raise TimeoutError("Lighter order execution was not confirmed")
        except Exception as exc:
            return OrderResult(self.name, order.symbol, str(client_index), order.side,
                               Decimal("0"), Decimal("0"), Decimal("0"), False,
                               str(exc), uncertain=submitted)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if self._signer is None or self._api_key_index is None:
            raise RuntimeError("Lighter signer is not connected")
        market = await self._market(symbol)
        _, response, error = await self._signer.cancel_order(
            market_index=int(market["market_id"]),
            order_index=int(order_id),
            api_key_index=self._api_key_index,
        )
        if error:
            raise RuntimeError(str(error))
        return response is not None and response.code == 200

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        positions = [item for item in await self.get_positions()
                     if item.symbol == symbol.upper() and item.side == side]
        if len(positions) != 1:
            raise ValueError("Expected exactly one Lighter position to close")
        close_side = OrderSide.SHORT if side == OrderSide.LONG else OrderSide.LONG
        return await self.place_order(OrderRequest(
            self.name, symbol.upper(), close_side, Decimal("0"), Decimal("1"),
            is_reduce_only=True, client_id=f"close:{symbol}:{utc_now().timestamp()}",
            quantity=positions[0].size,
        ))

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
