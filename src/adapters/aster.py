"""Aster V3 public USDT perpetual data; execution is handled by the paper engine.

Docs: https://asterdex.github.io/aster-api-website/futures-v3/market-data/
Fees: https://docs.asterdex.com/trading/perpetuals/fees-and-specs/fees
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp

from src.adapters.base import ExchangeAdapter
from src.models import Balance, FeeRate, FundingRate, Orderbook, OrderbookLevel, utc_now

# Published Group B list, reviewed 2026-09-27. Higher fee wins where categories overlap.
GROUP_B = frozenset(
    [
        "1000NEX",
        "AEON",
        "ASTEROID",
        "AVL",
        "BAY",
        "BASECAT",
        "BLEND",
        "B3",
        "CARDS",
        "CATE",
        "DELTA",
        "FONE",
        "MEME",
        "MARSCOIN",
        "NES",
        "O",
        "OKB",
        "PENGUIN",
        "PUNDIAI",
        "RTX",
        "SKHYNIX",
    ]
)


class AsterAdapter(ExchangeAdapter):
    supports_live = False
    funding_model = "period_history"

    def __init__(self, settings):
        self._settings = settings
        self._base_url = settings.aster_rest_url.rstrip("/")
        self._session = None
        self._markets = {}
        self._intervals = {}
        self._metadata_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def name(self):
        return "aster"

    async def connect(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
        )
        await self._load_markets()

    async def disconnect(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, endpoint, params=None):
        if self._session is None:
            raise RuntimeError("Aster adapter is not connected")
        async with self._session.get(
            self._base_url + "/fapi/v3/" + endpoint, params=params
        ) as response:
            response.raise_for_status()
            data = await response.json()
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
            raise ValueError(f"Aster API error: {data['code']}")
        return data

    async def _load_markets(self):
        async with self._lock:
            if self._markets and time.monotonic() - self._metadata_at < 300:
                return self._markets
            info, funding = await asyncio.gather(
                self._get("exchangeInfo"), self._get("fundingInfo")
            )
            markets = {}
            for row in info["symbols"]:
                if (
                    row.get("status") != "TRADING"
                    or row.get("contractType") != "PERPETUAL"
                    or row.get("quoteAsset") != "USDT"
                    or row.get("marginAsset") != "USDT"
                ):
                    continue
                symbol = row["baseAsset"].upper()
                if symbol in markets:
                    raise ValueError(f"Ambiguous Aster USDT market: {symbol}")
                markets[symbol] = row
            intervals = {}
            for row in funding:
                value = Decimal(str(row.get("fundingIntervalHours", "0")))
                if value.is_finite() and value > 0 and value == int(value):
                    intervals[row["symbol"]] = int(value)
            self._markets, self._intervals = markets, intervals
            self._metadata_at = time.monotonic()
            return markets

    async def _market(self, symbol):
        markets = await self._load_markets()
        if symbol.upper() not in markets:
            raise ValueError(f"Unknown Aster USDT perpetual: {symbol}")
        return markets[symbol.upper()]

    async def get_symbols(self):
        return sorted(await self._load_markets())

    def _rate(self, symbol, row):
        interval = self._intervals.get(row["symbol"])
        if interval is None:
            raise ValueError(f"Unknown Aster funding interval: {symbol}")
        return FundingRate(
            self.name,
            symbol,
            Decimal(str(row["lastFundingRate"])),
            interval,
            datetime.fromtimestamp(int(row["nextFundingTime"]) / 1000, UTC),
            datetime.fromtimestamp(int(row["time"]) / 1000, UTC),
        )

    async def get_funding_rates(self):
        markets = await self._load_markets()
        by_wire = {row["symbol"]: symbol for symbol, row in markets.items()}
        rows = await self._get("premiumIndex")
        return [
            self._rate(by_wire[row["symbol"]], row)
            for row in rows
            if row.get("symbol") in by_wire and row["symbol"] in self._intervals
        ]

    async def _premium(self, symbol):
        market = await self._market(symbol)
        result = await self._get("premiumIndex", {"symbol": market["symbol"]})
        rows = result if isinstance(result, list) else [result]
        for row in rows:
            if row.get("symbol") == market["symbol"]:
                return row
        raise ValueError("Aster premiumIndex symbol mismatch")

    async def get_funding_rate(self, symbol):
        return self._rate(symbol.upper(), await self._premium(symbol))

    async def get_mark_price(self, symbol):
        return Decimal(str((await self._premium(symbol))["markPrice"]))

    async def get_funding_history(self, symbol, lookback_hours=24):
        market = await self._market(symbol)
        interval = self._intervals.get(market["symbol"])
        if interval is None:
            raise ValueError("Unknown Aster funding interval")
        end = utc_now()
        start_ms = int((end - timedelta(hours=max(1, lookback_hours))).timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        result = {}
        while start_ms <= end_ms:
            rows = await self._get(
                "fundingRate",
                {
                    "symbol": market["symbol"],
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            for row in rows:
                if row.get("symbol") != market["symbol"]:
                    raise ValueError("Aster funding history symbol mismatch")
                ts = int(row["fundingTime"])
                if start_ms <= ts <= end_ms:
                    result[ts] = FundingRate(
                        self.name,
                        symbol.upper(),
                        Decimal(str(row["fundingRate"])),
                        interval,
                        None,
                        datetime.fromtimestamp(ts / 1000, UTC),
                    )
            if len(rows) < 1000:
                break
            next_ms = max(int(row["fundingTime"]) for row in rows) + 1
            if next_ms <= start_ms:
                raise ValueError("Aster funding pagination did not advance")
            start_ms = next_ms
        return [result[ts] for ts in sorted(result)]

    async def get_orderbook(self, symbol, depth=20):
        market = await self._market(symbol)
        limit = next((n for n in (5, 10, 20, 50, 100, 500, 1000) if n >= depth), 1000)
        data = await self._get("depth", {"symbol": market["symbol"], "limit": limit})
        if data.get("symbol", market["symbol"]) != market["symbol"]:
            raise ValueError("Aster depth symbol mismatch")

        def levels(key, reverse):
            values = [OrderbookLevel(Decimal(str(p)), Decimal(str(q))) for p, q in data[key]]
            if any(
                not v.price.is_finite() or not v.size.is_finite() or v.price <= 0 or v.size < 0
                for v in values
            ):
                raise ValueError("Invalid Aster book level")
            return sorted(
                (v for v in values if v.size > 0), key=lambda v: v.price, reverse=reverse
            )[:depth]

        timestamp = (
            datetime.fromtimestamp(int(data["E"]) / 1000, UTC) if data.get("E") else utc_now()
        )
        return Orderbook(
            self.name, symbol.upper(), levels("bids", True), levels("asks", False), timestamp
        )

    async def get_fee_rate(self, symbol):
        market = await self._market(symbol)
        fee = self._settings.aster_taker_fee
        if symbol.upper() in GROUP_B:
            fee = self._settings.aster_group_b_taker_fee
        elif market.get("symbolType") == 1:
            fee = self._settings.aster_rwa_taker_fee
        return FeeRate(self.name, symbol.upper(), self._settings.aster_maker_fee, fee)

    async def get_balance(self):
        return Balance(self.name, Decimal("0"), Decimal("0"))

    async def get_positions(self):
        return []

    async def place_order(self, order):
        raise NotImplementedError("Aster live trading is not enabled")

    async def cancel_order(self, symbol, order_id):
        raise NotImplementedError("Aster live trading is not enabled")

    async def close_position(self, symbol, side):
        raise NotImplementedError("Aster live trading is not enabled")

    async def subscribe_funding(self, callback):
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
