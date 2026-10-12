"""Risk manager — проверка лимитов перед открытием позиций."""

import asyncio
from collections.abc import Callable
from decimal import Decimal

import structlog

from src.config import Settings
from src.models import (
    ArbitragePosition,
    Balance,
    FundingSpread,
    Orderbook,
    OrderSide,
    as_utc,
    utc_now,
)

log = structlog.get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("risk.callback_task_failed", task=task.get_name(), error=str(task.exception()))


async def _safe_call(coro, label: str) -> None:
    """Выполняет coroutine, перехватывая и логируя любые исключения."""
    try:
        await coro
    except Exception as e:
        log.error("risk.callback_failed", label=label, error=str(e))


# Алерт если delta достигла этой доли от stop-loss порога (0.7 = 70%)
_DELTA_ALERT_THRESHOLD = Decimal("0.70")


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_delay = settings.circuit_breaker_base_delay_sec
        # Callback для уведомлений (устанавливается из main.py)
        self._on_circuit_open: Callable | None = None
        self._on_delta_warning: Callable | None = None
        # Cooldown counter: сколько подряд циклов спред ниже exit_threshold
        self._low_spread_count: dict[str, int] = {}  # pos_id → count
        self._take_profit_count: dict[str, int] = {}  # pos_id → consecutive executable quotes

    def set_callbacks(
        self,
        on_circuit_open: Callable | None = None,
        on_delta_warning: Callable | None = None,
    ) -> None:
        self._on_circuit_open = on_circuit_open
        self._on_delta_warning = on_delta_warning

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    # ── Проверки перед входом ─────────────────────────────────────────────────

    async def can_open(
        self,
        spread: FundingSpread,
        open_pairs: int,
        balance_long: Balance,
        balance_short: Balance,
        ob_long: Orderbook,
        ob_short: Orderbook,
        fees: tuple[Decimal, Decimal] | None = None,
        funding_hours: tuple[Decimal, Decimal] | None = None,
    ) -> tuple[bool, str]:
        allowed, reason, _ = await self.assess_open(
            spread,
            open_pairs,
            balance_long,
            balance_short,
            ob_long,
            ob_short,
            fees=fees,
            funding_hours=funding_hours,
        )
        return allowed, reason

    async def assess_open(
        self,
        spread: FundingSpread,
        open_pairs: int,
        balance_long: Balance,
        balance_short: Balance,
        ob_long: Orderbook,
        ob_short: Orderbook,
        fees: tuple[Decimal, Decimal] | None = None,
        funding_hours: tuple[Decimal, Decimal] | None = None,
    ) -> tuple[bool, str, Decimal]:
        """Return approval and the largest notional that passes depth and economics."""
        if self._circuit_open:
            return False, "circuit breaker активен", Decimal("0")

        if not spread.entry_ready:
            return False, spread.entry_reason or "history_not_ready", Decimal("0")
        for book in (ob_long, ob_short):
            if (
                not -5
                <= (utc_now() - as_utc(book.timestamp)).total_seconds()
                <= self._cfg.max_book_age_sec
            ):
                return False, "stale_orderbook", Decimal("0")

        if open_pairs >= self._cfg.max_open_pairs:
            return (
                False,
                f"достигнут лимит пар: {open_pairs}/{self._cfg.max_open_pairs}",
                Decimal("0"),
            )

        max_notional = self._calc_notional(balance_long, balance_short)
        if max_notional <= 0:
            return False, "недостаточно маржи", Decimal("0")

        if not ob_long.bids or not ob_long.asks:
            return False, f"пустой стакан на {spread.long_exchange}", Decimal("0")
        if not ob_short.bids or not ob_short.asks:
            return False, f"пустой стакан на {spread.short_exchange}", Decimal("0")
        if spread.spread < self._cfg.min_funding_spread:
            return False, "funding spread below entry threshold", Decimal("0")

        allowed, max_reason = self._check_notional(
            spread, max_notional, ob_long, ob_short, fees, funding_hours
        )
        if allowed:
            self._log_entry_economics(
                spread, max_notional, ob_long, ob_short, fees, funding_hours
            )
            return True, "", max_notional

        min_notional = min(max_notional, self._cfg.min_position_usd)
        allowed, min_reason = self._check_notional(
            spread, min_notional, ob_long, ob_short, fees, funding_hours
        )
        if not allowed:
            self._log_entry_economics(
                spread, min_notional, ob_long, ob_short, fees, funding_hours
            )
            return False, min_reason, Decimal("0")

        low, high = min_notional, max_notional
        for _ in range(self._cfg.position_sizing_iterations):
            candidate = (low + high) / 2
            candidate_ok, _ = self._check_notional(
                spread, candidate, ob_long, ob_short, fees, funding_hours
            )
            if candidate_ok:
                low = candidate
            else:
                high = candidate
        selected = max(
            min_notional,
            low * self._cfg.position_size_headroom,
        ).quantize(Decimal("0.01"))
        # Rounding can cross an orderbook level, so verify and step down one cent.
        selected_ok, reason = self._check_notional(
            spread, selected, ob_long, ob_short, fees, funding_hours
        )
        if not selected_ok:
            selected -= Decimal("0.01")
            selected_ok, reason = self._check_notional(
                spread, selected, ob_long, ob_short, fees, funding_hours
            )
        if not selected_ok or selected < min_notional:
            return False, reason, Decimal("0")
        log.info(
            "risk.position_sized",
            symbol=spread.symbol,
            requested_notional_usd=str(max_notional),
            selected_notional_usd=str(selected),
            reduction_pct=str((max_notional - selected) / max_notional),
            limiting_reason=max_reason,
        )
        self._log_entry_economics(spread, selected, ob_long, ob_short, fees, funding_hours)
        return True, "", selected

    def _check_notional(self, spread, notional, ob_long, ob_short, fees, funding_hours):
        if notional <= 0:
            return False, "invalid_position_size"

        slippage_long = ob_long.estimate_slippage(OrderSide.LONG, notional)
        slippage_short = ob_short.estimate_slippage(OrderSide.SHORT, notional)
        total_slippage = slippage_long + slippage_short

        if total_slippage > self._cfg.max_slippage:
            return (
                False,
                "слишком большое проскальзывание: "
                f"total={total_slippage:.4%}, long={slippage_long:.4%}, "
                f"short={slippage_short:.4%}",
            )

        forecast = spread.forecast_spread if spread.forecast_spread is not None else spread.spread
        horizon = min(
            spread.forecast_hours
            if spread.forecast_hours is not None
            else self._cfg.expected_hold_hours,
            Decimal(self._cfg.max_hold_hours),
        )
        quantity = min(notional / ob_long.asks[0].price, notional / ob_short.bids[0].price)
        prices = (
            ob_long.execution_price(OrderSide.LONG, quantity),
            ob_long.execution_price(OrderSide.SHORT, quantity),
            ob_short.execution_price(OrderSide.SHORT, quantity),
            ob_short.execution_price(OrderSide.LONG, quantity),
        )
        if any(price is None for price in prices):
            return False, "insufficient_round_trip_liquidity"
        buy_long, sell_long, sell_short, buy_short = prices
        # A book that can fill the position only at the current snapshot is too
        # fragile for a market exit. Size against deeper close-side liquidity so
        # a modest withdrawal of quotes does not turn a small stop into a large
        # realized loss.
        stressed_quantity = quantity * self._cfg.exit_liquidity_multiplier
        stressed_sell_long = ob_long.execution_price(OrderSide.SHORT, stressed_quantity)
        stressed_buy_short = ob_short.execution_price(OrderSide.LONG, stressed_quantity)
        if stressed_sell_long is None or stressed_buy_short is None:
            return False, "insufficient_stressed_exit_liquidity"
        mid_long = (ob_long.bids[0].price + ob_long.asks[0].price) / 2
        mid_short = (ob_short.bids[0].price + ob_short.asks[0].price) / 2
        stressed_exit_slippage = (
            max(Decimal("0"), (mid_long - stressed_sell_long) / mid_long)
            + max(Decimal("0"), (stressed_buy_short - mid_short) / mid_short)
        )
        if stressed_exit_slippage > self._cfg.max_slippage:
            return (
                False,
                "stressed exit slippage too large: "
                f"total={stressed_exit_slippage:.4%}, "
                f"depth_multiplier={self._cfg.exit_liquidity_multiplier}",
            )
        trading_cost = quantity * (buy_long - sell_long + buy_short - sell_short)
        if fees is None:
            # Backward-compatible direct risk calls; executor always supplies fresh fees.
            fee_cost = max(Decimal("0"), forecast * horizon - spread.estimated_net_pnl) * notional
        else:
            fee_cost = quantity * (
                (buy_long + sell_long) * fees[0] + (sell_short + buy_short) * fees[1]
            )
        cost = trading_cost + fee_cost
        basis_reserve = max(Decimal("0"), mid_long - mid_short) * quantity
        basis_risk_buffer = notional * self._cfg.basis_risk_buffer_pct
        cost += basis_reserve + basis_risk_buffer
        income_per_hour = min(
            quantity * (buy_long + sell_short) / 2 * forecast,
            quantity * (sell_short * spread.short_rate - buy_long * spread.long_rate),
        )
        expected = income_per_hour * horizon
        if funding_hours is not None:
            scheduled = quantity * (
                sell_short * spread.short_rate * funding_hours[1]
                - buy_long * spread.long_rate * funding_hours[0]
            )
            expected = min(expected, scheduled)
        payback = cost / income_per_hour if income_per_hour > 0 else Decimal("Infinity")
        if (
            forecast <= 0
            or payback > horizon * self._cfg.max_entry_payback_fraction
            or expected
            < cost * self._cfg.entry_cost_coverage + notional * self._cfg.min_net_pnl_after_fees
        ):
            return (
                False,
                "expected funding does not cover round-trip costs: "
                f"funding={expected:.6f}, costs={cost:.6f}, payback_h={payback:.2f}",
            )
        return True, ""

    def _log_entry_economics(self, spread, notional, ob_long, ob_short, fees, funding_hours):
        forecast = spread.forecast_spread if spread.forecast_spread is not None else spread.spread
        horizon = min(
            spread.forecast_hours
            if spread.forecast_hours is not None
            else self._cfg.expected_hold_hours,
            Decimal(self._cfg.max_hold_hours),
        )
        quantity = min(notional / ob_long.asks[0].price, notional / ob_short.bids[0].price)
        prices = (
            ob_long.execution_price(OrderSide.LONG, quantity),
            ob_long.execution_price(OrderSide.SHORT, quantity),
            ob_short.execution_price(OrderSide.SHORT, quantity),
            ob_short.execution_price(OrderSide.LONG, quantity),
        )
        if any(price is None for price in prices):
            log.info("risk.entry_economics", symbol=spread.symbol, notional_usd=str(notional),
                     result="insufficient_round_trip_liquidity")
            return
        buy_long, sell_long, sell_short, buy_short = prices
        trading_cost = quantity * (buy_long - sell_long + buy_short - sell_short)
        fee_cost = (
            max(Decimal("0"), forecast * horizon - spread.estimated_net_pnl) * notional
            if fees is None
            else quantity
            * ((buy_long + sell_long) * fees[0] + (sell_short + buy_short) * fees[1])
        )
        basis_reserve = max(
            Decimal("0"),
            (ob_long.bids[0].price + ob_long.asks[0].price) / 2
            - (ob_short.bids[0].price + ob_short.asks[0].price) / 2,
        ) * quantity
        basis_risk_buffer = notional * self._cfg.basis_risk_buffer_pct
        cost = trading_cost + fee_cost + basis_reserve + basis_risk_buffer
        income_per_hour = min(
            quantity * (buy_long + sell_short) / 2 * forecast,
            quantity * (sell_short * spread.short_rate - buy_long * spread.long_rate),
        )
        expected = income_per_hour * horizon
        if funding_hours is not None:
            expected = min(
                expected,
                quantity
                * (
                    sell_short * spread.short_rate * funding_hours[1]
                    - buy_long * spread.long_rate * funding_hours[0]
                ),
            )
        payback = cost / income_per_hour if income_per_hour > 0 else Decimal("Infinity")
        log.info(
            "risk.entry_economics",
            symbol=spread.symbol,
            notional_usd=str(notional),
            quantity=str(quantity),
            round_trip_cost_usd=str(cost),
            expected_funding_usd=str(expected),
            expected_profit_usd=str(expected - cost),
            adverse_basis_reserve_usd=str(basis_reserve),
            basis_risk_buffer_usd=str(basis_risk_buffer),
            payback_hours=str(payback),
            max_payback_hours=str(horizon * self._cfg.max_entry_payback_fraction),
            horizon_hours=str(horizon),
        )

    def calc_position_size(self, balance_long: Balance, balance_short: Balance) -> Decimal:
        """Размер одной ноги в USD = min_balance × max_position_pct × leverage."""
        return self._calc_notional(balance_long, balance_short)

    def _calc_notional(self, balance_long: Balance, balance_short: Balance) -> Decimal:
        if balance_long.exchange == "hyperliquid_spot":
            return min(
                balance_long.available_usd * self._cfg.max_position_pct,
                balance_short.available_usd * self._cfg.max_position_pct * self._cfg.leverage,
            )
        min_balance = min(balance_long.available_usd, balance_short.available_usd)
        return min_balance * self._cfg.max_position_pct * self._cfg.leverage

    # ── Мониторинг открытых позиций ───────────────────────────────────────────

    def should_close(
        self,
        position: ArbitragePosition,
        current_spread: Decimal,
        current_delta_pct: Decimal,
        hold_hours: float,
        close_pnl: Decimal | None = None,
    ) -> tuple[bool, str]:
        """Проверяет нужно ли закрыть позицию."""
        if hold_hours >= self._cfg.max_hold_hours:
            return True, f"превышен max_hold_hours: {hold_hours:.1f}h"

        if current_delta_pct >= self._cfg.delta_stop_loss_pct:
            return (
                True,
                f"delta stop-loss: {current_delta_pct:.2%} ≥ {self._cfg.delta_stop_loss_pct:.2%}",
            )

        if (
            close_pnl is not None
            and close_pnl <= -position.notional_usd * self._cfg.max_net_loss_pct
        ):
            return True, "net_loss_stop"

        if close_pnl is not None:
            previous_peak = position.peak_close_pnl
            position.peak_close_pnl = (
                close_pnl if previous_peak is None else max(previous_peak, close_pnl)
            )
            take_profit_ready = (
                self._cfg.take_profit_pct > 0
                and close_pnl >= position.notional_usd * self._cfg.take_profit_pct
            )
            if take_profit_ready:
                count = self._take_profit_count.get(position.id, 0) + 1
                self._take_profit_count[position.id] = count
                if count >= self._cfg.take_profit_confirmation_intervals:
                    self._take_profit_count.pop(position.id, None)
                    return True, "net_take_profit"
            else:
                self._take_profit_count.pop(position.id, None)
            activation = position.notional_usd * self._cfg.trailing_profit_activation_pct
            profit_floor = position.notional_usd * self._cfg.exit_profit_buffer_pct
            trailing_floor = position.peak_close_pnl * (
                Decimal("1") - self._cfg.trailing_profit_giveback_pct
            )
            if (
                self._cfg.trailing_profit_activation_pct > 0
                and position.peak_close_pnl >= activation
                and close_pnl >= profit_floor
                and close_pnl <= trailing_floor
            ):
                return True, "trailing_profit_exit"

        now = utc_now()
        if current_spread < 0:
            if position.negative_funding_since is None:
                position.negative_funding_since = now
            if (
                now - as_utc(position.negative_funding_since)
            ).total_seconds() >= self._cfg.negative_funding_exit_sec:
                return True, "sustained_negative_funding"
        else:
            position.negative_funding_since = None

        # Предупреждение до достижения stop-loss (70% от порога)
        alert_threshold = self._cfg.delta_stop_loss_pct * _DELTA_ALERT_THRESHOLD
        if current_delta_pct >= alert_threshold and self._on_delta_warning:
            asyncio.create_task(
                _safe_call(
                    self._on_delta_warning(
                        position.id,
                        position.symbol,
                        current_delta_pct,
                        self._cfg.delta_stop_loss_pct,
                    ),
                    label="on_delta_warning",
                )
            )

        # Cooldown: закрываем только после N последовательных циклов с низким спредом
        if current_spread <= self._cfg.exit_funding_spread:
            count = self._low_spread_count.get(position.id, 0) + 1
            self._low_spread_count[position.id] = count
            required = self._cfg.exit_spread_cooldown_intervals
            if count >= required:
                if hold_hours < self._cfg.min_soft_hold_hours or close_pnl is None:
                    return False, "waiting_for_soft_exit"
                buffer = position.notional_usd * self._cfg.exit_profit_buffer_pct
                if close_pnl >= buffer:
                    self._low_spread_count.pop(position.id, None)
                    return True, "low_funding_profitable_exit"
                # Sunk fees are not a reason to hold indefinitely. Only wait when
                # remaining conservative carry can plausibly recover the deficit.
                remaining = min(
                    self._cfg.max_recovery_hours,
                    max(Decimal("0"), Decimal(self._cfg.max_hold_hours) - Decimal(str(hold_hours))),
                )
                income = (
                    max(Decimal("0"), current_spread)
                    * position.notional_usd
                    * self._cfg.funding_forecast_discount
                )
                if current_spread >= 0 and income * remaining < buffer - close_pnl:
                    return True, "carry_cannot_recover_costs"
        else:
            self._low_spread_count.pop(position.id, None)

        return False, ""

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        log.warning("risk.order_failure", consecutive=self._consecutive_failures)
        if self._consecutive_failures >= self._cfg.circuit_breaker_failures:
            self._circuit_open = True
            log.error(
                "risk.circuit_breaker_open",
                delay_sec=self._circuit_delay,
                failures=self._consecutive_failures,
            )
            if self._on_circuit_open:
                asyncio.create_task(
                    _safe_call(
                        self._on_circuit_open(self._consecutive_failures, self._circuit_delay),
                        label="on_circuit_open",
                    )
                )
            asyncio.create_task(_safe_call(self._reset_circuit(), label="reset_circuit"))

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_delay = self._cfg.circuit_breaker_base_delay_sec

    async def _reset_circuit(self) -> None:
        await asyncio.sleep(self._circuit_delay)
        self._circuit_open = False
        # Exponential backoff: следующая пауза в 2 раза длиннее
        self._circuit_delay = min(self._circuit_delay * 2, 3600)
        log.info("risk.circuit_breaker_reset", next_delay_sec=self._circuit_delay)
