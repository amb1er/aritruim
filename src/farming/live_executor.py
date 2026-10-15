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

    async def preflight(self) -> None:
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
        required = (
            notional * self.settings.farming_live_balance_buffer
        )
        for name, adapter in adapters.items():
            balance, positions = await asyncio.gather(
                adapter.get_balance(), adapter.get_positions()
            )
            if positions:
                raise RuntimeError(
                    f"Live preflight found existing positions on {name}; reconcile manually"
                )
            if balance.available_usd < required:
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

    def route_supported(self, route: FarmingRoute) -> bool:
        adapters = self.live_adapters
        return route.long_venue in adapters and route.short_venue in adapters

    async def open(self, route: FarmingRoute) -> LiveFill | None:
        if self.halted or not self.route_supported(route):
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
            if long_result.uncertain:
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
            if short_result.uncertain:
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
            if not self._certain_fill(rollback):
                self._halt("second-leg failure could not be safely rolled back", position_id)
            return None

        matched = min(long_result.filled_size, short_result.filled_size)
        if long_result.filled_size != short_result.filled_size:
            adjustments = []
            if long_result.filled_size > matched:
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
            if any(not self._certain_fill(result) for result in results):
                self._halt("partial-fill hedge adjustment failed", position_id)
                return None
        if matched <= 0:
            self._halt("two-leg execution produced zero matched quantity", position_id)
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
        long_result, short_result = await asyncio.gather(
            self._reduce(
                position.id,
                "close_long",
                position.long_venue,
                position.symbol,
                OrderSide.LONG,
                position.quantity,
            ),
            self._reduce(
                position.id,
                "close_short",
                position.short_venue,
                position.symbol,
                OrderSide.SHORT,
                position.quantity,
            ),
        )
        results = [long_result, short_result]
        for index, result in enumerate(results):
            if not self._certain_fill(result):
                venue = position.long_venue if index == 0 else position.short_venue
                side = OrderSide.LONG if index == 0 else OrderSide.SHORT
                results[index] = await self._reduce(
                    position.id,
                    f"close_retry_{side.value}",
                    venue,
                    position.symbol,
                    side,
                    position.quantity,
                )
        if any(not self._certain_fill(result) for result in results):
            self._halt("live close left an unreconciled leg", position.id)
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
            "INSERT INTO farming_live_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )
        await self.db.commit()

    @staticmethod
    def _certain_fill(result: OrderResult) -> bool:
        return result.success and not result.uncertain and result.filled_size > 0

    def _halt(self, reason: str, position_id: str) -> None:
        self.halted = True
        self._log.critical(
            "farming.live_halted",
            position_id=position_id,
            reason=reason,
        )
