"""Polymarket Perps market data and optional authenticated trading."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

import aiohttp
import structlog

from src.adapters.base import ExchangeAdapter
from src.models import (
    Balance,
    BotMode,
    FeeRate,
    FundingRate,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderResult,
    OrderSide,
    Position,
    utc_now,
)


class PolymarketPerpsAdapter(ExchangeAdapter):
    """Polymarket Perps market data and official-SDK live execution."""

    funding_model = "period_history"

    def __init__(
        self,
        settings,
        instance_index: int | None = None,
        transport_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._instance_index = instance_index
        self._adapter_name = (
            "polymarket_perps"
            if instance_index is None
            else f"polymarket_perps_account_{instance_index}"
        )
        self._transport_url = transport_url
        self._address = ""
        self._credential = ""
        if instance_index is not None:
            accounts = settings.resolved_polymarket_perps_accounts()
            if accounts and instance_index >= len(accounts):
                raise IndexError(
                    f"Polymarket Perps account index {instance_index} is not configured"
                )
            if accounts:
                account = accounts[instance_index]
                self._address = account.address
                self._credential = account.credential
                self._transport_url = transport_url or account.transport_url
        self._log = structlog.get_logger(__name__).bind(instance_index=instance_index)
        self._base_url = settings.polymarket_perps_rest_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._instruments: dict[str, dict] = {}
        self._fees: dict[str, dict] = {}
        self._market_lock = asyncio.Lock()
        self._secure_client: Any | None = None
        self._perps_session: Any | None = None

    @property
    def supports_live(self) -> bool:
        return self._perps_session is not None

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
            await self._load_metadata(force=True)
            if (
                self._settings.bot_mode == BotMode.LIVE
                or self._settings.farming_mode == BotMode.LIVE
            ) and self._credential:
                await self._connect_live()
        except Exception as exc:
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._log.error(
                "polymarket_perps.connect_failed",
                transport_configured=bool(self._transport_url),
                error=str(exc),
            )
            raise

    async def disconnect(self) -> None:
        if self._perps_session is not None:
            await self._perps_session.close()
            self._perps_session = None
        if self._secure_client is not None:
            await self._secure_client.close()
            self._secure_client = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _connect_live(self) -> None:
        if self._transport_url:
            raise RuntimeError(
                "Polymarket official SDK does not expose per-client transport proxy support"
            )
        try:
            from polymarket import AsyncSecureClient
        except ImportError as exc:
            raise RuntimeError(
                "Install polymarket-client>=0.13,<0.14 for Polymarket Perps live trading"
            ) from exc
        self._secure_client = await AsyncSecureClient.create(
            private_key=self._credential,
            wallet=self._address or None,
        )
        try:
            self._perps_session = await self._secure_client.open_perps_session(
                label=f"funding-arb-{self._instance_index or 0}"
            )
        except BaseException:
            await self._secure_client.close()
            self._secure_client = None
            raise
        self._log.info("polymarket_perps.live_connected")

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
        if self._perps_session is None:
            return Balance(self.name, Decimal("0"), Decimal("0"))
        portfolio = await self._perps_session.fetch_portfolio()
        return Balance(
            self.name,
            Decimal(portfolio.margin.total_account_value),
            Decimal(portfolio.withdrawable),
            portfolio.timestamp,
        )

    async def get_positions(self) -> list[Position]:
        if self._perps_session is None:
            return []
        portfolio = await self._perps_session.fetch_portfolio()
        by_id = {int(row["instrument_id"]): symbol for symbol, row in self._instruments.items()}
        return [
            Position(
                exchange=self.name,
                symbol=by_id.get(int(item.instrument_id), item.symbol).upper(),
                side=OrderSide.LONG if item.size > 0 else OrderSide.SHORT,
                size=abs(Decimal(item.size)),
                entry_price=Decimal(item.entry_price),
                unrealized_pnl=Decimal(item.unrealized_pnl),
                timestamp=portfolio.timestamp,
            )
            for item in portfolio.positions
            if item.size != 0
        ]

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if self._perps_session is None:
            return self._order_error(order, "Polymarket Perps live session is not connected")
        try:
            instrument = await self._instrument(order.symbol)
            mark = await self.get_mark_price(order.symbol)
            quantity = order.quantity or order.notional_usd / mark
            step = Decimal(1).scaleb(-int(instrument["quantity_decimals"]))
            quantity = quantity.quantize(step, rounding=ROUND_DOWN)
            if quantity <= 0:
                return self._order_error(order, "Order quantity rounds to zero")
            placement = await self._perps_session.place_order(
                instrument_id=int(instrument["instrument_id"]),
                side="BUY" if order.side == OrderSide.LONG else "SELL",
                quantity=quantity,
                time_in_force="ioc",
                reduce_only=order.is_reduce_only,
                client_order_id=order.client_id or None,
            )
            placed = placement.order
            for attempt in range(3):
                found = await self._perps_session.fetch_orders(order_id=int(placed.id))
                if found:
                    placed = found[0]
                if placed.status not in {"accepted", "open", "partial"}:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.25)
            filled = Decimal(placed.filled_quantity)
            if filled > 0:
                for attempt in range(3):
                    page = await self._perps_session.list_fills(
                        start=utc_now() - timedelta(minutes=5)
                    ).first_page()
                    fills = [item for item in page.items if int(item.order_id) == int(placed.id)]
                    confirmed = sum((Decimal(item.quantity) for item in fills), Decimal("0"))
                    if confirmed >= filled:
                        break
                    if attempt < 2:
                        await asyncio.sleep(0.25)
                if confirmed != filled or not fills:
                    return self._order_error(
                        order, "Perps fill details unavailable", uncertain=True
                    )
                avg_price = sum(
                    (Decimal(item.price) * Decimal(item.quantity) for item in fills),
                    Decimal("0"),
                ) / filled
                fee_paid = sum((Decimal(item.total_fee) for item in fills), Decimal("0"))
            else:
                avg_price = Decimal("0")
                fee_paid = Decimal("0")
            terminal = placed.status not in {"accepted", "open", "partial"}
            if not terminal:
                return self._order_error(order, "Perps IOC status is not final", uncertain=True)
            success = filled > 0
            return OrderResult(
                exchange=self.name,
                symbol=order.symbol,
                order_id=str(placed.id),
                side=order.side,
                filled_size=filled,
                avg_price=avg_price,
                fee_paid=fee_paid,
                success=success,
                error="" if success else f"Polymarket order status: {placed.status}",
            )
        except Exception as exc:
            self._log.error("polymarket_perps.order_failed", symbol=order.symbol, error=str(exc))
            return self._order_error(order, str(exc), uncertain=True)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if self._perps_session is None:
            return False
        try:
            result = await self._perps_session.cancel_order(order_id=int(order_id))
            return result.status == "ok"
        except Exception as exc:
            self._log.error(
                "polymarket_perps.cancel_failed",
                symbol=symbol,
                order_id=order_id,
                error=str(exc),
            )
            return False

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        requested_side = OrderSide(side)
        position = next(
            (
                item
                for item in await self.get_positions()
                if item.symbol == symbol.upper() and item.side == requested_side
            ),
            None,
        )
        close_side = OrderSide.SHORT if requested_side == OrderSide.LONG else OrderSide.LONG
        request = OrderRequest(
            exchange=self.name,
            symbol=symbol,
            side=close_side,
            notional_usd=Decimal("0"),
            leverage=Decimal("1"),
            is_reduce_only=True,
            quantity=position.size if position else Decimal("0"),
        )
        if position is None:
            return self._order_error(request, "No matching Polymarket Perps position")
        return await self.place_order(request)

    def _order_error(
        self, order: OrderRequest, error: str, *, uncertain: bool = False
    ) -> OrderResult:
        return OrderResult(
            exchange=self.name,
            symbol=order.symbol,
            order_id="",
            side=order.side,
            filled_size=Decimal("0"),
            avg_price=Decimal("0"),
            fee_paid=Decimal("0"),
            success=False,
            error=error,
            uncertain=uncertain,
        )

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            for rate in await self.get_funding_rates():
                result = callback(rate)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(self._settings.scan_interval_sec)
