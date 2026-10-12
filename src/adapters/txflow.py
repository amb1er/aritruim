"""TxFlow public market-data adapter used by the paper farming strategy."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp

from src.adapters.base import ExchangeAdapter
from src.models import (
    Balance, FeeRate, FundingRate, Orderbook, OrderbookLevel, OrderRequest,
    OrderResult, Position, utc_now,
)


class TxFlowAdapter(ExchangeAdapter):
    supports_live = False
    funding_model = "scheduled_estimate"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._rest = settings.txflow_rest_url.rstrip("/")
        self._ws = settings.txflow_ws_url
        self._session: aiohttp.ClientSession | None = None
        self._markets: dict[str, dict] = {}
        self._by_index: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "txflow"

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
            headers={
                "User-Agent": "funding-arb-bot/0.1",
                "Origin": "https://app.txflow.com",
                "Referer": "https://app.txflow.com/",
                "x-api-version": self._settings.txflow_api_version,
            },
        )
        await self._load_markets()

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _load_markets(self) -> None:
        if self._session is None:
            raise RuntimeError("TxFlow adapter is not connected")
        async with self._session.post(
            f"{self._rest}/info", json={"type": "perpMeta", "dex": ""}
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if not payload.get("success"):
            raise RuntimeError(f"TxFlow metadata failed: {payload.get('message')}")
        universe = payload.get("data", {}).get("universe", [])
        self._markets = {
            row["baseCurrency"].upper(): row
            for row in universe
            if not row.get("delisted") and not row.get("haltTrading")
        }
        self._by_index = {str(row["index"]): symbol for symbol, row in self._markets.items()}

    async def _snapshot(self, subscription: dict) -> dict:
        if self._session is None:
            raise RuntimeError("TxFlow adapter is not connected")
        async with self._session.ws_connect(self._ws, origin="https://app.txflow.com") as ws:
            await ws.send_json({
                "method": "subscribe",
                "type": subscription["type"],
                "subscription": subscription,
            })
            async with asyncio.timeout(12):
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        payload = message.json()
                        if payload.get("channel") == subscription["type"]:
                            return payload.get("data") or {}
                    elif message.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED}:
                        break
        raise RuntimeError(f"No TxFlow {subscription['type']} snapshot")

    async def get_symbols(self) -> list[str]:
        return sorted(self._markets)

    async def get_funding_rates(self) -> list[FundingRate]:
        data = await self._snapshot({"type": "allAssetCtxs"})
        rates = []
        for row in data.get("ctxs", []):
            symbol = self._by_index.get(str(row.get("coin")))
            ctx = row.get("ctx") or {}
            if symbol is None or ctx.get("funding") is None:
                continue
            next_ms, prev_ms = ctx.get("nextFundingTime"), ctx.get("prevFundingTime")
            interval = max(1, round((int(next_ms) - int(prev_ms)) / 3_600_000))
            stamp_ms = ctx.get("blockTime")
            rates.append(FundingRate(
                self.name, symbol, Decimal(str(ctx["funding"])), interval,
                datetime.fromtimestamp(int(next_ms) / 1000, UTC) if next_ms else None,
                datetime.fromtimestamp(int(stamp_ms) / 1000, UTC) if stamp_ms else utc_now(),
            ))
        return rates

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        for rate in await self.get_funding_rates():
            if rate.symbol == symbol.upper():
                return rate
        raise ValueError(f"No TxFlow funding for {symbol}")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        market = self._markets[symbol.upper()]
        if self._session is None:
            raise RuntimeError("TxFlow adapter is not connected")
        # The REST endpoint expects the market index encoded as a string. Using
        # one request per book avoids opening many WebSockets and HTTP 429s.
        async with self._session.post(
            f"{self._rest}/info",
            json={"type": "l2Book", "coin": str(market["index"])},
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if not payload.get("success"):
            raise RuntimeError(f"TxFlow orderbook failed: {payload.get('message')}")
        data = payload.get("data") or {}
        sides = data.get("levels") or [[], []]
        def parse(rows, reverse):
            result = [OrderbookLevel(Decimal(str(x["px"])), Decimal(str(x["sz"]))) for x in rows]
            return sorted(result, key=lambda x: x.price, reverse=reverse)[:depth]
        stamp = data.get("time")
        return Orderbook(self.name, symbol.upper(), parse(sides[0], True), parse(sides[1], False),
                         datetime.fromtimestamp(int(stamp) / 1000, UTC) if stamp else utc_now())

    async def get_mark_price(self, symbol: str) -> Decimal:
        book = await self.get_orderbook(symbol, 1)
        if not book.bids or not book.asks:
            raise ValueError(f"Empty TxFlow book for {symbol}")
        return (book.bids[0].price + book.asks[0].price) / 2

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        return FeeRate(self.name, symbol.upper(), self._settings.txflow_maker_fee,
                       self._settings.txflow_taker_fee)

    async def get_balance(self) -> Balance:
        return Balance(self.name, Decimal("0"), Decimal("0"))

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("TxFlow live trading is not enabled")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError("TxFlow live trading is not enabled")

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        raise NotImplementedError("TxFlow live trading is not enabled")

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.farming_scan_interval_sec)
