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
    """Wrap an adapter and translate canonical symbols and quantities."""

    def __init__(self, adapter, symbols, catalog):
        self._adapter = adapter
        self._native = set(symbols)
        self._mapping = {}
        self._canonical = {}
        self._ambiguous = set()
        name_parts = adapter.name.rsplit("_account_", 1)
        catalog_exchange = (
            name_parts[0]
            if len(name_parts) == 2 and name_parts[1].isdigit()
            else adapter.name
        )
        for native in symbols:
            asset, factor = catalog.get((catalog_exchange, native), (native, Decimal("1")))
            self._mapping[native] = (asset, factor)
            if asset in self._canonical:
                self._ambiguous.add(asset)
            else:
                self._canonical[asset] = (native, factor)
        for asset in self._ambiguous:
            self._canonical.pop(asset, None)

    def __getattr__(self, name):
        return getattr(self._adapter, name)

    @property
    def supports_live(self):
        return getattr(self._adapter, "supports_live", False)

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

    async def get_positions(self):
        result = []
        for position in await self._adapter.get_positions():
            mapped = self._mapping.get(position.symbol)
            if mapped is None or mapped[0] not in self._canonical:
                raise ValueError(f"Unmapped live position: {self.name}/{position.symbol}")
            asset, factor = mapped
            result.append(replace(position, exchange=self.name, symbol=asset,
                                  size=position.size * factor,
                                  entry_price=position.entry_price / factor))
        return result

    async def place_order(self, order):
        native, factor = self._resolve(order.symbol)
        native_order = replace(
            order,
            exchange=self._adapter.name,
            symbol=native,
            quantity=order.quantity / factor if order.quantity is not None else None,
        )
        result = await self._adapter.place_order(native_order)
        return replace(
            result,
            exchange=self.name,
            symbol=order.symbol,
            filled_size=result.filled_size * factor,
            avg_price=result.avg_price / factor if result.avg_price else result.avg_price,
        )

    async def cancel_order(self, symbol, order_id):
        native, _ = self._resolve(symbol)
        return await self._adapter.cancel_order(native, order_id)

    async def close_position(self, symbol, side):
        native, factor = self._resolve(symbol)
        result = await self._adapter.close_position(native, side)
        return replace(
            result,
            exchange=self.name,
            symbol=symbol,
            filled_size=result.filled_size * factor,
            avg_price=result.avg_price / factor if result.avg_price else result.avg_price,
        )
