"""Arcus perpetual market data on Robinhood Chain."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
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


class ArcusAdapter(ExchangeAdapter):
    """Public Arcus REST adapter; signed trading remains disabled."""

    supports_live = False
    funding_model = "hourly_history"
    FUNDING_INTERVAL_HOURS = 1

    def __init__(
        self,
        settings,
        instance_index: int | None = None,
        transport_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._instance_index = instance_index
        self._adapter_name = (
            "arcus" if instance_index is None else f"arcus_account_{instance_index}"
        )
        self._transport_url = transport_url
        if instance_index is not None:
            accounts = settings.resolved_arcus_accounts()
            if accounts and instance_index >= len(accounts):
                raise IndexError(f"Arcus account index {instance_index} is not configured")
            if accounts:
                self._transport_url = transport_url or accounts[instance_index].transport_url
        self._base_url = settings.arcus_rest_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._markets: dict[str, dict] = {}
        self._fee_rate: FeeRate | None = None
        self._market_lock = asyncio.Lock()
        self._log = structlog.get_logger(__name__).bind(instance_index=instance_index)

    @property
    def name(self) -> str:
        return self._adapter_name

    async def connect(self) -> None:
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
                headers={"User-Agent": "funding-arb-bot/0.1"},
                proxy=self._transport_url,
            )
            await asyncio.gather(self._load_markets(force=True), self._load_fees())
            self._log.info(
                "arcus.connected",
                exchange=self.name,
                transport_configured=bool(self._transport_url),
            )
        except Exception as exc:
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._log.error("arcus.connect_failed", exchange=self.name, error=str(exc))
            raise

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if self._session is None:
            raise RuntimeError("Arcus adapter is not connected")
        async with self._session.get(f"{self._base_url}{path}", params=params) as response:
            response.raise_for_status()
            return await response.json()

    async def _load_markets(self, force: bool = False) -> dict[str, dict]:
        if self._markets and not force:
            return self._markets
        async with self._market_lock:
            if self._markets and not force:
                return self._markets
            payload = await self._get("/v1/markets")
            markets = {}
            for row in payload.get("markets", []):
                if row.get("status") != "ONLINE" or row.get("type") != "PERPETUAL":
                    continue
                symbol = str(row.get("baseAsset", "")).upper()
                if symbol:
                    markets[symbol] = row
            self._markets = markets
        return self._markets

    async def _load_fees(self) -> FeeRate:
        payload = await self._get("/v1/feetiers")
        tiers = payload.get("tiers", [])
        base = next((row for row in tiers if int(row.get("level", -1)) == 0), None)
        if base is None:
            raise ValueError("Arcus base fee tier is missing")
        scale = Decimal("1000000")
        self._fee_rate = FeeRate(
            self.name,
            "*",
            Decimal(str(base["maker_fee_ppm"])) / scale,
            Decimal(str(base["taker_fee_ppm"])) / scale,
        )
        return self._fee_rate

    async def _market(self, symbol: str) -> dict:
        markets = await self._load_markets()
        try:
            return markets[symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown Arcus market: {symbol}") from exc

    async def get_symbols(self) -> list[str]:
        return sorted(await self._load_markets())

    async def get_funding_rates(self) -> list[FundingRate]:
        markets = await self._load_markets(force=True)
        now = utc_now()
        return [
            FundingRate(
                exchange=self.name,
                symbol=symbol,
                rate=Decimal(str(row["fundingRate"])),
                interval_hours=self.FUNDING_INTERVAL_HOURS,
                next_funding_ts=datetime.fromtimestamp(int(row["nextFundingAt"]), UTC),
                timestamp=now,
            )
            for symbol, row in markets.items()
        ]

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        market = await self._market(symbol)
        return FundingRate(
            self.name,
            symbol.upper(),
            Decimal(str(market["fundingRate"])),
            self.FUNDING_INTERVAL_HOURS,
            datetime.fromtimestamp(int(market["nextFundingAt"]), UTC),
        )

    async def get_funding_history(
        self, symbol: str, lookback_hours: int = 24
    ) -> list[FundingRate]:
        market = await self._market(symbol)
        payload = await self._get(
            "/v1/fundingRates",
            {
                "market": market["marketDisplayName"],
                "limit": min(1000, max(1, lookback_hours)),
            },
        )
        return [
            FundingRate(
                self.name,
                symbol.upper(),
                Decimal(str(row["fundingRate"])),
                self.FUNDING_INTERVAL_HOURS,
                None,
                datetime.fromtimestamp(int(row["time"]) / 1_000_000, UTC),
            )
            for row in payload.get("fundingRates", [])
        ]

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        market = await self._market(symbol)
        payload = await self._get(f"/v1/bbo/{market['marketDisplayName']}")

        def level(value: object) -> list[OrderbookLevel]:
            if not isinstance(value, dict):
                return []
            price = Decimal(str(value.get("price", "0")))
            size = Decimal(str(value.get("size", "0")))
            return [OrderbookLevel(price, size)] if price > 0 and size > 0 else []

        timestamp = datetime.fromtimestamp(
            int(payload.get("timestamp", 0)) / 1_000_000, UTC
        )
        return Orderbook(
            self.name,
            symbol.upper(),
            level(payload.get("bestBid")),
            level(payload.get("bestAsk")),
            timestamp,
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        return Decimal(str(market["markPrice"]))

    async def get_funding_price(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        return Decimal(str(market["oraclePrice"]))

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        fee = self._fee_rate or await self._load_fees()
        return FeeRate(self.name, symbol.upper(), fee.maker, fee.taker)

    async def get_balance(self) -> Balance:
        return Balance(self.name, Decimal("0"), Decimal("0"))

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("Arcus live trading is not enabled")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError("Arcus live trading is not enabled")

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        raise NotImplementedError("Arcus live trading is not enabled")

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.farming_scan_interval_sec)
