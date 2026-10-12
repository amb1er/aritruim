"""Pair execution with persisted fills, remaining quantities and recovery state."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal

import structlog

from src.adapters.base import ExchangeAdapter
from src.adapters.normalized import NormalizedAdapter
from src.config import Settings
from src.core.funding_accounting import observe_leg, paid_hours_within
from src.core.risk import RiskManager
from src.db import Database
from src.models import (
    ArbitragePosition,
    Balance,
    BotMode,
    FundingSpread,
    OrderRequest,
    OrderResult,
    OrderSide,
    PositionStatus,
    as_utc,
    utc_now,
)
from src.notifier import Notifier

log = structlog.get_logger(__name__)
ZERO = Decimal("0")


class Executor:
    def __init__(
        self,
        adapters: dict[str, ExchangeAdapter],
        risk: RiskManager,
        db: Database,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self._adapters = adapters
        self._risk = risk
        self._db = db
        self._notifier = notifier
        self._cfg = settings
        self._open_positions: dict[str, ArbitragePosition] = {}
        self._opening_symbols: set[str] = set()
        self._closing_ids: set[str] = set()
        self._entry_lock = asyncio.Lock()
        self._fill_lock = asyncio.Lock()
        self._running = False
        self.funding_before_close = None
        self._liquidity_failures: dict[tuple[str, str, str], int] = {}
        self._liquidity_cooldown_until: dict[tuple[str, str, str], float] = {}

    async def restore(self) -> None:
        for pos in await self._db.get_open_positions():
            if pos.mode != self._cfg.bot_mode:
                continue
            if pos.long_remaining is None:
                pos.long_remaining = pos.long_order.filled_size if pos.long_order else ZERO
            if pos.short_remaining is None:
                pos.short_remaining = pos.short_order.filled_size if pos.short_order else ZERO
            if pos.mode == BotMode.PAPER:
                # Missing observations during downtime must not accrue at today's rate.
                pos.last_funding_at = utc_now()
            if pos.mode == BotMode.LIVE:
                # A local journal alone cannot prove the current exchange exposure.
                pos.needs_reconciliation = True
            elif pos.status != PositionStatus.OPEN:
                pos.status = PositionStatus.CLOSING
            self._open_positions[pos.id] = pos
            await self._db.save_position(pos)

    async def run(self, opportunities: asyncio.Queue[FundingSpread]) -> None:
        self._running = True
        while self._running:
            try:
                spread = await asyncio.wait_for(opportunities.get(), timeout=5)
                try:
                    await self._handle_opportunity(spread)
                finally:
                    opportunities.task_done()
            except TimeoutError:
                continue
            except Exception:
                log.exception("executor.error")

    async def stop(self) -> None:
        self._running = False

    async def _paper_balance(self, exchange: str) -> Balance:
        ledger = await self._db.get_ledger_totals(exchange=exchange, mode=BotMode.PAPER)
        cash = self._cfg.paper_balance_usd + ledger["net_pnl"]
        reserved = ZERO
        unrealized = ZERO
        for pos in self._open_positions.values():
            for name, order, remaining, side in (
                (pos.long_exchange, pos.long_order, pos.long_remaining, OrderSide.LONG),
                (pos.short_exchange, pos.short_order, pos.short_remaining, OrderSide.SHORT),
            ):
                if name != exchange or order is None:
                    continue
                quantity = remaining if remaining is not None else order.filled_size
                reserved += (
                    quantity
                    * order.avg_price
                    / (Decimal("1") if exchange == "hyperliquid_spot" else pos.leverage)
                )
                if quantity:
                    mark = await self._adapters[exchange].get_mark_price(pos.symbol)
                    unrealized += (
                        quantity * (mark - order.avg_price) * (1 if side == OrderSide.LONG else -1)
                    )
        equity = cash + unrealized
        return Balance(exchange, equity, max(ZERO, equity - reserved))

    async def _handle_opportunity(self, spread: FundingSpread) -> None:
        if self._cfg.bot_mode == BotMode.DRY_RUN:
            return
        if self._cfg.bot_mode == BotMode.LIVE and any(
            getattr(adapter, "supports_live", True) is False for adapter in self._adapters.values()
        ):
            raise RuntimeError("An exchange adapter does not support confirmed live execution")
        async with self._entry_lock:
            symbol = spread.symbol
            liquidity_key = (symbol, spread.long_exchange, spread.short_exchange)
            cooldown_until = self._liquidity_cooldown_until.get(liquidity_key, 0)
            if cooldown_until > time.monotonic():
                return
            self._liquidity_cooldown_until.pop(liquidity_key, None)
            aliases = {symbol}
            for adapter in self._adapters.values():
                if isinstance(adapter, NormalizedAdapter):
                    aliases.update(adapter.native_aliases(symbol))
            if any(p.symbol in aliases for p in self._open_positions.values()):
                return
            if any(
                p.needs_reconciliation or p.status != PositionStatus.OPEN
                for p in self._open_positions.values()
            ):
                return
            if (
                utc_now() - as_utc(spread.timestamp)
            ).total_seconds() > self._cfg.max_signal_age_sec:
                return
            if self._cfg.reentry_cooldown_sec:
                dates = [
                    await self._db.get_last_closed_at(alias, self._cfg.bot_mode)
                    for alias in sorted(aliases)
                ]
                last_closed = max((date for date in dates if date is not None), default=None)
                if last_closed is not None:
                    elapsed = (utc_now() - as_utc(last_closed)).total_seconds()
                    if elapsed < self._cfg.reentry_cooldown_sec:
                        log.info(
                            "executor.skip_reentry_cooldown",
                            symbol=symbol,
                            remaining_sec=round(self._cfg.reentry_cooldown_sec - elapsed),
                        )
                        return
            if self._cfg.pair_loss_cooldown_sec:
                until = await self._db.pair_loss_cooldown_until(
                    symbol,
                    spread.long_exchange,
                    spread.short_exchange,
                    self._cfg.bot_mode,
                    self._cfg,
                    symbols=aliases,
                )
                if until is not None and until > utc_now():
                    log.info(
                        "executor.skip_pair_loss_cooldown", symbol=symbol, until=until.isoformat()
                    )
                    return
            if self._cfg.max_daily_loss_usd > 0:
                daily = await self._db.get_daily_pnl(mode=self._cfg.bot_mode)
                if daily["net_pnl"] <= -self._cfg.max_daily_loss_usd:
                    log.warning(
                        "executor.skip_daily_loss_limit",
                        net_pnl_usd=str(daily["net_pnl"]),
                        limit_usd=str(self._cfg.max_daily_loss_usd),
                    )
                    return
            self._opening_symbols.add(symbol)
            try:
                await self._open_pair(spread)
            finally:
                self._opening_symbols.discard(symbol)

    async def _open_pair(self, spread: FundingSpread) -> None:
        long_adapter = self._adapters[spread.long_exchange]
        short_adapter = self._adapters[spread.short_exchange]
        balances = (
            (self._paper_balance(spread.long_exchange), self._paper_balance(spread.short_exchange))
            if self._cfg.bot_mode == BotMode.PAPER
            else (long_adapter.get_balance(), short_adapter.get_balance())
        )
        balance_long, balance_short, ob_long, ob_short = await asyncio.gather(
            *balances,
            long_adapter.get_orderbook(spread.symbol),
            short_adapter.get_orderbook(spread.symbol),
        )
        try:
            rates = await asyncio.gather(
                long_adapter.get_funding_rate(spread.symbol),
                short_adapter.get_funding_rate(spread.symbol),
            )
            if any(
                r.interval_hours <= 0
                or not r.rate.is_finite()
                or not -5
                <= (utc_now() - as_utc(r.timestamp)).total_seconds()
                <= self._cfg.max_funding_age_sec
                for r in rates
            ):
                raise ValueError("stale_or_invalid_funding")
            long_rate, short_rate = (r.rate / Decimal(r.interval_hours) for r in rates)
            if getattr(short_adapter, "is_spot", False) is True or (
                getattr(long_adapter, "is_spot", False) is True and short_rate <= 0
            ):
                raise ValueError("invalid_spot_carry_direction")
            current = short_rate - long_rate
            forecast = min(
                current,
                spread.forecast_spread if spread.forecast_spread is not None else spread.spread,
            )
            spread = replace(
                spread,
                long_rate=long_rate,
                short_rate=short_rate,
                spread=current,
                forecast_spread=forecast,
            )
            fee_rates = await asyncio.gather(
                long_adapter.get_fee_rate(spread.symbol), short_adapter.get_fee_rate(spread.symbol)
            )
            fees = tuple(f.taker for f in fee_rates)
            if any(not f.is_finite() or f < 0 for f in fees):
                raise ValueError("invalid_fee")
            horizon = min(
                spread.forecast_hours
                if spread.forecast_hours is not None
                else self._cfg.expected_hold_hours,
                Decimal(self._cfg.max_hold_hours),
            )
            funding_hours = tuple(
                paid_hours_within(rate, adapter.funding_model, horizon, utc_now())
                for rate, adapter in zip(rates, (long_adapter, short_adapter), strict=True)
            )
        except Exception as exc:
            log.warning(
                "executor.skip_entry_data",
                symbol=spread.symbol,
                reason=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
            return
        can_open, reason, notional = await self._risk.assess_open(
            spread,
            len(self._open_positions),
            balance_long,
            balance_short,
            ob_long,
            ob_short,
            fees=fees,
            funding_hours=funding_hours,
        )
        if not can_open:
            liquidity_key = (spread.symbol, spread.long_exchange, spread.short_exchange)
            liquidity_failure = reason.startswith("слишком большое проскальзывание") or reason in {
                "insufficient_round_trip_liquidity",
                "insufficient_stressed_exit_liquidity",
            } or reason.startswith("stressed exit slippage too large")
            if liquidity_failure and self._cfg.liquidity_cooldown_sec > 0:
                failures = self._liquidity_failures.get(liquidity_key, 0) + 1
                if failures >= self._cfg.liquidity_reject_limit:
                    self._liquidity_failures.pop(liquidity_key, None)
                    self._liquidity_cooldown_until[liquidity_key] = (
                        time.monotonic() + self._cfg.liquidity_cooldown_sec
                    )
                    log.info(
                        "executor.liquidity_cooldown_started",
                        symbol=spread.symbol,
                        long_exchange=spread.long_exchange,
                        short_exchange=spread.short_exchange,
                        cooldown_sec=self._cfg.liquidity_cooldown_sec,
                    )
                else:
                    self._liquidity_failures[liquidity_key] = failures
            else:
                self._liquidity_failures.pop(liquidity_key, None)
            log.info(
                "executor.skip_risk_check",
                reason=reason,
                symbol=spread.symbol,
                long_exchange=spread.long_exchange,
                short_exchange=spread.short_exchange,
                spread_per_hour=str(spread.spread),
                estimated_net_pnl=str(spread.estimated_net_pnl),
            )
            return
        liquidity_key = (spread.symbol, spread.long_exchange, spread.short_exchange)
        self._liquidity_failures.pop(liquidity_key, None)
        self._liquidity_cooldown_until.pop(liquidity_key, None)
        # Reject obviously mismatched markets before opening. Monitoring measures
        # adverse basis movement relative to actual entry fills instead.
        try:
            long_mark, short_mark = await asyncio.gather(
                long_adapter.get_mark_price(spread.symbol),
                short_adapter.get_mark_price(spread.symbol),
            )
            if not all(p.is_finite() and p > 0 for p in (long_mark, short_mark)):
                raise ValueError("Invalid mark price")
        except Exception:
            log.warning("executor.skip_mark_unavailable", symbol=spread.symbol)
            return
        delta = abs(long_mark - short_mark) / ((long_mark + short_mark) / 2)
        if delta >= self._cfg.delta_stop_loss_pct:
            log.info(
                "executor.skip_entry_delta",
                symbol=spread.symbol,
                delta=str(delta),
                long_exchange=spread.long_exchange,
                short_exchange=spread.short_exchange,
            )
            return
        # Fail before trading when settlement data cannot even be initialized.
        try:
            await asyncio.gather(
                observe_leg(long_adapter, spread.symbol, "long", ZERO, {}, utc_now()),
                observe_leg(short_adapter, spread.symbol, "short", ZERO, {}, utc_now()),
            )
        except Exception as exc:
            log.warning("executor.skip_funding_accounting", symbol=spread.symbol, reason=str(exc))
            return
        position = ArbitragePosition(
            id=uuid.uuid4().hex,
            symbol=spread.symbol,
            long_exchange=spread.long_exchange,
            short_exchange=spread.short_exchange,
            notional_usd=notional,
            leverage=self._cfg.leverage,
            entry_spread=spread.spread,
            mode=self._cfg.bot_mode,
        )
        self._open_positions[position.id] = position
        await self._db.save_position(position)
        try:
            long_result, short_result = await self._open_both_legs(
                position,
                ob_long,
                ob_short,
                long_adapter,
                short_adapter,
            )
            position.long_order, position.short_order = long_result, short_result
            position.long_remaining = long_result.filled_size
            position.short_remaining = short_result.filled_size
            position.fees_paid = long_result.fee_paid + short_result.fee_paid
            position.needs_reconciliation = long_result.uncertain or short_result.uncertain
            position.update_net_pnl()
            await self._db.save_position(position)
            # Quantities, not dollar notionals, must match to hedge price movements.
            balanced = (
                long_result.success
                and short_result.success
                and long_result.filled_size > 0
                and long_result.filled_size == short_result.filled_size
            )
            if not balanced or position.needs_reconciliation:
                await self._rollback(
                    position, long_result, short_result, long_adapter, short_adapter
                )
                return
            position.status = PositionStatus.OPEN
            position.last_funding_at = utc_now()
            for leg, adapter in (("long", long_adapter), ("short", short_adapter)):
                _, _, state = await observe_leg(adapter, position.symbol, leg, ZERO, {}, utc_now())
                position.funding_state[leg] = state
            await self._db.save_position(position)
            self._risk.record_success()
            log.info(
                "executor.position_opened",
                position_id=position.id,
                symbol=position.symbol,
                long_exchange=position.long_exchange,
                short_exchange=position.short_exchange,
                notional_usd=str(position.notional_usd),
                entry_spread_per_hour=str(position.entry_spread),
                entry_fees_usd=str(position.fees_paid),
            )
            await self._notifier.on_position_opened(position, spread)
        except BaseException:
            # Includes cancellation while an exchange request is in flight.
            position.needs_reconciliation = self._cfg.bot_mode == BotMode.LIVE
            position.status = PositionStatus.CLOSING
            await asyncio.shield(self._db.save_position(position))
            raise

    async def _open_both_legs(self, position, ob_long, ob_short, long_adapter, short_adapter):
        # Same base quantity on both exchanges; each leg stays within the USD budget.
        quantity = min(
            position.notional_usd / ob_long.asks[0].price,
            position.notional_usd / ob_short.bids[0].price,
        )
        requests = [
            OrderRequest(
                exchange,
                position.symbol,
                side,
                position.notional_usd,
                position.leverage,
                client_id=uuid.uuid4().hex,
                quantity=quantity,
            )
            for exchange, side in (
                (position.long_exchange, OrderSide.LONG),
                (position.short_exchange, OrderSide.SHORT),
            )
        ]

        async def submit(adapter, req, ob, leg):
            result = (
                await self._simulate_order(req, ob)
                if self._cfg.bot_mode == BotMode.PAPER
                else await self._place_with_retry(adapter, req)
            )
            async with self._fill_lock:
                setattr(position, f"{leg}_order", result)
                setattr(position, f"{leg}_remaining", result.filled_size)
                position.needs_reconciliation |= result.uncertain
                position.fees_paid += result.fee_paid
                position.update_net_pnl()
                await self._db.record_fill(position, result)
            return result

        return await asyncio.gather(
            submit(long_adapter, requests[0], ob_long, "long"),
            submit(short_adapter, requests[1], ob_short, "short"),
        )

    async def _place_with_retry(self, adapter, req, max_attempts: int = 3) -> OrderResult:
        """One submission only: an HTTP failure is not proof an order was rejected."""
        if not req.client_id:
            req.client_id = uuid.uuid4().hex
        try:
            result = await adapter.place_order(req)
        except Exception as exc:
            return OrderResult(
                req.exchange,
                req.symbol,
                req.client_id,
                req.side,
                ZERO,
                ZERO,
                ZERO,
                False,
                str(exc),
                uncertain=True,
            )
        # Legacy adapters signal transport uncertainty through the error string.
        if not result.success and any(
            word in result.error.lower()
            for word in ("timeout", "connection", "unknown", "unconfirmed")
        ):
            result.uncertain = True
        return result

    async def _rollback(self, position, long_result, short_result, long_adapter, short_adapter):
        self._risk.record_failure()
        position.status = PositionStatus.CLOSING
        await self._db.save_position(position)
        await self._notifier.on_error(
            f"Incomplete pair {position.symbol}; closing confirmed exposure"
        )
        # Unknown fills require reconciliation, never a speculative opposite order.
        if not position.needs_reconciliation:
            await self.close_position(position, "rollback")

    async def _submit_close(self, position, leg, side):
        remaining = getattr(position, f"{leg}_remaining")
        entry = getattr(position, f"{leg}_order")
        if remaining is None:
            if entry is None:
                raise ValueError("Cannot close a leg without its persisted entry fill")
            remaining = entry.filled_size
            setattr(position, f"{leg}_remaining", remaining)
        if remaining == 0:
            return None
        adapter = self._adapters[getattr(position, f"{leg}_exchange")]
        if self._cfg.bot_mode == BotMode.PAPER:
            book = await adapter.get_orderbook(position.symbol)
            req = OrderRequest(
                adapter.name,
                position.symbol,
                side,
                remaining * entry.avg_price,
                position.leverage,
                is_reduce_only=True,
                quantity=remaining,
            )
            return await self._simulate_order(req, book)
        return await adapter.close_position(position.symbol, leg)

    async def close_position(self, position: ArbitragePosition, reason: str) -> None:
        if position.status == PositionStatus.CLOSED or position.id in self._closing_ids:
            return
        if position.needs_reconciliation:
            await self._notifier.on_error(f"{position.symbol}: exchange reconciliation required")
            return
        self._closing_ids.add(position.id)
        self._open_positions[position.id] = position
        try:
            if self.funding_before_close is not None and position.status == PositionStatus.OPEN:
                try:
                    await self.funding_before_close(position, {})
                except Exception as exc:
                    # Settlement outages must never disable emergency liquidation.
                    position.funding_state["accounting_incomplete"] = True
                    log.error(
                        "executor.close_funding_incomplete",
                        position_id=position.id,
                        reason=str(exc),
                    )
            position.status = PositionStatus.CLOSING
            if not position.close_reason:
                position.close_reason = reason
            await self._db.save_position(position)
            legs = (("long", OrderSide.SHORT), ("short", OrderSide.LONG))
            results = await asyncio.gather(
                *(self._submit_close(position, leg, side) for leg, side in legs),
                return_exceptions=True,
            )
            for (leg, _), result in zip(legs, results, strict=True):
                if isinstance(result, BaseException):
                    position.needs_reconciliation |= self._cfg.bot_mode == BotMode.LIVE
                    log.error("executor.close_failed", leg=leg, error=str(result))
                    await self._db.save_position(position)
                    continue
                if result is None:
                    continue
                entry = getattr(position, f"{leg}_order")
                remaining = getattr(position, f"{leg}_remaining")
                if result.uncertain or (not result.success and "timeout" in result.error.lower()):
                    position.needs_reconciliation = True
                if result.filled_size > remaining or result.filled_size < 0:
                    position.needs_reconciliation = True
                    await self._db.save_position(position)
                    continue
                if result.filled_size > 0 and result.avg_price > 0:
                    realized = result.filled_size * (result.avg_price - entry.avg_price)
                    if leg == "short":
                        realized = -realized
                    position.realized_price_pnl += realized
                    position.fees_paid += result.fee_paid
                    setattr(position, f"{leg}_remaining", remaining - result.filled_size)
                    position.unrealized_price_pnl = ZERO
                    position.update_net_pnl()
                    try:
                        await self._db.record_fill(position, result, realized)
                    except BaseException:
                        position.needs_reconciliation = True
                        raise
                elif not result.success:
                    log.warning("executor.close_rejected", leg=leg, error=result.error)
            flat = position.long_remaining == ZERO and position.short_remaining == ZERO
            if flat and not position.needs_reconciliation:
                position.status = PositionStatus.CLOSED
                position.closed_at = utc_now()
                position.unrealized_price_pnl = ZERO
                position.update_net_pnl()
                await self._db.save_position(position)
                self._open_positions.pop(position.id, None)
                log.info(
                    "executor.position_closed",
                    symbol=position.symbol,
                    reason=position.close_reason,
                    net_pnl=str(position.net_pnl),
                )
                await self._notifier.on_position_closed(position, position.close_reason)
            else:
                await self._db.save_position(position)
                self._risk.record_failure()
                await self._notifier.on_error(
                    f"{position.symbol}: close incomplete; position retained"
                )
        except asyncio.CancelledError:
            position.needs_reconciliation = True
            await asyncio.shield(self._db.save_position(position))
            raise
        except Exception:
            # An exchange fill may have completed before local persistence failed.
            # Keep the position visible and prohibit blind retries.
            position.needs_reconciliation = True
            with suppress(Exception):
                await asyncio.shield(self._db.save_position(position))
            raise
        finally:
            self._closing_ids.discard(position.id)

    async def close_all(self, reason: str = "shutdown") -> None:
        # Retry confirmed remaining quantities, never repeat a completed leg.
        for _ in range(3):
            positions = [p for p in self._open_positions.values() if not p.needs_reconciliation]
            if not positions:
                break
            for position in positions:
                await self.close_position(position, reason)
            if self._open_positions:
                await asyncio.sleep(1)

    async def _simulate_order(self, req: OrderRequest, ob) -> OrderResult:
        adapter = self._adapters.get(req.exchange)
        try:
            fee = (await adapter.get_fee_rate(req.symbol)).taker
        except Exception:
            fee = Decimal("0.0005")
        quantity, cost = ZERO, ZERO
        remaining = req.quantity if req.quantity is not None else req.notional_usd
        levels = ob.asks if req.side == OrderSide.LONG else ob.bids
        for level in levels:
            if level.price <= 0 or level.size <= 0:
                continue
            if req.quantity is None:
                value = min(remaining, level.size * level.price)
                take = value / level.price
                remaining -= value
            else:
                take = min(level.size, remaining)
                value = take * level.price
                remaining -= take
            quantity += take
            cost += value
            if remaining == 0:
                break
        return OrderResult(
            req.exchange,
            req.symbol,
            f"paper_{uuid.uuid4().hex}",
            req.side,
            quantity,
            cost / quantity if quantity else ZERO,
            cost * fee,
            remaining == 0 and quantity > 0,
            "" if remaining == 0 and quantity > 0 else "Insufficient orderbook liquidity",
        )

    @property
    def open_positions(self) -> dict[str, ArbitragePosition]:
        return self._open_positions
