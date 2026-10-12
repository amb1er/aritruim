"""Абстрактный интерфейс для всех биржевых адаптеров."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from src.models import (
    Balance,
    FeeRate,
    FundingRate,
    Orderbook,
    OrderRequest,
    OrderResult,
    Position,
)


class ExchangeAdapter(ABC):
    """
    Базовый класс для биржевых адаптеров.
    Каждая биржа реализует все абстрактные методы.
    Все методы async; денежные величины — Decimal.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальное имя биржи, например 'hyperliquid' или 'nado'."""

    # ── Жизненный цикл ────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Инициализация соединений, аутентификация."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Корректное закрытие всех соединений."""

    # ── Рыночные данные ───────────────────────────────────────────────────────

    @abstractmethod
    async def get_symbols(self) -> list[str]:
        """Список всех доступных перп-символов на этой бирже."""

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Текущий funding rate для символа."""

    @abstractmethod
    async def get_funding_rates(self) -> list[FundingRate]:
        """Funding rates для всех символов (один запрос если API позволяет)."""

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        """Стакан заявок до depth уровней с каждой стороны."""

    @abstractmethod
    async def get_fee_rate(self, symbol: str) -> FeeRate:
        """Комиссии maker/taker для символа."""

    # ── Аккаунт ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_balance(self) -> Balance:
        """Баланс аккаунта в USD."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Все открытые позиции."""

    # ── Торговля ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Разместить ордер. В dry_run/paper режиме — симуляция."""

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Отменить ордер. Возвращает True если успешно."""

    @abstractmethod
    async def close_position(self, symbol: str, side: str) -> OrderResult:
        """Закрыть позицию по рынку (reduce-only market order)."""

    # ── WebSocket подписки ────────────────────────────────────────────────────

    @abstractmethod
    async def subscribe_funding(
        self,
        callback: Callable[[FundingRate], None],
    ) -> None:
        """
        Подписаться на обновления funding rate.
        callback вызывается при каждом обновлении.
        """

    # ── Утилиты ───────────────────────────────────────────────────────────────

    async def get_funding_price(self, symbol: str):
        """Reference price for paper funding; venues may override with oracle/index."""
        return await self.get_mark_price(symbol)

    async def __aenter__(self) -> "ExchangeAdapter":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()
