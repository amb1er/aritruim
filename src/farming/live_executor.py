"""Fail-closed two-leg execution for live farming routes."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from decimal import Decimal

import aiosqlite
import structlog

from src.farming.optimizer import FarmingRoute
from src.models import OrderRequest, OrderResult, OrderSide, utc_now

D = Decimal


@dataclass(frozen=True)
class LiveFill:
    position_id: str
    quantity: Decimal
    long_price: Decimal
    short_price: Decimal
    fees: Decimal


@dataclass(frozen=True)
class LiveClose:
    long_exit: Decimal
    short_exit: Decimal
    fees: Decimal


class LiveFarmingExecutor:
    """Execute two IOC legs and flatten immediately when the hedge is incomplete."""

    def __init__(self, adapters: dict, settings, db: aiosqlite.Connection, instance_index: int):
        self.adapters = adapters
        self.settings = settings
        self.db = db
        self.instance_index = instance_index
        self.halted = False
        self._log = structlog.get_logger(__name__).bind(instance_index=instance_index)

    @property
    def live_adapters(self) -> dict:
        return {
            name: adapter
            for name, adapter in self.adapters.items()
            if bool(getattr(adapter, "supports_live", False))
        }

    async def preflight(self, known_positions: list | None = None) -> None:
        if not self.settings.farming_live_acknowledge_risk:
            raise RuntimeError(
                "Live farming requires FARMING_LIVE_ACKNOWLEDGE_RISK=true"
            )
        if self.settings.farming_leverage != D("1"):
            raise RuntimeError("Initial live farming is restricted to FARMING_LEVERAGE=1")
        if self.settings.farming_max_open_pairs != 1:
            raise RuntimeError("Initial live farming requires FARMING_MAX_OPEN_PAIRS=1")
        notional = self.settings.farming_notional_usd * self.settings.farming_leverage
        if notional > self.settings.farming_live_max_notional_usd:
            raise RuntimeError(
                f"Live notional {notional} exceeds FARMING_LIVE_MAX_NOTIONAL_USD="
                f"{self.settings.farming_live_max_notional_usd}"
            )
        adapters = self.live_adapters
        if len(adapters) < 2:
            raise RuntimeError("Live farming requires at least two live-capable venues")
        known_positions = known_positions or []
        for position in known_positions:
            if position.long_venue not in adapters or position.short_venue not in adapters:
                raise RuntimeError(
                    "Open live position uses an unavailable venue; reconcile manually"
                )
        pending = await self.db.execute(
            "SELECT intent.position_id,intent.phase FROM farming_live_orders intent "
            "WHERE intent.phase LIKE '%:intent' AND NOT EXISTS ("
            "SELECT 1 FROM farming_live_orders result WHERE result.position_id=intent.position_id "
            "AND result.phase=substr(intent.phase,1,length(intent.phase)-7))"
        )
        if await pending.fetchone():
            raise RuntimeError("Unresolved live order intent; reconcile manually")
        uncertain = await self.db.execute(
            "SELECT 1 FROM farming_live_orders WHERE uncertain=1 LIMIT 1"
        )
        if await uncertain.fetchone():
            raise RuntimeError("Uncertain live order result; reconcile manually")
        required = (
            notional * self.settings.farming_live_balance_buffer
        )
        for name, adapter in adapters.items():
            balance, positions = await asyncio.gather(
                adapter.get_balance(), adapter.get_positions()
            )
            expected = [
                (p.symbol, side, p.quantity)
                for p in known_positions
                for venue, side in ((p.long_venue, OrderSide.LONG),
                                    (p.short_venue, OrderSide.SHORT))
                if venue == name
            ]
            actual = [(p.symbol, p.side, p.size) for p in positions]
            if sorted(actual) != sorted(expected):
                raise RuntimeError(
                    f"Live positions on {name} differ from the local journal; reconcile manually"
                )
            if not known_positions and balance.available_usd < required:
                raise RuntimeError(
                    f"Insufficient available balance on {name}: "
                    f"{balance.available_usd} < {required}"
                )
            self._log.info(
                "farming.live_preflight_venue",
                venue=name,
                available_usd=str(balance.available_usd),
                required_usd=str(required),
            )
        markets = await asyncio.gather(*(adapter.get_symbols() for adapter in adapters.values()))
        shared = set.intersection(*(set(symbols) for symbols in markets))
        if not shared:
            raise RuntimeError("Live venues have no common markets")
        for symbol in sorted(shared)[:5]:
            books = await asyncio.gather(
                *(adapter.get_orderbook(symbol, 1) for adapter in adapters.values()),
                return_exceptions=True,
            )
            if all(
                not isinstance(book, BaseException)
                and book.bids and book.asks
                and book.bids[0].price > 0
                and book.bids[0].price <= book.asks[0].price
                for book in books
            ):
                self._log.info("farming.live_preflight_books", symbol=symbol)
                return
        raise RuntimeError("Live venues have no common market with valid two-sided books")

    def route_supported(self, route: FarmingRoute) -> bool:
        adapters = self.live_adapters
        return route.long_venue in adapters and route.short_venue in adapters

    async def open(self, route: FarmingRoute) -> LiveFill | None:
        if self.halted or not self.route_supported(route):
            return None
        for venue in (route.long_venue, route.short_venue):
            positions = await self.adapters[venue].get_positions()
            if any(p.symbol == route.symbol for p in positions):
                self._halt("unexpected existing position before entry", "preflight")
                return None
        position_id = uuid.uuid4().hex
        long_result = await self._place(
            position_id,
            "open_long",
            route.long_venue,
            route.symbol,
            OrderSide.LONG,
            route.quantity,
        )
        if not self._certain_fill(long_result):
            if long_result.uncertain or long_result.filled_size > 0:
                self._halt("uncertain first-leg result", position_id)
            return None

        short_result = await self._place(
            position_id,
            "open_short",
            route.short_venue,
            route.symbol,
            OrderSide.SHORT,
            long_result.filled_size,
        )
        if not self._certain_fill(short_result):
            if short_result.uncertain or short_result.filled_size > 0:
                self._halt("uncertain second-leg result requires reconciliation", position_id)
                return None
            rollback = await self._reduce(
                position_id,
                "rollback_long",
                route.long_venue,
                route.symbol,
                OrderSide.LONG,
                long_result.filled_size,
            )
            if not self._certain_fill(rollback) or rollback.filled_size != long_result.filled_size:
                self._halt("second-leg failure could not be safely rolled back", position_id)
            return None

        matched = min(long_result.filled_size, short_result.filled_size)
        if long_result.filled_size != short_result.filled_size:
            adjustments = []
            adjustment_sizes = []
            if long_result.filled_size > matched:
                adjustment_sizes.append(long_result.filled_size - matched)
                adjustments.append(
                    self._reduce(
                        position_id,
                        "trim_long",
                        route.long_venue,
                        route.symbol,
                        OrderSide.LONG,
                        long_result.filled_size - matched,
                    )
                )
            if short_result.filled_size > matched:
                adjustment_sizes.append(short_result.filled_size - matched)
                adjustments.append(
                    self._reduce(
                        position_id,
                        "trim_short",
                        route.short_venue,
                        route.symbol,
                        OrderSide.SHORT,
                        short_result.filled_size - matched,
                    )
                )
            results = await asyncio.gather(*adjustments)
            if any(not self._certain_fill(result) or result.filled_size != expected
                   for result, expected in zip(results, adjustment_sizes, strict=True)):
                self._halt("partial-fill hedge adjustment failed", position_id)
                return None
        if matched <= 0:
            self._halt("two-leg execution produced zero matched quantity", position_id)
            return None
        for venue, side in ((route.long_venue, OrderSide.LONG),
                            (route.short_venue, OrderSide.SHORT)):
            positions = await self.adapters[venue].get_positions()
            if not any(p.symbol == route.symbol and p.side == side and p.size == matched
                       for p in positions):
                self._halt("venue position does not match hedge after entry", position_id)
                return None
        self._log.warning(
            "farming.live_open",
            position_id=position_id,
            symbol=route.symbol,
            long=route.long_venue,
            short=route.short_venue,
            quantity=str(matched),
        )
        return LiveFill(
            position_id,
            matched,
            long_result.avg_price,
            short_result.avg_price,
            long_result.fee_paid + short_result.fee_paid,
        )

    async def close(self, position, reason: str) -> LiveClose | None:
        if self.halted:
            return None
        results = await asyncio.gather(
            self._close_leg(position, position.long_venue, OrderSide.LONG),
            self._close_leg(position, position.short_venue, OrderSide.SHORT),
        )
        if any(result is None for result in results):
            self._halt("live close left an unreconciled leg", position.id)
            return None
        for venue in (position.long_venue, position.short_venue):
            current = await self.adapters[venue].get_positions()
            if any(p.symbol == position.symbol for p in current):
                self._halt("live close left a position on venue", position.id)
                return None
        exit_fees = sum((result.fee_paid for result in results), D("0"))
        self._log.warning(
            "farming.live_close",
            position_id=position.id,
            symbol=position.symbol,
            reason=reason,
            exit_fees=str(exit_fees),
        )
        return LiveClose(results[0].avg_price, results[1].avg_price, exit_fees)

    async def _close_leg(self, position, venue: str, side: OrderSide) -> OrderResult | None:
        remaining = position.quantity
        filled = D("0")
        value = D("0")
        fees = D("0")
        for attempt in range(3):
            result = await self._reduce(
                position.id, f"close_{side.value}_{attempt}", venue,
                position.symbol, side, remaining,
            )
            if result.uncertain or result.filled_size < 0 or result.filled_size > remaining:
                return None
            if result.filled_size:
                if not self._certain_fill(result):
                    return None
                filled += result.filled_size
                remaining -= result.filled_size
                value += result.filled_size * result.avg_price
                fees += result.fee_paid
                if remaining == 0:
                    return OrderResult(venue, position.symbol, result.order_id, result.side,
                                       filled, value / filled, fees, True)
        return None

    async def _reduce(
        self,
        position_id: str,
        phase: str,
        venue: str,
        symbol: str,
        open_side: OrderSide,
        quantity: Decimal,
    ) -> OrderResult:
        close_side = OrderSide.SHORT if open_side == OrderSide.LONG else OrderSide.LONG
        return await self._place(
            position_id,
            phase,
            venue,
            symbol,
            close_side,
            quantity,
            reduce_only=True,
        )

    async def _place(
        self,
        position_id: str,
        phase: str,
        venue: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        *,
        reduce_only: bool = False,
    ) -> OrderResult:
        request = OrderRequest(
            exchange=venue,
            symbol=symbol,
            side=side,
            notional_usd=D("0"),
            leverage=self.settings.farming_leverage,
            is_reduce_only=reduce_only,
            client_id=uuid.uuid4().hex,
            quantity=quantity,
        )
        await self.db.execute(
            "INSERT INTO farming_live_orders "
            "(timestamp,position_id,phase,venue,symbol,side,requested_quantity,order_id,"
            "filled_quantity,avg_price,fee_paid,success,error,uncertain) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utc_now().isoformat(), position_id, f"{phase}:intent", venue, symbol,
             side.value, str(quantity), request.client_id, "0", "0", "0", 0, "pending", 0),
        )
        await self.db.commit()
        try:
            result = await self.adapters[venue].place_order(request)
        except Exception as exc:
            result = OrderResult(
                exchange=venue,
                symbol=symbol,
                order_id="",
                side=side,
                filled_size=D("0"),
                avg_price=D("0"),
                fee_paid=D("0"),
                success=False,
                error=str(exc),
                uncertain=True,
            )
        await self._journal(position_id, phase, request, result)
        return result

    async def _journal(
        self,
        position_id: str,
        phase: str,
        request: OrderRequest,
        result: OrderResult,
    ) -> None:
        await self.db.execute(
            "INSERT INTO farming_live_orders "
            "(timestamp,position_id,phase,venue,symbol,side,requested_quantity,order_id,"
            "filled_quantity,avg_price,fee_paid,success,error,uncertain) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                utc_now().isoformat(),
                position_id,
                phase,
                request.exchange,
                request.symbol,
                request.side.value,
                str(request.quantity or D("0")),
                result.order_id,
                str(result.filled_size),
                str(result.avg_price),
                str(result.fee_paid),
                int(result.success),
                result.error,
                int(result.uncertain),
            ),
        )
        await self.db.commit()

    @staticmethod
    def _certain_fill(result: OrderResult) -> bool:
        return (
            result.success and not result.uncertain
            and result.filled_size.is_finite() and result.filled_size > 0
            and result.avg_price.is_finite() and result.avg_price > 0
            and result.fee_paid.is_finite() and result.fee_paid >= 0
        )

    def _halt(self, reason: str, position_id: str) -> None:
        self.halted = True
        self._log.critical(
            "farming.live_halted",
            position_id=position_id,
            reason=reason,
        )
