"""Public market-data adapter for the Lighter domain on Robinhood Chain."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp
import structlog

from src.adapters.base import ExchangeAdapter
from src.models import (
    Balance,
    FeeRate,
    FundingRate,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderResult,
    Position,
    utc_now,
)


class LighterRobinhoodAdapter(ExchangeAdapter):
    """Lighter Robinhood Domain; live writes remain deliberately disabled."""

    supports_live = False
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
        if instance_index is not None:
            accounts = settings.resolved_lighter_robinhood_accounts()
            if accounts and instance_index >= len(accounts):
                raise IndexError(
                    f"Lighter Robinhood account index {instance_index} is not configured"
                )
            if accounts:
                self._transport_url = transport_url or accounts[instance_index].transport_url
        self._log = structlog.get_logger(__name__).bind(instance_index=instance_index)
        self._base_url = settings.lighter_robinhood_rest_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._markets: dict[str, dict] = {}
        self._market_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._adapter_name

    async def connect(self) -> None:
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
                proxy=self._transport_url,
            )
            await self._load_markets(force=True)
            self._log.info(
                "lighter_robinhood.connected",
                exchange=self.name,
                transport_configured=bool(self._transport_url),
            )
        except Exception as exc:
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._log.error(
                "lighter_robinhood.connect_failed",
                exchange=self.name,
                error=str(exc),
            )
            raise

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if self._session is None:
            raise RuntimeError("Lighter Robinhood adapter is not connected")
        async with self._session.get(f"{self._base_url}{path}", params=params) as response:
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
        return Balance(self.name, Decimal("0"), Decimal("0"))

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("Lighter Robinhood live trading is not enabled")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError("Lighter Robinhood live trading is not enabled")

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        raise NotImplementedError("Lighter Robinhood live trading is not enabled")

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
