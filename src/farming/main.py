"""Standalone entrypoint for the four-venue paper farming strategy."""

import asyncio
import logging
from contextlib import AsyncExitStack, suppress
from logging.handlers import RotatingFileHandler

import structlog

from src.adapters.entropy import EntropyAdapter
from src.adapters.nado import NadoAdapter
from src.adapters.normalized import NormalizedAdapter, load_catalog
from src.adapters.polymarket_perps import PolymarketPerpsAdapter
from src.adapters.txflow import TxFlowAdapter
from src.config import get_settings
from src.farming.engine import FarmingEngine


def setup_logging(settings) -> None:
    settings.farming_log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("farming")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler, renderer in (
        (logging.StreamHandler(), structlog.dev.ConsoleRenderer()),
        (RotatingFileHandler(settings.farming_log_file, maxBytes=10_000_000,
                             backupCount=5, encoding="utf-8"), structlog.processors.JSONRenderer()),
    ):
        handler.setFormatter(structlog.stdlib.ProcessorFormatter(
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer]))
        logger.addHandler(handler)
    structlog.configure(
        processors=[structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso"),
                    structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=lambda *args: logger,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


async def run() -> None:
    settings = get_settings()
    setup_logging(settings)
    if not settings.farming_enabled:
        raise RuntimeError("FARMING_ENABLED is false")
    raw = [NadoAdapter(settings), EntropyAdapter(settings),
           PolymarketPerpsAdapter(settings), TxFlowAdapter(settings)]
    async with AsyncExitStack() as stack:
        for adapter in raw:
            await adapter.connect()
            stack.push_async_callback(adapter.disconnect)
        catalog = load_catalog()
        normalized = [NormalizedAdapter(a, await a.get_symbols(), catalog) for a in raw]
        adapters = {a.name: a for a in normalized}
        log = structlog.get_logger(__name__)
        for adapter in normalized:
            log.info("farming.venue_ready", venue=adapter.name,
                     markets=len(await adapter.get_symbols()))
        engine = FarmingEngine(adapters, settings)
        await engine.connect()
        stack.push_async_callback(engine.disconnect)
        log.info("farming.paper_running", venues=list(adapters),
                 notional=str(settings.farming_notional_usd))
        await engine.run()


def main() -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
