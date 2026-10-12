"""
HTTP Health Check сервер.

GET /health  → 200 JSON со статусом бота
GET /metrics → 200 JSON с метриками (uptime, open_pairs, pnl, errors)

Используется systemd/load balancer для проверки работоспособности.
Также удобно для быстрого просмотра состояния через curl.

Пример:
  curl http://localhost:8080/health
  curl http://localhost:8080/metrics
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import structlog
from aiohttp import web

log = structlog.get_logger(__name__)


class HealthServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._host = host
        self._port = port
        self._start_time = time.monotonic()
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        # Метрики — обновляются внешними компонентами
        self.open_pairs: int = 0
        self.total_funding_received: float = 0.0
        self.total_fees_paid: float = 0.0
        self.net_pnl: float = 0.0
        self.error_count: int = 0
        self.circuit_breaker_open: bool = False
        self.bot_mode: str = "unknown"
        self.last_scan_ts: str = ""
        self.data_stale: bool = False

        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/metrics", self._handle_metrics)

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        log.info("health_server.started", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        log.info("health_server.stopped")

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        """
        Возвращает 200 если бот работает нормально, 503 если circuit breaker открыт.
        """
        uptime_sec = int(time.monotonic() - self._start_time)
        status = "degraded" if self.circuit_breaker_open or self.data_stale else "ok"
        http_status = 503 if status == "degraded" else 200

        return web.json_response(
            {
                "status": status,
                "uptime_sec": uptime_sec,
                "bot_mode": self.bot_mode,
                "open_pairs": self.open_pairs,
                "circuit_breaker_open": self.circuit_breaker_open,
                "data_stale": self.data_stale,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            status=http_status,
        )

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Расширенные метрики для мониторинга."""
        uptime_sec = int(time.monotonic() - self._start_time)
        return web.json_response(
            {
                "uptime_sec": uptime_sec,
                "bot_mode": self.bot_mode,
                "open_pairs": self.open_pairs,
                "net_pnl": round(self.net_pnl, 8),
                "funding_received": round(self.total_funding_received, 8),
                "fees_paid": round(self.total_fees_paid, 8),
                "error_count": self.error_count,
                "circuit_breaker_open": self.circuit_breaker_open,
                "data_stale": self.data_stale,
                "last_scan_ts": self.last_scan_ts,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def update_from_positions(self, positions: list) -> None:
        """Обновляет метрики из списка открытых позиций."""
        self.open_pairs = len(positions)
        self.net_pnl = sum(float(p.net_pnl) for p in positions)
        self.total_funding_received = sum(float(p.funding_received) for p in positions)
        self.total_fees_paid = sum(float(p.fees_paid) for p in positions)
