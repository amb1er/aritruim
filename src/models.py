"""Domain models — все денежные величины в Decimal."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Old database timestamps without an offset were written in UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BotMode(StrEnum):
    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"


class ShutdownMode(StrEnum):
    CLOSE_POSITIONS = "close_positions"
    SAVE_STATE = "save_state"


class TelegramLevel(StrEnum):
    ALL = "all"
    TRADES_ONLY = "trades_only"
    ERRORS_ONLY = "errors_only"


class OrderSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class PositionStatus(StrEnum):
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    ROLLBACK = "rollback"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


# ── Market data ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FundingRate:
    exchange: str
    symbol: str
    rate: Decimal  # Quoted over interval_hours (HL: 1h, Nado archive: 24h).
    interval_hours: int  # длина интервала в часах
    next_funding_ts: datetime | None
    timestamp: datetime = field(default_factory=utc_now)

    @property
    def annualized(self) -> Decimal:
        """Годовая ставка: rate × (8760 / interval_hours)."""
        if self.interval_hours <= 0:
            return Decimal("0")
        periods_per_year = Decimal("8760") / Decimal(self.interval_hours)
        return self.rate * periods_per_year


@dataclass(frozen=True)
class FundingSpread:
    symbol: str
    long_exchange: str  # биржа где открываем лонг (низкий funding)
    short_exchange: str  # биржа где открываем шорт (высокий funding)
    long_rate: Decimal
    short_rate: Decimal
    spread: Decimal  # short_rate - long_rate (всегда > 0 при выгодном спреде)
    estimated_net_pnl: Decimal  # Return after fees; risk checks deduct slippage.
    timestamp: datetime = field(default_factory=utc_now)
    forecast_spread: Decimal | None = None
    forecast_hours: Decimal | None = None
    history_hours: Decimal = Decimal("0")
    entry_ready: bool = True
    entry_reason: str = ""


@dataclass(frozen=True)
class OrderbookLevel:
    price: Decimal
    size: Decimal


@dataclass
class Orderbook:
    exchange: str
    symbol: str
    bids: list[OrderbookLevel]  # убывающий порядок
    asks: list[OrderbookLevel]  # возрастающий порядок
    timestamp: datetime = field(default_factory=utc_now)

    def execution_price(self, side: OrderSide, quantity: Decimal) -> Decimal | None:
        """Full-size VWAP, or None when a valid two-sided book cannot fill it."""
        if (
            not quantity.is_finite()
            or quantity <= 0
            or not self.bids
            or not self.asks
            or self.bids[0].price > self.asks[0].price
        ):
            return None
        left, value = quantity, Decimal("0")
        for level in self.asks if side == OrderSide.LONG else self.bids:
            if (
                not level.price.is_finite()
                or not level.size.is_finite()
                or level.price <= 0
                or level.size <= 0
            ):
                return None
            take = min(left, level.size)
            value += take * level.price
            left -= take
            if left == 0:
                return value / quantity
        return None

    def estimate_slippage(self, side: OrderSide, notional_usd: Decimal) -> Decimal:
        """Средневзвешенная цена исполнения vs mid, возвращает долю проскальзывания."""
        levels = self.asks if side == OrderSide.LONG else self.bids
        if not levels:
            return Decimal("1")  # нет ликвидности

        if not self.bids or not self.asks:
            return Decimal("1")  # нельзя вычислить mid без обеих сторон

        mid = (self.bids[0].price + self.asks[0].price) / Decimal("2")
        if mid == 0:
            return Decimal("1")

        if notional_usd <= 0 or mid <= 0 or self.bids[0].price > self.asks[0].price:
            return Decimal("1")
        filled = Decimal("0")
        quantity = Decimal("0")
        for lvl in levels:
            if lvl.price <= 0 or lvl.size <= 0:
                continue
            take = min(lvl.price * lvl.size, notional_usd - filled)
            filled += take
            quantity += take / lvl.price
            if filled >= notional_usd:
                break
        if filled < notional_usd or quantity <= 0:
            return Decimal("1")
        avg_price = filled / quantity
        slippage = ((avg_price - mid) if side == OrderSide.LONG else (mid - avg_price)) / mid
        return max(Decimal("0"), slippage)


@dataclass
class FeeRate:
    exchange: str
    symbol: str
    maker: Decimal
    taker: Decimal


@dataclass
class Balance:
    exchange: str
    total_usd: Decimal
    available_usd: Decimal
    timestamp: datetime = field(default_factory=utc_now)


# ── Orders & positions ────────────────────────────────────────────────────────


@dataclass
class OrderRequest:
    exchange: str
    symbol: str
    side: OrderSide
    notional_usd: Decimal  # размер в USD (бот считает в USD, адаптер конвертирует)
    leverage: Decimal
    is_reduce_only: bool = False
    client_id: str = ""
    quantity: Decimal | None = None


@dataclass
class OrderResult:
    exchange: str
    symbol: str
    order_id: str
    side: OrderSide
    filled_size: Decimal
    avg_price: Decimal
    fee_paid: Decimal
    success: bool
    error: str = ""
    timestamp: datetime = field(default_factory=utc_now)
    uncertain: bool = False


@dataclass
class ArbitragePosition:
    """Одна арбитражная пара (два ордера на двух биржах)."""

    id: str
    symbol: str
    long_exchange: str
    short_exchange: str
    notional_usd: Decimal  # размер одной ноги
    leverage: Decimal
    long_order: OrderResult | None = None
    short_order: OrderResult | None = None
    status: PositionStatus = PositionStatus.OPENING
    entry_spread: Decimal = Decimal("0")
    funding_received: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    opened_at: datetime = field(default_factory=utc_now)
    closed_at: datetime | None = None
    realized_price_pnl: Decimal = Decimal("0")
    unrealized_price_pnl: Decimal = Decimal("0")
    long_remaining: Decimal | None = None
    short_remaining: Decimal | None = None
    last_funding_at: datetime | None = None
    mode: BotMode = BotMode.PAPER
    close_reason: str = ""
    needs_reconciliation: bool = False
    close_pnl_estimate: Decimal | None = None
    strategy_version: str = "carry-v2"
    funding_state: dict = field(default_factory=dict)
    negative_funding_since: datetime | None = None
    peak_close_pnl: Decimal | None = None

    def __post_init__(self) -> None:
        self.opened_at = as_utc(self.opened_at)
        if self.closed_at:
            self.closed_at = as_utc(self.closed_at)
        if self.last_funding_at:
            self.last_funding_at = as_utc(self.last_funding_at)

    def update_net_pnl(self) -> None:
        self.net_pnl = (
            self.funding_received
            + self.realized_price_pnl
            + self.unrealized_price_pnl
            - self.fees_paid
        )

    @property
    def is_complete(self) -> bool:
        return self.long_order is not None and self.short_order is not None


@dataclass
class Position:
    """Открытая позиция на бирже."""

    exchange: str
    symbol: str
    side: OrderSide
    size: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    timestamp: datetime = field(default_factory=utc_now)
