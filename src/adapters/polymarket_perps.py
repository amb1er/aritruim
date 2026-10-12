"""Public market-data adapter for Polymarket Perps."""

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


class PolymarketPerpsAdapter(ExchangeAdapter):
    """Polymarket Perps public data and paper execution; live writes are disabled."""

    supports_live = False
    funding_model = "period_history"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._base_url = settings.polymarket_perps_rest_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._instruments: dict[str, dict] = {}
        self._fees: dict[str, dict] = {}
        self._market_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "polymarket_perps"

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
            headers={"User-Agent": "funding-arb-bot/0.1"},
        )
        await self._load_metadata(force=True)

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict | None = None):
        if self._session is None:
            raise RuntimeError("Polymarket Perps adapter is not connected")
        async with self._session.get(f"{self._base_url}{path}", params=params) as response:
            response.raise_for_status()
            return await response.json()

    async def _load_metadata(self, force: bool = False) -> None:
        if self._instruments and not force:
            return
        async with self._market_lock:
            if self._instruments and not force:
                return
            instruments, fees = await asyncio.gather(
                self._get("/v1/info/instruments"), self._get("/v1/info/fees")
            )
            now_ms = int(utc_now().timestamp() * 1000)
            self._instruments = {
                item["base_asset"].upper(): item
                for item in instruments
                if item.get("instrument_type") == "perpetual"
                and int(item.get("ui_live_time") or 0) <= now_ms
            }
            self._fees = {item["category"]: item for item in fees.get("fee_schedule", [])}

    async def _instrument(self, symbol: str) -> dict:
        await self._load_metadata()
        try:
            return self._instruments[symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown Polymarket Perps market: {symbol}") from exc

    @staticmethod
    def _timestamp(value: int | None) -> datetime | None:
        return datetime.fromtimestamp(value / 1000, UTC) if value is not None else None

    @staticmethod
    def _interval_hours(value: str) -> int:
        if not value.endswith("h"):
            raise ValueError(f"Unsupported Polymarket funding interval: {value}")
        hours = int(value[:-1])
        if hours <= 0:
            raise ValueError(f"Invalid Polymarket funding interval: {value}")
        return hours

    async def get_symbols(self) -> list[str]:
        await self._load_metadata()
        return sorted(self._instruments)

    async def get_funding_rates(self) -> list[FundingRate]:
        await self._load_metadata()
        tickers = await self._get("/v1/info/tickers")
        by_id = {item["instrument_id"]: item for item in self._instruments.values()}
        rates = []
        for ticker in tickers:
            instrument = by_id.get(ticker.get("instrument_id"))
            if instrument is None or ticker.get("funding_rate") is None:
                continue
            rates.append(self._rate(instrument, ticker))
        return rates

    def _rate(self, instrument: dict, ticker: dict) -> FundingRate:
        return FundingRate(
            self.name,
            instrument["base_asset"].upper(),
            Decimal(str(ticker["funding_rate"])),
            self._interval_hours(instrument["funding_interval"]),
            self._timestamp(ticker.get("next_funding")),
            self._timestamp(ticker.get("timestamp")) or utc_now(),
        )

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        instrument = await self._instrument(symbol)
        return self._rate(instrument, await self._ticker(instrument))

    async def _ticker(self, instrument: dict) -> dict:
        tickers = await self._get(
            "/v1/info/tickers", {"instrument_id": instrument["instrument_id"]}
        )
        # The public endpoint can return every market despite the query filter.
        for ticker in tickers:
            if str(ticker.get("instrument_id")) == str(instrument["instrument_id"]):
                return ticker
        raise ValueError(f"No Polymarket Perps ticker for {instrument['base_asset']}")

    async def get_funding_history(self, symbol: str, lookback_hours: int = 24) -> list[FundingRate]:
        instrument = await self._instrument(symbol)
        end = utc_now()
        payload = await self._get(
            "/v1/info/funding",
            {
                "instrument_id": instrument["instrument_id"],
                "start_timestamp": int((end - timedelta(hours=lookback_hours)).timestamp() * 1000),
                "end_timestamp": int(end.timestamp() * 1000),
            },
        )
        return [
            FundingRate(
                self.name,
                symbol.upper(),
                Decimal(str(item["funding_rate"])),
                self._interval_hours(instrument["funding_interval"]),
                None,
                self._timestamp(item["timestamp"]) or end,
            )
            for item in payload.get("data", [])
        ]

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        instrument = await self._instrument(symbol)
        if depth <= 10:
            requested_depth = 10
        elif depth <= 100:
            requested_depth = 100
        elif depth <= 500:
            requested_depth = 500
        else:
            requested_depth = 1000
        payload = await self._get(
            "/v1/info/book",
            {"instrument_id": instrument["instrument_id"], "depth": requested_depth},
        )
        if str(payload.get("instrument_id")) != str(instrument["instrument_id"]):
            raise ValueError("Polymarket orderbook instrument mismatch")

        def levels(rows: list, reverse: bool) -> list[OrderbookLevel]:
            parsed = [
                OrderbookLevel(Decimal(str(row[0])), Decimal(str(row[1])))
                for row in rows
                if Decimal(str(row[0])) > 0 and Decimal(str(row[1])) > 0
            ]
            return sorted(parsed, key=lambda row: row.price, reverse=reverse)[:depth]

        return Orderbook(
            self.name,
            symbol.upper(),
            levels(payload.get("bids", []), True),
            levels(payload.get("asks", []), False),
            self._timestamp(payload.get("timestamp")) or utc_now(),
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        instrument = await self._instrument(symbol)
        ticker = await self._ticker(instrument)
        return Decimal(str(ticker["mark_price"]))

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        instrument = await self._instrument(symbol)
        schedule = self._fees.get(instrument["category"], {})
        return FeeRate(
            self.name,
            symbol.upper(),
            Decimal(str(schedule.get("maker_fee_rate", self._settings.polymarket_perps_maker_fee))),
            Decimal(str(schedule.get("taker_fee_rate", self._settings.polymarket_perps_taker_fee))),
        )

    async def get_balance(self) -> Balance:
        return Balance(self.name, Decimal("0"), Decimal("0"))

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("Polymarket Perps live trading is not enabled")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError("Polymarket Perps live trading is not enabled")

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        raise NotImplementedError("Polymarket Perps live trading is not enabled")

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
