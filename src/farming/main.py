"""Standalone entrypoint for the multi-venue paper farming strategy."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import structlog

from src.adapters.arcus import ArcusAdapter
from src.adapters.entropy import EntropyAdapter
from src.adapters.lighter_robinhood import LighterRobinhoodAdapter
from src.adapters.nado import NadoAdapter
from src.adapters.normalized import NormalizedAdapter, load_catalog
from src.adapters.polymarket_perps import PolymarketPerpsAdapter
from src.adapters.txflow import TxFlowAdapter
from src.config import get_settings
from src.farming.engine import FarmingEngine
from src.models import BotMode


@dataclass
class FarmingInstanceContext:
    instance_index: int
    nado_adapter: NadoAdapter | None
    entropy_adapter: EntropyAdapter | None
    polymarket_perps_adapter: PolymarketPerpsAdapter | None
    txflow_adapter: TxFlowAdapter | None
    lighter_robinhood_adapter: LighterRobinhoodAdapter | None
    arcus_adapter: ArcusAdapter | None
    monitored_venues: frozenset[str]
    engine: FarmingEngine
    task: asyncio.Task | None = None

    @classmethod
    async def create(
        cls,
        settings,
        index,
        nado_adapter,
        entropy_adapter,
        polymarket_perps_adapter,
        txflow_adapter,
        lighter_robinhood_adapter=None,
        arcus_adapter=None,
        monitored_venues=frozenset(),
    ):
        owned_adapters = [
            adapter
            for adapter in (
                nado_adapter,
                entropy_adapter,
                polymarket_perps_adapter,
                txflow_adapter,
            )
            if adapter is not None
        ]
        if lighter_robinhood_adapter is not None:
            owned_adapters.append(lighter_robinhood_adapter)
        if arcus_adapter is not None:
            owned_adapters.append(arcus_adapter)
        connected = []
        try:
            for adapter in owned_adapters:
                await adapter.connect()
                connected.append(adapter)
            catalog = load_catalog()
            normalized = [
                NormalizedAdapter(adapter, await adapter.get_symbols(), catalog)
                for adapter in owned_adapters
            ]
            engine = FarmingEngine(
                {adapter.name: adapter for adapter in normalized},
                settings,
                instance_index=index,
            )
            await engine.connect()
        except Exception:
            for adapter in reversed(connected):
                await adapter.disconnect()
            raise
        return cls(
            index,
            nado_adapter,
            entropy_adapter,
            polymarket_perps_adapter,
            txflow_adapter,
            lighter_robinhood_adapter,
            arcus_adapter,
            frozenset(monitored_venues),
            engine,
        )

    def start(self) -> None:
        with structlog.contextvars.bound_contextvars(instance_index=self.instance_index):
            self.task = asyncio.create_task(
                self.engine.run(), name=f"farming_{self.instance_index}"
            )

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.engine.disconnect()
        adapters = (
            self.lighter_robinhood_adapter,
            self.arcus_adapter,
            self.txflow_adapter,
            self.polymarket_perps_adapter,
            self.entropy_adapter,
            self.nado_adapter,
        )
        await asyncio.gather(
            *(adapter.disconnect() for adapter in adapters if adapter is not None),
            return_exceptions=True,
        )


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
    entropy_accounts = settings.resolved_entropy_accounts()
    nado_accounts = settings.resolved_nado_accounts()
    polymarket_accounts = settings.resolved_polymarket_perps_accounts()
    txflow_accounts = settings.resolved_txflow_accounts()
    lighter_accounts = settings.resolved_lighter_robinhood_accounts()
    arcus_accounts = settings.resolved_arcus_accounts()
    if settings.farming_instance_venues:
        account_counts = {"venue_profile_count": len(settings.farming_instance_venues)}
    elif settings.farming_mode == BotMode.LIVE:
        account_counts = {
            "entropy_count": len(entropy_accounts),
            "polymarket_perps_count": len(polymarket_accounts),
        }
    else:
        primary_count = min(len(entropy_accounts), len(nado_accounts))
        account_counts = {
            "entropy_count": len(entropy_accounts),
            "nado_count": len(nado_accounts),
            "polymarket_perps_count": len(polymarket_accounts) or primary_count,
            "txflow_count": len(txflow_accounts) or primary_count,
        }
        if settings.lighter_robinhood_enabled:
            account_counts["lighter_robinhood_count"] = len(lighter_accounts) or primary_count
        if settings.arcus_enabled:
            account_counts["arcus_count"] = len(arcus_accounts) or primary_count
    effective_count = min(account_counts.values())
    log = structlog.get_logger(__name__)
    if len(set(account_counts.values())) != 1 and effective_count > 0:
        log.warning("wallet_count_mismatch", **account_counts, effective_count=effective_count)
    if effective_count == 0:
        raise ValueError(
            "No complete farming instances configured"
        )

    contexts = []
    try:
        for index in range(effective_count):
            monitored = settings.farming_venues_for_instance(index)
            if settings.farming_mode == BotMode.LIVE:
                monitored &= {"entropy", "polymarket_perps"}
            if not settings.entropy_enabled:
                monitored.discard("entropy")
            if not settings.polymarket_perps_enabled:
                monitored.discard("polymarket_perps")
            if not settings.txflow_enabled:
                monitored.discard("txflow")
            if not settings.lighter_robinhood_enabled:
                monitored.discard("lighter_robinhood")
            if not settings.arcus_enabled:
                monitored.discard("arcus")
            if len(monitored) < 2:
                log.error(
                    "farming.instance_skipped",
                    instance_index=index,
                    error="fewer than two monitored venues remain enabled",
                )
                continue
            account_lists = {
                "entropy": entropy_accounts,
                "nado": nado_accounts,
                "polymarket_perps": polymarket_accounts,
                "txflow": txflow_accounts,
                "lighter_robinhood": lighter_accounts,
                "arcus": arcus_accounts,
            }
            missing_accounts = [
                venue
                for venue in monitored
                if (
                    venue in {"entropy", "nado"}
                    or bool(account_lists[venue])
                )
                and index >= len(account_lists[venue])
            ]
            if missing_accounts:
                log.error(
                    "farming.instance_skipped",
                    instance_index=index,
                    error="missing account for selected venue",
                    venues=sorted(missing_accounts),
                )
                continue
            nado_adapter = (
                None
                if "nado" not in monitored
                else NadoAdapter(
                    settings,
                    instance_index=index,
                    transport_url=nado_accounts[index].transport_url,
                )
            )
            entropy_adapter = (
                EntropyAdapter(
                    settings,
                    instance_index=index,
                    transport_url=entropy_accounts[index].transport_url,
                )
                if "entropy" in monitored
                else None
            )
            polymarket_adapter = (
                PolymarketPerpsAdapter(
                    settings,
                    instance_index=index,
                    transport_url=(
                        polymarket_accounts[index].transport_url
                        if polymarket_accounts
                        else None
                    ),
                )
                if "polymarket_perps" in monitored
                else None
            )
            txflow_adapter = (
                None
                if "txflow" not in monitored
                else TxFlowAdapter(
                    settings,
                    instance_index=index,
                    transport_url=(
                        txflow_accounts[index].transport_url if txflow_accounts else None
                    ),
                )
            )
            lighter_adapter = (
                LighterRobinhoodAdapter(
                    settings,
                    instance_index=index,
                    transport_url=(
                        lighter_accounts[index].transport_url if lighter_accounts else None
                    ),
                )
                if "lighter_robinhood" in monitored
                else None
            )
            arcus_adapter = (
                ArcusAdapter(
                    settings,
                    instance_index=index,
                    transport_url=(arcus_accounts[index].transport_url if arcus_accounts else None),
                )
                if "arcus" in monitored
                else None
            )
            try:
                with structlog.contextvars.bound_contextvars(instance_index=index):
                    context = await FarmingInstanceContext.create(
                        settings,
                        index,
                        nado_adapter,
                        entropy_adapter,
                        polymarket_adapter,
                        txflow_adapter,
                        lighter_adapter,
                        arcus_adapter,
                        monitored,
                    )
            except Exception as exc:
                log.error("farming.instance_skipped", instance_index=index, error=str(exc))
                continue
            contexts.append(context)
            log.info(
                "farming.instance_venues",
                instance_index=index,
                venues=sorted(context.monitored_venues),
            )
            for venue, adapter in context.engine.adapters.items():
                log.info(
                    "farming.venue_ready",
                    instance_index=index,
                    venue=venue,
                    markets=len(await adapter.get_symbols()),
                )
            if not (
                settings.farming_mode == BotMode.LIVE
                and settings.farming_live_preflight_only
            ):
                context.start()
        if not contexts:
            raise RuntimeError("No farming instances connected")
        if settings.farming_mode == BotMode.LIVE and settings.farming_live_preflight_only:
            log.warning(
                "farming.live_preflight_complete",
                instances=len(contexts),
                orders_enabled=False,
            )
            return
        log.info(
            f"farming.{settings.farming_mode.value}_running",
            instances=len(contexts),
            margin_per_leg=str(settings.farming_notional_usd),
            leverage=str(settings.farming_leverage),
            notional_per_leg=str(
                settings.farming_notional_usd * settings.farming_leverage
            ),
        )
        await asyncio.gather(*(context.task for context in contexts if context.task))
    finally:
        await asyncio.gather(
            *(context.close() for context in reversed(contexts)),
            return_exceptions=True,
        )


def main() -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
