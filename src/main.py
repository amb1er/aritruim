"""Async application lifecycle, task supervision and cross-platform shutdown."""

import asyncio
import logging
import signal
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import structlog

from src.adapters.arcus import ArcusAdapter
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


@dataclass
class InstanceContext:
    instance_index: int
    hyperliquid_adapter: object
    nado_adapter: object
    shared_adapters: list
    adapters: dict
    db: Database
    risk: RiskManager
    scanner: FundingScanner
    executor: Executor
    tracker: PnlTracker
    tasks: list[asyncio.Task] = field(default_factory=list)

    @classmethod
    async def create(
        cls,
        settings,
        index,
        hl_adapter,
        nado_adapter,
        shared_adapters,
        notifier,
    ):
        instance_adapters = [hl_adapter, nado_adapter]
        connected = []
        db = None
        try:
            for adapter in instance_adapters:
                await adapter.connect()
                connected.append(adapter)
                await adapter.get_symbols()
            catalog = load_catalog()
            adapters_list = [
                NormalizedAdapter(adapter, await adapter.get_symbols(), catalog)
                for adapter in [*instance_adapters, *shared_adapters]
            ]
            adapters = {adapter.name: adapter for adapter in adapters_list}
            if index == 0:
                adapters.setdefault("hyperliquid", adapters[hl_adapter.name])
                adapters.setdefault("nado", adapters[nado_adapter.name])

            # Instance 0 keeps the legacy database so existing positions aren't lost.
            db_path = (
                settings.sqlite_path
                if index == 0
                else settings.sqlite_path.parent / f"arb.instance_{index}.db"
            )
            db = Database(db_path)
            await db.connect()
            risk = RiskManager(settings)
            risk.set_callbacks(
                on_circuit_open=notifier.on_circuit_breaker,
                on_delta_warning=notifier.on_delta_warning,
            )
            scanner = FundingScanner(adapters_list, settings, db)
            executor = Executor(adapters, risk, db, notifier, settings)
            await executor.restore()
            tracker = PnlTracker(adapters, risk, db, settings)
            tracker.set_executor(executor)
            return cls(
                instance_index=index,
                hyperliquid_adapter=hl_adapter,
                nado_adapter=nado_adapter,
                shared_adapters=shared_adapters,
                adapters=adapters,
                db=db,
                risk=risk,
                scanner=scanner,
                executor=executor,
                tracker=tracker,
            )
        except Exception:
            if db is not None:
                await db.disconnect()
            for adapter in reversed(connected):
                await adapter.disconnect()
            raise

    def start(self, settings) -> None:
        with structlog.contextvars.bound_contextvars(instance_index=self.instance_index):
            self.tasks.append(
                asyncio.create_task(
                    self.scanner.run(), name=f"scanner_{self.instance_index}"
                )
            )
            if settings.bot_mode == BotMode.PAPER:
                self.tasks.extend(
                    [
                        asyncio.create_task(
                            self.executor.run(self.scanner.opportunities),
                            name=f"executor_{self.instance_index}",
                        ),
                        asyncio.create_task(
                            self.tracker.run(), name=f"pnl_tracker_{self.instance_index}"
                        ),
                    ]
                )

    async def stop(self, settings, notifier) -> None:
        with structlog.contextvars.bound_contextvars(instance_index=self.instance_index):
            await self.scanner.stop()
            await self.tracker.stop()
            await self.executor.stop()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if settings.shutdown_mode == ShutdownMode.CLOSE_POSITIONS:
                before = len(self.executor.open_positions)
                try:
                    await asyncio.wait_for(
                        self.executor.close_all("shutdown"), settings.shutdown_timeout_sec
                    )
                except TimeoutError:
                    structlog.get_logger(__name__).error(
                        "bot.shutdown_timeout",
                        remaining=len(self.executor.open_positions),
                    )
                finally:
                    await notifier.on_shutdown(before - len(self.executor.open_positions))
            await self.db.disconnect()
            await self.nado_adapter.disconnect()
            await self.hyperliquid_adapter.disconnect()


