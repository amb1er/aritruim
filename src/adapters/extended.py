"""Public market-data adapter for Extended Exchange on Starknet."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp

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


class ExtendedAdapter(ExchangeAdapter):
    """Extended public data and paper execution; live writes are disabled."""

    supports_live = False
    funding_model = "hourly_history"
    FUNDING_INTERVAL_HOURS = 1

    def __init__(self, settings) -> None:
        self._settings = settings
        self._base_url = settings.extended_rest_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._markets: dict[str, dict] = {}
        self._market_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "extended"

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
            headers={"User-Agent": "funding-arb-bot/0.1"},
        )
        await self._load_markets(force=True)

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if self._session is None:
            raise RuntimeError("Extended adapter is not connected")
        async with self._session.get(f"{self._base_url}{path}", params=params) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("status") != "OK":
            raise ValueError(f"Extended API failure: {payload}")
        return payload

    async def _load_markets(self, force: bool = False) -> dict[str, dict]:
        if self._markets and not force:
            return self._markets
        async with self._market_lock:
            if self._markets and not force:
                return self._markets
            payload = await self._get("/api/v1/info/markets")
            self._markets = {
                item["assetName"].upper(): item
                for item in payload.get("data", [])
                if item.get("type") == "PERPETUAL"
                and item.get("active") is True
                and item.get("status") == "ACTIVE"
            }
        return self._markets

    async def _market(self, symbol: str) -> dict:
        markets = await self._load_markets()
        try:
            return markets[symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown Extended market: {symbol}") from exc

    async def get_symbols(self) -> list[str]:
        return sorted(await self._load_markets())

    async def get_funding_rates(self) -> list[FundingRate]:
        markets = await self._load_markets(force=True)
        now = utc_now()
        result = []
        for symbol, market in markets.items():
            stats = market.get("marketStats") or {}
            if stats.get("fundingRate") is None:
                continue
            next_ts = stats.get("nextFundingRate")
            result.append(
                FundingRate(
                    self.name,
                    symbol,
                    Decimal(str(stats["fundingRate"])),
                    self.FUNDING_INTERVAL_HOURS,
                    datetime.fromtimestamp(int(next_ts) / 1000, UTC)
                    if next_ts
                    else now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
                    now,
                )
            )
        return result

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        symbol = symbol.upper()
        for rate in await self.get_funding_rates():
            if rate.symbol == symbol:
                return rate
        raise ValueError(f"No Extended funding rate for {symbol}")

    async def get_funding_history(
        self, symbol: str, lookback_hours: int = 24
    ) -> list[FundingRate]:
        market = await self._market(symbol)
        end = utc_now()
        start = end - timedelta(hours=max(1, lookback_hours))
        payload = await self._get(
            f"/api/v1/info/{market['name']}/funding",
            {"startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000)},
        )
        return [
            FundingRate(
                self.name,
                symbol.upper(),
                Decimal(str(row["f"])),
                self.FUNDING_INTERVAL_HOURS,
                None,
                datetime.fromtimestamp(int(row["T"]) / 1000, UTC),
            )
            for row in payload.get("data", [])
        ]

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        market = await self._market(symbol)
        payload = await self._get(f"/api/v1/info/markets/{market['name']}/orderbook")
        data = payload.get("data") or {}

        def levels(key: str, reverse: bool) -> list[OrderbookLevel]:
            parsed = [
                OrderbookLevel(Decimal(str(row["price"])), Decimal(str(row["qty"])))
                for row in data.get(key, [])
                if Decimal(str(row["price"])) > 0 and Decimal(str(row["qty"])) > 0
            ]
            return sorted(parsed, key=lambda row: row.price, reverse=reverse)[:depth]

        return Orderbook(self.name, symbol.upper(), levels("bid", True), levels("ask", False))

    async def get_mark_price(self, symbol: str) -> Decimal:
        await self._load_markets(force=True)
        market = await self._market(symbol)
        return Decimal(str(market["marketStats"]["markPrice"]))

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        await self._market(symbol)
        return FeeRate(
            self.name,
            symbol.upper(),
            self._settings.extended_maker_fee,
            self._settings.extended_taker_fee,
        )

    async def get_balance(self) -> Balance:
        return Balance(self.name, Decimal("0"), Decimal("0"))

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("Extended live trading is not enabled")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError("Extended live trading is not enabled")

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        raise NotImplementedError("Extended live trading is not enabled")

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
