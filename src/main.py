"""Async application lifecycle, task supervision and cross-platform shutdown."""

import asyncio
import logging
import signal
import time
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import structlog

from src.adapters.aster import AsterAdapter
from src.adapters.entropy import EntropyAdapter
from src.adapters.extended import ExtendedAdapter
from src.adapters.hibachi import HibachiAdapter
from src.adapters.hyperliquid import HyperliquidAdapter
from src.adapters.hyperliquid_spot import HyperliquidSpotAdapter
from src.adapters.lighter_robinhood import LighterRobinhoodAdapter
from src.adapters.nado import LIVE_UNAVAILABLE, NadoAdapter
from src.adapters.normalized import NormalizedAdapter, load_catalog
from src.adapters.polymarket_perps import PolymarketPerpsAdapter
from src.config import get_settings
from src.core.executor import Executor
from src.core.pnl_tracker import PnlTracker
from src.core.risk import RiskManager
from src.core.scanner import FundingScanner
from src.db import Database
from src.health import HealthServer
from src.models import BotMode, ShutdownMode, utc_now
from src.notifier import Notifier


def _setup_logging(settings):
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("arbitrage")
    logger.setLevel(settings.log_level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler()
    file = RotatingFileHandler(
        settings.log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    for handler, renderer in (
        (console, structlog.processors.JSONRenderer()
         if settings.log_format.value == "json" else structlog.dev.ConsoleRenderer()),
        (file, structlog.processors.JSONRenderer()),
    ):
        handler.setFormatter(structlog.stdlib.ProcessorFormatter(processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ]))
        logger.addHandler(handler)
    structlog.configure(
        processors=[*processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=lambda *args: logger,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level.upper())
        ),
    )


def _install_signal_handlers(loop, shutdown_event):
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            signal.signal(sig, lambda signum, frame: loop.call_soon_threadsafe(shutdown_event.set))

    def restore():
        for sig, handler in previous.items():
            with suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
            signal.signal(sig, handler)

    return restore


async def _daily_report_loop(notifier, executor, db, health, start_time):
    while True:
        now = utc_now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((midnight - now).total_seconds())
        try:
            report_date = (utc_now() - timedelta(days=1)).date()
            mode = BotMode(health.bot_mode)
            daily = await db.get_daily_pnl(report_date.isoformat(), mode=mode)
            closed_today = sum(
                1
                for p in await db.get_all_positions()
                if p.closed_at and p.closed_at.date() == report_date and p.mode == mode
            )
            await notifier.on_daily_report(
                open_pairs=len(executor.open_positions),
                net_pnl=daily["net_pnl"],
                funding_received=daily["funding_received"],
                fees_paid=daily["fees_paid"],
                closed_today=closed_today,
                uptime_sec=int(time.monotonic() - start_time),
            )
        except Exception:
            structlog.get_logger(__name__).exception("daily_report.failed")


