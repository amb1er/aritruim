"""Public market-data adapter for the Hibachi perpetual exchange."""

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

log = structlog.get_logger(__name__)


class HibachiAdapter(ExchangeAdapter):
    """Hibachi public data and paper execution; live writes are disabled."""

    supports_live = False
    funding_model = "scheduled_estimate"
    FUNDING_INTERVAL_HOURS = 1

    def __init__(self, settings) -> None:
        self._settings = settings
        self._base_url = settings.hibachi_data_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._contracts: dict[str, dict] = {}
        self._fee_config: dict = {}
        self._market_lock = asyncio.Lock()
        self._funding_request_limit = asyncio.Semaphore(4)

    @property
    def name(self) -> str:
        return "hibachi"

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
            headers={"User-Agent": "funding-arb-bot/0.1"},
        )
        await self._load_contracts(force=True)

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if self._session is None:
            raise RuntimeError("Hibachi adapter is not connected")
        async with self._session.get(f"{self._base_url}{path}", params=params) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("status") == "failed":
            raise ValueError(f"Hibachi API failure: {payload}")
        return payload

    async def _load_contracts(self, force: bool = False) -> dict[str, dict]:
        if self._contracts and not force:
            return self._contracts
        async with self._market_lock:
            if self._contracts and not force:
                return self._contracts
            payload = await self._get("/market/exchange-info")
            self._fee_config = payload.get("feeConfig") or {}
            self._contracts = {
                item["underlyingSymbol"].upper(): item
                for item in payload.get("futureContracts", [])
                if item.get("status") == "LIVE" and item.get("symbolStatus") == "OPEN"
            }
        return self._contracts

    async def _contract(self, symbol: str) -> dict:
        contracts = await self._load_contracts()
        try:
            return contracts[symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown Hibachi market: {symbol}") from exc

    async def _price(self, symbol: str) -> dict:
        contract = await self._contract(symbol)
        return await self._get("/market/data/prices", {"symbol": contract["symbol"]})

    @staticmethod
    def _timestamp(value: int | str | None) -> datetime | None:
        if value is None:
            return None
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, UTC)

    async def get_symbols(self) -> list[str]:
        return sorted(await self._load_contracts())

    async def get_funding_rates(self) -> list[FundingRate]:
        symbols = await self.get_symbols()
        async def fetch(symbol: str):
            async with self._funding_request_limit:
                for attempt in range(3):
                    try:
                        return await self._price(symbol)
                    except aiohttp.ClientResponseError as exc:
                        if exc.status != 429 or attempt == 2:
                            raise
                        await asyncio.sleep(0.25 * (2**attempt))

        results = await asyncio.gather(
            *(fetch(symbol) for symbol in symbols), return_exceptions=True
        )
        now = utc_now()
        rates = []
        failures = []
        for symbol, payload in zip(symbols, results, strict=True):
            if isinstance(payload, Exception):
                failures.append((symbol, payload))
                continue
            funding = payload.get("fundingRateEstimation") or {}
            if funding.get("estimatedFundingRate") is None:
                continue
            rates.append(
                FundingRate(
                    self.name,
                    symbol,
                    Decimal(str(funding["estimatedFundingRate"])),
                    self.FUNDING_INTERVAL_HOURS,
                    self._timestamp(funding.get("nextFundingTimestamp"))
                    or now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
                    self._timestamp(payload.get("timestampMs")) or now,
                )
            )
        if failures:
            log.warning(
                "hibachi.partial_funding_rates",
                successful=len(rates),
                failed=len(failures),
                failed_symbols=[symbol for symbol, _ in failures],
                errors=sorted({type(exc).__name__ for _, exc in failures}),
            )
        if not rates and failures:
            raise RuntimeError("Hibachi funding unavailable for every market") from failures[0][1]
        return rates

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        payload = await self._price(symbol)
        funding = payload.get("fundingRateEstimation") or {}
        now = utc_now()
        return FundingRate(
            self.name,
            symbol.upper(),
            Decimal(str(funding["estimatedFundingRate"])),
            self.FUNDING_INTERVAL_HOURS,
            self._timestamp(funding.get("nextFundingTimestamp")),
            self._timestamp(payload.get("timestampMs")) or now,
        )

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        contract = await self._contract(symbol)
        granularities = contract.get("orderbookGranularities") or [contract["tickSize"]]
        payload = await self._get(
            "/market/data/orderbook",
            {
                "symbol": contract["symbol"],
                "depth": min(max(depth, 1), 100),
                "granularity": granularities[0],
            },
        )

        def levels(side: str, reverse: bool) -> list[OrderbookLevel]:
            parsed = [
                OrderbookLevel(Decimal(str(row["price"])), Decimal(str(row["quantity"])))
                for row in (payload.get(side) or {}).get("levels", [])
                if Decimal(str(row["price"])) > 0 and Decimal(str(row["quantity"])) > 0
            ]
            return sorted(parsed, key=lambda row: row.price, reverse=reverse)[:depth]

        return Orderbook(self.name, symbol.upper(), levels("bid", True), levels("ask", False))

    async def get_mark_price(self, symbol: str) -> Decimal:
        payload = await self._price(symbol)
        return Decimal(str(payload["markPrice"]))

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        await self._contract(symbol)
        return FeeRate(
            self.name,
            symbol.upper(),
            Decimal(str(self._fee_config.get("tradeMakerFeeRate", "0"))),
            Decimal(
                str(
                    self._fee_config.get(
                        "tradeTakerFeeRate", self._settings.hibachi_taker_fee
                    )
                )
            ),
        )

    async def get_balance(self) -> Balance:
        return Balance(self.name, Decimal("0"), Decimal("0"))

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("Hibachi live trading is not enabled")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError("Hibachi live trading is not enabled")

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        raise NotImplementedError("Hibachi live trading is not enabled")

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
