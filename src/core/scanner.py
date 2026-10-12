"""
Scanner — опрашивает биржи, находит спреды funding rate.

Логика:
  1. Параллельно запросить funding rates со всех адаптеров
  2. Найти общие символы (intersection)
  3. Spike detection: пропускаем символы с аномальным funding < FUNDING_SPIKE_LOOKBACK_MIN назад
  4. Рассчитать спред и net PnL с учётом комиссий
  5. Вывести таблицу в лог; найденные возможности положить в очередь
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from src.adapters.base import ExchangeAdapter
from src.config import Settings
from src.core.funding_history import FundingHistory
from src.models import FundingRate, FundingSpread, as_utc

log = structlog.get_logger(__name__)

# Ставка считается аномальной если > этого множителя от среднего
_SPIKE_MULTIPLIER = Decimal("5")


class FundingScanner:
    def __init__(self, adapters: list[ExchangeAdapter], settings: Settings, db=None) -> None:
        self._adapters = adapters
        self._cfg = settings
        self._db = db
        self._history = FundingHistory(settings)
        self._history_loaded = False
        self._observation_time: datetime | None = None
        self.opportunities: asyncio.Queue[FundingSpread] = asyncio.Queue(maxsize=100)
        self._running = False
        self.last_scan_at: datetime | None = None
        self._entry_confirmations: dict[tuple[str, str, str], int] = {}
        # Кэш результатов spike check: {(exchange, symbol): bool}
        self._spike_cache: dict[tuple[str, str], bool] = {}

    # ── Основной цикл ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Бесконечный цикл сканирования. Остановить через stop()."""
        self._running = True
        log.info("scanner.started", interval_sec=self._cfg.scan_interval_sec)
        while self._running:
            try:
                await self._scan_once()
            except Exception as e:
                self._entry_confirmations.clear()
                log.error("scanner.error", error=str(e))
            await asyncio.sleep(self._cfg.scan_interval_sec)

    async def stop(self) -> None:
        self._running = False
        log.info("scanner.stopped")

    # ── Один цикл сканирования ────────────────────────────────────────────────

    async def _scan_once(self) -> None:
        # Spike results are reusable across exchange pairs within one scan, but
        # must be refreshed on the next scan.
        self._spike_cache.clear()
        # Параллельный сбор funding rates со всех бирж
        tasks = [adapter.get_funding_rates() for adapter in self._adapters]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        rates_by_exchange: dict[str, dict[str, FundingRate]] = {}
        for adapter, result in zip(self._adapters, results, strict=False):
            if isinstance(result, Exception):
                log.warning(
                    "scanner.fetch_failed",
                    exchange=adapter.name,
                    error=str(result) or repr(result),
                    error_type=type(result).__name__,
                )
                continue
            rates_by_exchange[adapter.name] = {r.symbol: r for r in result}

        now = datetime.now(UTC)
        self._observation_time = now
        rates_by_exchange = {
            ex: {
                symbol: rate
                for symbol, rate in rates.items()
                if rate.rate.is_finite()
                and rate.interval_hours > 0
                and -5
                <= (now - as_utc(rate.timestamp)).total_seconds()
                <= self._cfg.max_funding_age_sec
            }
            for ex, rates in rates_by_exchange.items()
        }
        if self._db is not None and not self._history_loaded:
            self._history.samples = await self._db.load_funding_observations()
            self._history_loaded = True
        rows = self._history.observe(rates_by_exchange, now)
        if self._db is not None:
            await self._db.save_funding_observations(rows, self._cfg)
        if len(rates_by_exchange) < 2:
            self._entry_confirmations.clear()
            log.warning("scanner.insufficient_exchanges", count=len(rates_by_exchange))
            return

        # Находим спреды между всеми парами бирж
        exchange_names = list(rates_by_exchange.keys())
        adapters_by_name = {adapter.name: adapter for adapter in self._adapters}
        spreads: list[FundingSpread] = []
        compared_symbols: set[str] = set()

        for i in range(len(exchange_names)):
            for j in range(i + 1, len(exchange_names)):
                ex_a = exchange_names[i]
                ex_b = exchange_names[j]
                # Spot can only be bought; two spot legs cannot earn perp funding.
                if (
                    getattr(adapters_by_name[ex_a], "is_spot", False) is True
                    and getattr(adapters_by_name[ex_b], "is_spot", False) is True
                ):
                    continue
                pair_symbols = self._get_common_symbols(
                    {ex_a: rates_by_exchange[ex_a], ex_b: rates_by_exchange[ex_b]}
                )
                compared_symbols.update(pair_symbols)
                pair_spreads = await self._find_spreads(
                    symbol_set=pair_symbols,
                    rates_a=rates_by_exchange[ex_a],
                    rates_b=rates_by_exchange[ex_b],
                )
                spreads.extend(pair_spreads)
        log.debug("scanner.compared_symbols", count=len(compared_symbols))

        # Сортируем по net PnL (лучшая возможность первой)
        spreads.sort(key=lambda s: s.estimated_net_pnl, reverse=True)

        self.last_scan_at = datetime.now(UTC)
        self._print_table(spreads)

        # Кладём выгодные возможности в очередь для Executor (M2)
        confirmed_this_scan = {}
        for spread in spreads:
            if spread.entry_ready and spread.estimated_net_pnl >= self._cfg.min_net_pnl_after_fees:
                key = (spread.symbol, spread.long_exchange, spread.short_exchange)
                count = self._entry_confirmations.get(key, 0) + 1
                confirmed_this_scan[key] = count
                if count < self._cfg.entry_confirmation_scans:
                    continue
                if self.opportunities.full():
                    self.opportunities.get_nowait()
                    self.opportunities.task_done()
                self.opportunities.put_nowait(spread)
        self._entry_confirmations = confirmed_this_scan

    # ── Вычисление спредов ────────────────────────────────────────────────────

    async def _find_spreads(
        self,
        symbol_set: set[str],
        rates_a: dict[str, FundingRate],
        rates_b: dict[str, FundingRate],
    ) -> list[FundingSpread]:
        spreads: list[FundingSpread] = []

        for symbol in symbol_set:
            rate_a = rates_a.get(symbol)
            rate_b = rates_b.get(symbol)
            if not rate_a or not rate_b or min(rate_a.interval_hours, rate_b.interval_hours) <= 0:
                continue

            # Нормализуем ставки к одному интервалу (per hour)
            rate_a_per_h = rate_a.rate / Decimal(rate_a.interval_hours)
            rate_b_per_h = rate_b.rate / Decimal(rate_b.interval_hours)

            # long_exchange = где ставка ниже (лонгу выгоднее или меньше платить)
            if rate_a_per_h <= rate_b_per_h:
                long_ex, short_ex = rate_a.exchange, rate_b.exchange
                long_rate, short_rate = rate_a_per_h, rate_b_per_h
            else:
                long_ex, short_ex = rate_b.exchange, rate_a.exchange
                long_rate, short_rate = rate_b_per_h, rate_a_per_h

            long_adapter = next(a for a in self._adapters if a.name == long_ex)
            short_adapter = next(a for a in self._adapters if a.name == short_ex)
            if getattr(short_adapter, "is_spot", False) is True:
                continue
            if getattr(long_adapter, "is_spot", False) is True and short_rate <= 0:
                continue

            spread = short_rate - long_rate
            if spread < self._cfg.min_funding_spread:
                continue

            forecast, horizon, history_hours, reason = self._history.forecast(
                symbol, long_ex, short_ex, spread, self._observation_time or datetime.now(UTC)
            )
            net_pnl = await self._estimate_net_pnl(symbol, long_ex, short_ex, forecast, horizon)

            spreads.append(
                FundingSpread(
                    symbol=symbol,
                    long_exchange=long_ex,
                    short_exchange=short_ex,
                    long_rate=long_rate,
                    short_rate=short_rate,
                    spread=spread,
                    estimated_net_pnl=net_pnl,
                    forecast_spread=forecast,
                    forecast_hours=horizon,
                    history_hours=history_hours,
                    entry_ready=not reason,
                    entry_reason=reason,
                )
            )

        return spreads

    async def _estimate_net_pnl(
        self,
        symbol: str,
        long_exchange: str,
        short_exchange: str,
        spread: Decimal,
        horizon: Decimal | None = None,
    ) -> Decimal:
        """
        Expected return over expected_hold_hours, as a fraction of leg notional.
        net = hourly_spread * hours - 2 * (taker_long + taker_short).
        RiskManager subsequently deducts estimated round-trip slippage.
        """
        fee_long = await self._get_taker_fee(long_exchange, symbol)
        fee_short = await self._get_taker_fee(short_exchange, symbol)
        # round-trip = open + close для обеих ног
        total_fees = (fee_long + fee_short) * Decimal("2")
        return (
            spread * (horizon if horizon is not None else self._cfg.expected_hold_hours)
            - total_fees
        )

    async def _is_spike(self, exchange_name: str, symbol: str, current_rate_per_h: Decimal) -> bool:
        """
        Проверяет, не является ли текущая ставка аномальным спайком.
        Спайк = текущая ставка > SPIKE_MULTIPLIER × средняя за lookback период.

        Если ставка была аномально высокой недавно — скорее всего схлопнется
        до следующей выплаты, делая вход невыгодным.
        """
        if self._cfg.funding_spike_lookback_min <= 0:
            return False

        cache_key = (exchange_name, symbol)
        if cache_key in self._spike_cache:
            return self._spike_cache[cache_key]

        lookback_h = self._cfg.funding_spike_lookback_min / 60
        adapter = next((a for a in self._adapters if a.name == exchange_name), None)
        if adapter is None:
            return False

        # Проверяем только если адаптер поддерживает get_funding_history
        if not hasattr(adapter, "get_funding_history"):
            return False

        try:
            history = await adapter.get_funding_history(symbol, int(lookback_h) + 1)
        except Exception:
            return True  # Missing history must not authorize an entry.

        if len(history) < 2:
            return False

        rates = [abs(r.rate / Decimal(r.interval_hours)) for r in history if r.interval_hours > 0]
        if not rates:
            return False
        avg = sum(rates) / Decimal(len(rates))
        if avg == 0:
            return False

        is_spike = abs(current_rate_per_h) > avg * _SPIKE_MULTIPLIER
        if is_spike:
            log.warning(
                "scanner.spike_detected",
                exchange=exchange_name,
                symbol=symbol,
                current_rate=str(current_rate_per_h),
                avg_rate=str(avg),
            )
        self._spike_cache[cache_key] = is_spike
        return is_spike

    async def _get_taker_fee(self, exchange_name: str, symbol: str) -> Decimal:
        for adapter in self._adapters:
            if adapter.name == exchange_name:
                try:
                    fee = await adapter.get_fee_rate(symbol)
                    if fee.taker.is_finite() and fee.taker >= 0:
                        return fee.taker
                    break
                except Exception:
                    break
        # Дефолт если не удалось получить
        return Decimal("Infinity")

    # ── Утилиты ───────────────────────────────────────────────────────────────

    def _get_common_symbols(self, rates_by_exchange: dict[str, dict[str, FundingRate]]) -> set[str]:
        """Пересечение символов всех бирж с учётом whitelist/blacklist."""
        symbol_sets = [set(rates.keys()) for rates in rates_by_exchange.values()]
        common = symbol_sets[0].intersection(*symbol_sets[1:])

        whitelist = self._cfg.symbol_whitelist
        if whitelist:
            common &= set(whitelist)

        common -= self._cfg.symbol_blacklist_set
        return common

    def _print_table(self, spreads: list[FundingSpread]) -> None:
        """Выводит таблицу спредов в лог. В dry_run это основной output."""
        now = datetime.now(UTC).strftime("%H:%M:%S")
        reasons = Counter(
            s.entry_reason
            if not s.entry_ready
            else "candidate_before_orderbook"
            if s.estimated_net_pnl >= self._cfg.min_net_pnl_after_fees
            else "forecast_below_costs"
            for s in spreads
        )
        log.info(
            "scanner.scan_complete",
            timestamp=now,
            total_spreads=len(spreads),
            entry_filters=dict(reasons),
        )

        if not spreads:
            log.info("scanner.no_opportunities")
            return

        # Таблица в консоль (только в dry_run для наглядности)
        header = (
            f"{'Symbol':<10} {'Long':<18} {'Short':<18} {'Spread/h':>12} {'NetPnL':>11} {'Ann%':>8}"
        )
        separator = "-" * len(header)
        print(f"\n[{now}] Funding Rate Spreads")
        print(separator)
        print(header)
        print(separator)

        for s in spreads:
            ann_pct = float(s.spread) * 8760 * 100  # spread is per hour
            marker = (
                " *"
                if s.entry_ready and s.estimated_net_pnl >= self._cfg.min_net_pnl_after_fees
                else ""
            )
            status = s.entry_reason or (
                "candidate_before_orderbook" if marker else "forecast_below_costs"
            )
            print(
                f"{s.symbol:<10} {s.long_exchange:<18} {s.short_exchange:<18} "
                f"{float(s.spread):>12.8f} {float(s.estimated_net_pnl):>11.7f} "
                f"{ann_pct:>7.1f}%{marker} {status} "
                f"forecast_h={s.forecast_hours} history_h={s.history_hours:.2f}"
            )
        print(separator)
        print("* = прогноз после комиссий; стаканы и запас окупаемости проверяются перед входом\n")
