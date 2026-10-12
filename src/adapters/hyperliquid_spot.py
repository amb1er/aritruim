"""Public Hyperliquid USDC spot markets for paper cash-and-carry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from src.adapters.hyperliquid import HyperliquidAdapter
from src.models import FeeRate, FundingRate, Orderbook, OrderbookLevel, OrderRequest, OrderResult


class HyperliquidSpotAdapter(HyperliquidAdapter):
    supports_live = False
    is_spot = True
    funding_model = "spot"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._adapter_name = "hyperliquid_spot"
        self._markets: dict[str, str] = {}

    async def connect(self) -> None:
        await super().connect()
        await self._load_markets()

    async def _load_markets(self) -> None:
        meta = await self._post({"type": "spotMeta"})
        tokens = {token["index"]: token["name"] for token in meta["tokens"]}
        markets: dict[str, str] = {}
        ambiguous: set[str] = set()
        for item in meta["universe"]:
            base_id, quote_id = item["tokens"]
            if tokens.get(quote_id) != "USDC":
                continue
            symbol = tokens.get(base_id, "").upper()
            # Ambiguous token names cannot safely be matched to perp underlyings.
            if not symbol or symbol in ambiguous:
                continue
            if symbol in markets:
                markets.pop(symbol, None)
                ambiguous.add(symbol)
                continue
            markets[symbol] = item["name"]
        self._markets = markets

    def _coin(self, symbol: str) -> str:
        try:
            return self._markets[symbol.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown Hyperliquid USDC spot market: {symbol}") from exc

    async def get_symbols(self) -> list[str]:
        return sorted(self._markets)

    async def get_funding_rates(self) -> list[FundingRate]:
        now = datetime.now(UTC)
        return [FundingRate(self.name, symbol, Decimal("0"), 1, None, now)
                for symbol in self._markets]

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        self._coin(symbol)
        return FundingRate(self.name, symbol.upper(), Decimal("0"), 1, None)

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        data = await self._post({"type": "l2Book", "coin": self._coin(symbol)})
        raw = data.get("levels") or [[], []]
        def levels(rows):
            return [OrderbookLevel(Decimal(row["px"]), Decimal(row["sz"]))
                    for row in rows[:depth]]
        return Orderbook(self.name, symbol.upper(), levels(raw[0]), levels(raw[1]))

    async def get_mark_price(self, symbol: str) -> Decimal:
        book = await self.get_orderbook(symbol, depth=1)
        if not book.bids or not book.asks:
            raise ValueError(f"Empty spot orderbook for {symbol}")
        return (book.bids[0].price + book.asks[0].price) / 2

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        self._coin(symbol)
        return FeeRate(self.name, symbol.upper(), self._settings.hyperliquid_spot_maker_fee,
                       self._settings.hyperliquid_spot_taker_fee)

    async def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("Hyperliquid spot live trading is disabled")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        raise NotImplementedError("Hyperliquid spot live trading is disabled")

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        raise NotImplementedError("Hyperliquid spot live trading is disabled")

    async def subscribe_funding(self, callback) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