async def run():
    settings = get_settings()
    if settings.bot_mode == BotMode.LIVE:
        raise RuntimeError(LIVE_UNAVAILABLE)
    _setup_logging(settings)
    log = structlog.get_logger(__name__)
    start_time = time.monotonic()
    async with AsyncExitStack() as resources:
        adapters_list = [HyperliquidAdapter(settings), NadoAdapter(settings)]
        if settings.aster_enabled:
            adapters_list.append(AsterAdapter(settings))
        if settings.hyperliquid_spot_enabled:
            adapters_list.append(HyperliquidSpotAdapter(settings))
        if settings.entropy_enabled:
            adapters_list.append(EntropyAdapter(settings))
        if settings.lighter_robinhood_enabled:
            adapters_list.append(LighterRobinhoodAdapter(settings))
        if settings.extended_enabled:
            adapters_list.append(ExtendedAdapter(settings))
        if settings.hibachi_enabled:
            adapters_list.append(HibachiAdapter(settings))
        if settings.polymarket_perps_enabled:
            adapters_list.append(PolymarketPerpsAdapter(settings))
        for adapter in adapters_list:
            resources.push_async_callback(adapter.disconnect)
            await adapter.connect()
        catalog = load_catalog()
        adapters_list = [
            NormalizedAdapter(adapter, await adapter.get_symbols(), catalog)
            for adapter in adapters_list
        ]
        for adapter in adapters_list:
            log.info(
                "instruments.mapped",
                exchange=adapter.name,
                markets=len(await adapter.get_symbols()),
                aliases=sum(native != asset for native, (asset, _) in adapter._mapping.items()),
                ambiguous=sorted(adapter._ambiguous),
            )
        adapters = {adapter.name: adapter for adapter in adapters_list}
        db = Database(settings.sqlite_path)
        resources.push_async_callback(db.disconnect)
        await db.connect()
        notifier = Notifier(
            settings.telegram_token, settings.telegram_chat_id, settings.telegram_level
        )
        resources.push_async_callback(notifier.disconnect)
        await notifier.connect()
        risk = RiskManager(settings)
        risk.set_callbacks(
            on_circuit_open=notifier.on_circuit_breaker, on_delta_warning=notifier.on_delta_warning
        )
        scanner = FundingScanner(adapters_list, settings, db)
        executor = Executor(adapters, risk, db, notifier, settings)
        await executor.restore()
        tracker = PnlTracker(adapters, risk, db, settings)
        tracker.set_executor(executor)
        health = HealthServer(host="0.0.0.0", port=8080)
        health.bot_mode = settings.bot_mode.value
        resources.push_async_callback(health.stop)
        await health.start()
        shutdown_event = asyncio.Event()
        resources.callback(_install_signal_handlers(asyncio.get_running_loop(), shutdown_event))
        await notifier.on_startup(settings.bot_mode.value)
        tasks = [asyncio.create_task(scanner.run(), name="scanner")]
        if settings.bot_mode == BotMode.PAPER:
            tasks.extend(
                [
                    asyncio.create_task(executor.run(scanner.opportunities), name="executor"),
                    asyncio.create_task(tracker.run(), name="pnl_tracker"),
                    asyncio.create_task(
                        _daily_report_loop(notifier, executor, db, health, start_time),
                        name="daily_report",
                    ),
                ]
            )

        async def update_health():
            while True:
                health.update_from_positions(list(executor.open_positions.values()))
                health.circuit_breaker_open = risk.circuit_open
                health.last_scan_ts = (
                    scanner.last_scan_at.isoformat() if scanner.last_scan_at else ""
                )
                health.data_stale = scanner.last_scan_at is None or (
                    utc_now() - scanner.last_scan_at
                ).total_seconds() > max(60, settings.scan_interval_sec * 3)
                await asyncio.sleep(10)

        tasks.append(asyncio.create_task(update_health(), name="health_updater"))
        waiter = asyncio.create_task(shutdown_event.wait(), name="shutdown_waiter")
        log.info("bot.running", mode=settings.bot_mode.value)
        try:
            done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not waiter:
                    if not task.cancelled() and task.exception():
                        raise task.exception()
                    raise RuntimeError(f"Background task stopped: {task.get_name()}")
        finally:
            await scanner.stop()
            await tracker.stop()
            await executor.stop()
            for task in [waiter, *tasks]:
                task.cancel()
            await asyncio.gather(waiter, *tasks, return_exceptions=True)
            if settings.shutdown_mode == ShutdownMode.CLOSE_POSITIONS:
                before = len(executor.open_positions)
                try:
                    await asyncio.wait_for(
                        executor.close_all("shutdown"), settings.shutdown_timeout_sec
                    )
                except TimeoutError:
                    log.error("bot.shutdown_timeout", remaining=len(executor.open_positions))
                finally:
                    await notifier.on_shutdown(before - len(executor.open_positions))
            log.info("bot.stopped", remaining=len(executor.open_positions))


def main():
    with suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
