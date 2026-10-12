"""
Telegram уведомления.

Уровни (TELEGRAM_LEVEL в .env):
  all          — все события
  trades_only  — только открытие/закрытие позиций и ошибки
  errors_only  — только ошибки и circuit breaker

Бот молча игнорирует ошибки Telegram (не должен ронять основной процесс).
"""

from __future__ import annotations

import ssl
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp
import certifi
import structlog

from src.models import ArbitragePosition, FundingSpread, TelegramLevel

log = structlog.get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_uptime(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


class Notifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        level: TelegramLevel,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._level = level
        self._enabled = bool(token and chat_id)
        self._session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        if self._enabled:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(
                    resolver=aiohttp.ThreadedResolver(), ssl=ssl_context
                ),
            )
            log.info("notifier.connected", level=self._level.value)

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ── События ───────────────────────────────────────────────────────────────

    async def on_position_opened(self, position: ArbitragePosition, spread: FundingSpread) -> None:
        if self._level == TelegramLevel.ERRORS_ONLY:
            return
        # Scanner spreads are normalized per hour.
        ann_pct = float(spread.spread) * 8760 * 100
        text = (
            f"📈 *Позиция открыта* #{position.id}\n"
            f"Символ: `{position.symbol}`\n"
            f"LONG: `{position.long_exchange}` / SHORT: `{position.short_exchange}`\n"
            f"Размер ноги: `${float(position.notional_usd):,.0f}` × {float(position.leverage)}x\n"
            f"Спред/час: `{float(spread.spread):.6f}` (~{ann_pct:.1f}% год.)\n"
            f"Ожидаемый net PnL: `{float(spread.estimated_net_pnl):.6f}`"
        )
        await self._send(text)

    async def on_position_closed(self, position: ArbitragePosition, reason: str) -> None:
        if self._level == TelegramLevel.ERRORS_ONLY:
            return
        hold = ""
        if position.closed_at and position.opened_at:
            h = (position.closed_at - position.opened_at).total_seconds() / 3600
            hold = f"\nДержали: `{h:.1f}h`"

        pnl_sign = "+" if position.net_pnl >= 0 else ""
        text = (
            f"📉 *Позиция закрыта* #{position.id}\n"
            f"Символ: `{position.symbol}`\n"
            f"Причина: `{reason}`{hold}\n"
            f"Funding: `+{float(position.funding_received):.6f}`\n"
            f"Fees: `-{float(position.fees_paid):.6f}`\n"
            f"Net PnL: `{pnl_sign}{float(position.net_pnl):.6f}`"
        )
        await self._send(text)

    async def on_error(self, message: str) -> None:
        text = f"🚨 *Ошибка*\n`{message}`"
        await self._send(text)

    async def on_circuit_breaker(self, failures: int, delay_sec: int) -> None:
        text = f"⚡ *Circuit Breaker*\nПодряд ошибок: `{failures}`\nПауза: `{delay_sec}s`"
        await self._send(text)

    async def on_daily_report(
        self,
        open_pairs: int,
        net_pnl: Decimal,
        funding_received: Decimal,
        fees_paid: Decimal,
        closed_today: int = 0,
        uptime_sec: int = 0,
    ) -> None:
        if self._level == TelegramLevel.ERRORS_ONLY:
            return
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        pnl_sign = "+" if net_pnl >= 0 else ""
        uptime_str = _fmt_uptime(uptime_sec)
        text = (
            f"📊 *Дневной отчёт* {date}\n"
            f"Uptime: `{uptime_str}`\n"
            f"Открытых пар: `{open_pairs}`\n"
            f"Закрыто сегодня: `{closed_today}`\n"
            f"Funding получено: `+{float(funding_received):.6f}`\n"
            f"Fees уплачено: `-{float(fees_paid):.6f}`\n"
            f"Net PnL: `{pnl_sign}{float(net_pnl):.6f}`"
        )
        await self._send(text)

    async def on_delta_warning(
        self,
        position_id: str,
        symbol: str,
        delta_pct: Decimal,
        threshold_pct: Decimal,
    ) -> None:
        """Алерт когда delta приближается к stop-loss (> 70% от лимита)."""
        text = (
            f"⚠️ *Delta Warning* #{position_id}\n"
            f"Символ: `{symbol}`\n"
            f"Delta: `{float(delta_pct):.2%}` "
            f"(лимит: `{float(threshold_pct):.2%}`)\n"
            f"Позиция перестаёт быть delta-neutral"
        )
        await self._send(text)

    async def on_startup(self, mode: str) -> None:
        if self._level == TelegramLevel.ERRORS_ONLY:
            return
        text = f"🟢 *Бот запущен*\nРежим: `{mode}`"
        await self._send(text)

    async def on_shutdown(self, open_pairs: int) -> None:
        if self._level == TelegramLevel.ERRORS_ONLY:
            return
        text = f"🔴 *Бот остановлен*\nЗакрыто позиций при shutdown: `{open_pairs}`"
        await self._send(text)

    # ── Отправка ──────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:

        if not self._enabled or not self._session:
            log.debug("notifier.skip", text=text[:60])
            return
        url = _TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("notifier.send_failed", status=resp.status, body=body[:200])
        except Exception as e:
            # Никогда не ронять основной процесс из-за Telegram
            log.warning("notifier.send_error", error=str(e))