async def _daily_report_loop(notifier, contexts, health, start_time):
    while True:
        now = utc_now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((midnight - now).total_seconds())
        try:
            report_date = (utc_now() - timedelta(days=1)).date()
            mode = BotMode(health.bot_mode)
            daily_rows = [
                await context.db.get_daily_pnl(report_date.isoformat(), mode=mode)
                for context in contexts
            ]
            daily = {
                key: sum((row[key] for row in daily_rows), start=0)
                for key in ("net_pnl", "funding_received", "fees_paid")
            }
            closed_today = 0
            for context in contexts:
                closed_today += sum(
                    1
                    for p in await context.db.get_all_positions()
                    if p.closed_at and p.closed_at.date() == report_date and p.mode == mode
                )
            await notifier.on_daily_report(
                open_pairs=sum(len(context.executor.open_positions) for context in contexts),
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
        hyperliquid_configs = settings.resolved_hyperliquid_accounts()
        nado_configs = settings.resolved_nado_accounts()
        effective_count = min(len(hyperliquid_configs), len(nado_configs))
        if len(hyperliquid_configs) != len(nado_configs) and effective_count > 0:
            log.warning(
                "wallet_count_mismatch",
                hl_count=len(hyperliquid_configs),
                nado_count=len(nado_configs),
                effective_count=effective_count,
            )
        if effective_count == 0:
            raise ValueError("No paired Hyperliquid and Nado instances configured")

        shared_adapters = []
        if settings.aster_enabled:
            shared_adapters.append(AsterAdapter(settings))
        if settings.arcus_enabled:
            shared_adapters.append(ArcusAdapter(settings))
        if settings.hyperliquid_spot_enabled:
            shared_adapters.append(HyperliquidSpotAdapter(settings))
        if settings.entropy_enabled:
            shared_adapters.append(EntropyAdapter(settings))
        if settings.lighter_robinhood_enabled:
            shared_adapters.append(LighterRobinhoodAdapter(settings))
        if settings.extended_enabled:
            shared_adapters.append(ExtendedAdapter(settings))
        if settings.hibachi_enabled:
            shared_adapters.append(HibachiAdapter(settings))
        if settings.polymarket_perps_enabled:
            shared_adapters.append(PolymarketPerpsAdapter(settings))
        for adapter in shared_adapters:
            await adapter.connect()
            resources.push_async_callback(adapter.disconnect)

        notifier = Notifier(
            settings.telegram_token, settings.telegram_chat_id, settings.telegram_level
        )
        resources.push_async_callback(notifier.disconnect)
        await notifier.connect()

        contexts = []
        for index in range(effective_count):
            hl_adapter = HyperliquidAdapter(
                settings,
                instance_index=index,
                transport_url=hyperliquid_configs[index].transport_url,
            )
            nado_adapter = NadoAdapter(
                settings,
                instance_index=index,
                transport_url=nado_configs[index].transport_url,
            )
            try:
                with structlog.contextvars.bound_contextvars(instance_index=index):
                    context = await InstanceContext.create(
                        settings,
                        index,
                        hl_adapter,
                        nado_adapter,
                        shared_adapters,
                        notifier,
                    )
            except Exception as exc:
                log.error("instance.skipped", instance_index=index, error=str(exc))
                continue
            contexts.append(context)
        if not contexts:
            raise RuntimeError("No strategy instances connected")

        health = HealthServer(host="0.0.0.0", port=8080)
        health.bot_mode = settings.bot_mode.value
        resources.push_async_callback(health.stop)
        await health.start()
        shutdown_event = asyncio.Event()
        resources.callback(_install_signal_handlers(asyncio.get_running_loop(), shutdown_event))
        await notifier.on_startup(settings.bot_mode.value)
        for context in contexts:
            context.start(settings)
        tasks = []
        if settings.bot_mode == BotMode.PAPER:
            tasks.append(
                asyncio.create_task(
                    _daily_report_loop(notifier, contexts, health, start_time),
                    name="daily_report",
                )
            )

        async def update_health():
            while True:
                positions = [
                    position
                    for context in contexts
                    for position in context.executor.open_positions.values()
                ]
                health.update_from_positions(positions)
                health.circuit_breaker_open = any(
                    context.risk.circuit_open for context in contexts
                )
                scan_times = [
                    context.scanner.last_scan_at
                    for context in contexts
                    if context.scanner.last_scan_at is not None
                ]
                health.last_scan_ts = max(scan_times).isoformat() if scan_times else ""
                health.data_stale = len(scan_times) != len(contexts) or any(
                    (utc_now() - timestamp).total_seconds()
                    > max(60, settings.scan_interval_sec * 3)
                    for timestamp in scan_times
                )
                await asyncio.sleep(10)

        tasks.append(asyncio.create_task(update_health(), name="health_updater"))
        waiter = asyncio.create_task(shutdown_event.wait(), name="shutdown_waiter")
        instance_tasks = [task for context in contexts for task in context.tasks]
        log.info("bot.running", mode=settings.bot_mode.value, instances=len(contexts))
        try:
            done, _ = await asyncio.wait(
                [waiter, *tasks, *instance_tasks], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not waiter:
                    if not task.cancelled() and task.exception():
                        raise task.exception()
                    raise RuntimeError(f"Background task stopped: {task.get_name()}")
        finally:
            for task in [waiter, *tasks]:
                task.cancel()
            await asyncio.gather(waiter, *tasks, return_exceptions=True)
            for context in contexts:
                await context.stop(settings, notifier)
            log.info(
                "bot.stopped",
                remaining=sum(
                    len(context.executor.open_positions) for context in contexts
                ),
            )


def main():
    with suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
