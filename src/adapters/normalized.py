"""Offline, venue-specific PerpDexList aliases and base-unit conversions.

The execution engine sees canonical base units. Legacy native symbols remain
readable in their original units so existing paper positions can be closed.
"""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from src.models import OrderbookLevel

CATALOG = Path(__file__).resolve().parents[1] / "instrument_mappings.json"


def load_catalog(path=CATALOG):
    data = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for row in data["markets"]:
        factor = Decimal(row["base_units"])
        if not factor.is_finite() or factor <= 0:
            raise ValueError("Invalid instrument multiplier")
        key = (row["exchange"], row["native_symbol"])
        value = (row["asset"], factor)
        if key in result and result[key] != value:
            raise ValueError(f"Conflicting instrument mapping: {key}")
        result[key] = value
    return result


class NormalizedAdapter:
    """Wrap an already connected adapter; canonical quantities are paper-only."""

    supports_live = False

    def __init__(self, adapter, symbols, catalog):
        self._adapter = adapter
        self._native = set(symbols)
        self._mapping = {}
        self._canonical = {}
        self._ambiguous = set()
        for native in symbols:
            asset, factor = catalog.get((adapter.name, native), (native, Decimal("1")))
            self._mapping[native] = (asset, factor)
            if asset in self._canonical:
                self._ambiguous.add(asset)
            else:
                self._canonical[asset] = (native, factor)
        for asset in self._ambiguous:
            self._canonical.pop(asset, None)

    def __getattr__(self, name):
        return getattr(self._adapter, name)

    def canonical_symbol(self, symbol):
        return self._mapping.get(symbol, (symbol, Decimal("1")))[0]

    def native_aliases(self, asset):
        return {native for native, (canonical, _) in self._mapping.items() if canonical == asset}

    def _resolve(self, symbol):
        if symbol in self._ambiguous:
            raise ValueError(f"Ambiguous canonical market: {self.name}/{symbol}")
        if symbol in self._canonical:
            return self._canonical[symbol]
        if symbol in self._native:
            # Old database positions used native quantities/prices.
            return symbol, Decimal("1")
        raise ValueError(f"Unknown market: {self.name}/{symbol}")

    async def get_symbols(self):
        return sorted(self._canonical)

    async def get_funding_rates(self):
        result = []
        for rate in await self._adapter.get_funding_rates():
            mapped = self._mapping.get(rate.symbol)
            if mapped and mapped[0] in self._canonical:
                result.append(replace(rate, symbol=mapped[0]))
        return result

    async def get_funding_rate(self, symbol):
        native, _ = self._resolve(symbol)
        return replace(await self._adapter.get_funding_rate(native), symbol=symbol)

    async def get_funding_history(self, symbol, lookback_hours=24):
        native, _ = self._resolve(symbol)
        return [
            replace(rate, symbol=symbol)
            for rate in await self._adapter.get_funding_history(native, lookback_hours)
        ]

    async def get_orderbook(self, symbol, depth=20):
        native, factor = self._resolve(symbol)
        book = await self._adapter.get_orderbook(native, depth)

        def convert(levels):
            return [OrderbookLevel(level.price / factor, level.size * factor) for level in levels]

        return replace(book, symbol=symbol, bids=convert(book.bids), asks=convert(book.asks))

    async def get_mark_price(self, symbol):
        native, factor = self._resolve(symbol)
        return await self._adapter.get_mark_price(native) / factor

    async def get_funding_price(self, symbol):
        native, factor = self._resolve(symbol)
        return await self._adapter.get_funding_price(native) / factor

    async def get_funding_indices(self, symbol):
        native, factor = self._resolve(symbol)
        return tuple(value / factor for value in await self._adapter.get_funding_indices(native))

    async def get_fee_rate(self, symbol):
        native, _ = self._resolve(symbol)
        return replace(await self._adapter.get_fee_rate(native), symbol=symbol)

    async def place_order(self, order):
        raise NotImplementedError("Normalized instruments support paper execution only")

    async def close_position(self, symbol, side):
        raise NotImplementedError("Normalized instruments support paper execution only")
