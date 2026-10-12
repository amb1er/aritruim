"""Paper funding estimates, mark-to-market P&L and position exit monitoring."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import structlog

from src.core.funding_accounting import observe_leg
from src.models import BotMode, OrderSide, PositionStatus, as_utc, utc_now

log = structlog.get_logger(__name__)
ZERO = Decimal("0")


class PnlTracker:
    def __init__(self, adapters, risk, db, settings):
        self._adapters, self._risk, self._db, self._cfg = adapters, risk, db, settings
        self._running = False
        self._executor = None
        self._missing_since = {}
        self._close_missing_since = {}
        self._spread_history = {}

    def set_executor(self, executor):
        self._executor = executor
        executor.funding_before_close = self._accrue_funding

    async def run(self):
        self._running = True
        while self._running:
            await asyncio.sleep(self._cfg.monitor_interval_sec)
            try:
                await self._monitor_once()
            except Exception:
                log.exception("pnl_tracker.error")

    async def stop(self):
        self._running = False

    async def _monitor_once(self):
        if self._executor is None:
            return
        positions = list(self._executor.open_positions.values())
        active = {p.id for p in positions}
        self._missing_since = {k: v for k, v in self._missing_since.items() if k in active}
        self._close_missing_since = {
            k: v for k, v in self._close_missing_since.items() if k in active
        }
        self._spread_history = {k: v for k, v in self._spread_history.items() if k in active}
        self._risk._low_spread_count = {
            k: v for k, v in self._risk._low_spread_count.items() if k in active
        }
        self._risk._take_profit_count = {
            k: v for k, v in self._risk._take_profit_count.items() if k in active
        }
        names = {name for p in positions for name in (p.long_exchange, p.short_exchange)}
        results = await asyncio.gather(
            *(self._adapters[name].get_funding_rates() for name in names), return_exceptions=True
        )
        rates = {
            name: {r.symbol: r for r in result}
            for name, result in zip(names, results, strict=True)
            if not isinstance(result, BaseException)
        }
        for pos in positions:
            try:
                if pos.needs_reconciliation:
                    continue
                if pos.status in (PositionStatus.CLOSING, PositionStatus.ROLLBACK):
                    await self._executor.close_position(pos, "retry_incomplete_close")
                    continue
                if pos.status != PositionStatus.OPEN:
                    continue
                close, reason = await self._check_position(pos, rates)
                if close:
                    await self._executor.close_position(pos, reason)
            except Exception:
                # One unavailable market must not disable all other exit checks.
                log.exception("pnl_tracker.position_failed", position_id=pos.id)
        await self._save_daily_snapshot(list(self._executor.open_positions.values()))

    async def _check_position(self, pos, rates_by_exchange):
        delta = await self._calc_delta(pos, rates_by_exchange)
        funding_ok = True
        try:
            await self._accrue_funding(pos, rates_by_exchange)
        except Exception as exc:
            funding_ok = False
            log.warning(
                "pnl_tracker.funding_unavailable",
                position_id=pos.id,
                reason=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
        pos.update_net_pnl()
        # Refresh executable close PnL before emergency decisions. Previously a
        # delta stop could return here with a quote from the preceding monitor
        # cycle, hiding a fast deterioration in exit liquidity.
        pos.close_pnl_estimate = await self._estimate_close_pnl(pos)
        await self._db.save_position(pos)
        long_rate = rates_by_exchange.get(pos.long_exchange, {}).get(pos.symbol)
        short_rate = rates_by_exchange.get(pos.short_exchange, {}).get(pos.symbol)
        hours = (utc_now() - as_utc(pos.opened_at)).total_seconds() / 3600
        if hours >= self._cfg.max_hold_hours:
            return True, "max_hold_hours"
        if delta is not None and delta >= self._cfg.delta_stop_loss_pct:
            return True, f"delta stop-loss: {delta:.2%}"
        if (
            long_rate
            and short_rate
            and min(long_rate.interval_hours, short_rate.interval_hours) > 0
            and all(
                r.rate.is_finite()
                and -5
                <= (utc_now() - as_utc(r.timestamp)).total_seconds()
                <= self._cfg.max_funding_age_sec
                for r in (long_rate, short_rate)
            )
        ):
            raw_spread = short_rate.rate / Decimal(
                short_rate.interval_hours
            ) - long_rate.rate / Decimal(long_rate.interval_hours)
            spread = self._filter_exit_spike(pos, raw_spread)
        else:
            spread = None
        if delta is None or spread is None or not funding_ok:
            pos.negative_funding_since = None
            self._risk._low_spread_count.pop(pos.id, None)
            await self._db.save_position(pos)
            since = self._missing_since.setdefault(pos.id, time.monotonic())
            elapsed = time.monotonic() - since
            reason = "price_data_unavailable" if delta is None else "funding_data_unavailable"
            log.warning(
                "pnl_tracker.waiting_for_data",
                position_id=pos.id,
                reason=reason,
                elapsed_sec=round(elapsed),
            )
            return elapsed >= self._cfg.market_data_grace_sec, reason
        self._missing_since.pop(pos.id, None)
        if pos.close_pnl_estimate is None:
            since = self._close_missing_since.setdefault(pos.id, time.monotonic())
            if time.monotonic() - since >= self._cfg.market_data_grace_sec:
                return True, "close_quote_unavailable"
        else:
            self._close_missing_since.pop(pos.id, None)
        decision = self._risk.should_close(pos, spread, delta, hours, pos.close_pnl_estimate)
        await self._db.save_position(pos)
        log.info(
            "pnl_tracker.exit_economics",
            position_id=pos.id,
            symbol=pos.symbol,
            funding=str(pos.funding_received),
            close_pnl=str(pos.close_pnl_estimate),
            peak_close_pnl=str(pos.peak_close_pnl),
            spread_per_hour=str(spread),
            hold_hours=round(hours, 3),
            decision=decision[1] or "hold",
        )
        return decision

    def _filter_exit_spike(self, pos, current):
        """Ignore a lone upward funding spike that could postpone a profitable exit."""
        history = self._spread_history.setdefault(pos.id, [])
        effective = current
        if len(history) >= 3 and current > 0:
            ordered = sorted(history[-5:])
            median = ordered[len(ordered) // 2]
            if median > 0 and current > median * self._cfg.exit_funding_spike_multiplier:
                effective = median
                log.warning(
                    "pnl_tracker.funding_spike_filtered",
                    position_id=pos.id,
                    symbol=pos.symbol,
                    raw_spread_per_hour=str(current),
                    effective_spread_per_hour=str(effective),
                )
        history.append(effective)
        del history[:-5]
        return effective

    async def _estimate_close_pnl(self, pos):
        """Cash PnL of closing remaining quantities, including all four fees."""
        result = pos.funding_received + pos.realized_price_pnl - pos.fees_paid
        try:
            for leg, side, sign in (("long", OrderSide.SHORT, 1), ("short", OrderSide.LONG, -1)):
                order = getattr(pos, f"{leg}_order")
                if order is None:
                    return None
                quantity = getattr(pos, f"{leg}_remaining")
                quantity = order.filled_size if quantity is None else quantity
                if quantity == 0:
                    continue
                adapter = self._adapters[getattr(pos, f"{leg}_exchange")]
                book, fee = await asyncio.gather(
                    adapter.get_orderbook(pos.symbol), adapter.get_fee_rate(pos.symbol)
                )
                if (
                    not -5
                    <= (utc_now() - as_utc(book.timestamp)).total_seconds()
                    <= self._cfg.max_book_age_sec
                ):
                    return None
                price = book.execution_price(side, quantity)
                if price is None or not fee.taker.is_finite() or fee.taker < 0:
                    return None
                result += quantity * ((price - order.avg_price) * sign - price * fee.taker)
            return result
        except Exception:
            log.warning("pnl_tracker.close_quote_unavailable", position_id=pos.id)
            return None

    async def _accrue_funding(self, pos, rates_by_exchange):
        if self._cfg.bot_mode == BotMode.LIVE:
            raise RuntimeError(
                "Live funding requires confirmed exchange payments; estimates are paper-only"
            )
        now = utc_now()
        payments = []
        states = {}
        for exchange, side, order, remaining in (
            (pos.long_exchange, "long", pos.long_order, pos.long_remaining),
            (pos.short_exchange, "short", pos.short_order, pos.short_remaining),
        ):
            if order is None:
                return ZERO
            quantity = remaining if remaining is not None else order.filled_size
            applied_rate, payment, state = await observe_leg(
                self._adapters[exchange],
                pos.symbol,
                side,
                quantity,
                pos.funding_state.get(side, {}),
                now,
                max_age_sec=max(180, self._cfg.monitor_interval_sec * 2),
            )
            states[side] = state
            if payment:
                payments.append((exchange, side, applied_rate, payment))
        total = sum((item[3] for item in payments), ZERO)
        old_funding, old_time, old_state = (
            pos.funding_received,
            pos.last_funding_at,
            pos.funding_state,
        )
        pos.funding_received += total
        pos.last_funding_at = now
        pos.funding_state = states
        pos.update_net_pnl()
        try:
            await self._db.record_funding(pos, payments)
        except BaseException:
            pos.funding_received, pos.last_funding_at = old_funding, old_time
            pos.funding_state = old_state
            pos.update_net_pnl()
            raise
        return total

    async def _calc_delta(self, pos, rates_by_exchange):
        try:
            long_price, short_price = await asyncio.gather(
                self._adapters[pos.long_exchange].get_mark_price(pos.symbol),
                self._adapters[pos.short_exchange].get_mark_price(pos.symbol),
            )
            if (
                not long_price.is_finite()
                or not short_price.is_finite()
                or long_price <= 0
                or short_price <= 0
            ):
                raise ValueError("Non-positive mark price")
            unrealized = ZERO
            if pos.long_order:
                qty = (
                    pos.long_remaining
                    if pos.long_remaining is not None
                    else pos.long_order.filled_size
                )
                unrealized += qty * (long_price - pos.long_order.avg_price)
            if pos.short_order:
                qty = (
                    pos.short_remaining
                    if pos.short_remaining is not None
                    else pos.short_order.filled_size
                )
                unrealized += qty * (pos.short_order.avg_price - short_price)
            pos.unrealized_price_pnl = unrealized
            if pos.long_order and pos.short_order and pos.notional_usd > 0:
                # Equal base quantities hedge market direction. Adverse change in
                # the cross-venue basis appears as negative paired price PnL.
                return max(ZERO, -unrealized / pos.notional_usd)
            return abs(long_price - short_price) / ((long_price + short_price) / 2)
        except Exception:
            # Missing prices are not evidence of zero risk.
            log.warning("pnl_tracker.price_unavailable", position_id=pos.id)
            return None

    async def _save_daily_snapshot(self, open_positions):
        ledger = await self._db.get_daily_pnl(mode=self._cfg.bot_mode)
        positions = await self._db.get_all_positions()
        closed = sum(
            1
            for p in positions
            if p.mode == self._cfg.bot_mode
            and p.closed_at
            and p.closed_at.date() == utc_now().date()
        )
        await self._db.save_pnl_snapshot(
            open_pairs=len(open_positions),
            closed_pairs=closed,
            funding_received=ledger["funding_received"],
            fees_paid=ledger["fees_paid"],
            net_pnl=ledger["net_pnl"],
        )
