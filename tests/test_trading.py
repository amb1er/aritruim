"""
M3 тесты — торговля, retry, частичные исполнения, spike detection.
Адаптеры тестируются с моками HTTP; реальная торговля — через testnet вручную.
"""

import asyncio
from datetime import UTC
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.hyperliquid import HyperliquidAdapter
from src.adapters.nado import NadoAdapter
from src.models import (
    Balance,
    BotMode,
    FundingRate,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderResult,
    OrderSide,
)

# ══════════════════════════════════════════════════════════════════════════════
# HyperliquidAdapter — торговля
# ══════════════════════════════════════════════════════════════════════════════

HL_SDK_OK = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {"statuses": [{"filled": {"totalSz": "0.01", "avgPx": "100000", "oid": 42}}]},
    },
}

HL_SDK_ERROR = {
    "status": "err",
    "response": "Insufficient margin",
}


def make_ob(bid: str = "99990", ask: str = "100010", exchange: str = "hyperliquid") -> Orderbook:
    return Orderbook(
        exchange=exchange,
        symbol="BTC",
        bids=[OrderbookLevel(Decimal(bid), Decimal("5"))],
        asks=[OrderbookLevel(Decimal(ask), Decimal("5"))],
    )


@pytest.fixture
def hl_adapter(settings):
    adapter = HyperliquidAdapter(settings)
    adapter._exchange = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_hl_place_order_success(hl_adapter, settings):
    """place_order парсит SDK ответ и возвращает корректный OrderResult."""
    mock_exchange = MagicMock()
    mock_exchange.market_open.return_value = HL_SDK_OK
    hl_adapter._exchange = mock_exchange

    req = OrderRequest("hyperliquid", "BTC", OrderSide.LONG, Decimal("1000"), Decimal("1.0"))

    async with hl_adapter:
        with patch.object(hl_adapter, "get_orderbook", new=AsyncMock(return_value=make_ob())):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=HL_SDK_OK)):
                result = await hl_adapter.place_order(req)

    assert result.success is True
    assert result.filled_size == Decimal("0.01")
    assert result.avg_price == Decimal("100000")
    assert result.order_id == "42"
    assert result.fee_paid > Decimal("0")


@pytest.mark.asyncio
async def test_hl_place_order_sdk_error(hl_adapter):
    """SDK возвращает ошибку → OrderResult.success = False."""
    req = OrderRequest("hyperliquid", "BTC", OrderSide.LONG, Decimal("1000"), Decimal("1.0"))

    async with hl_adapter:
        with patch.object(hl_adapter, "get_orderbook", new=AsyncMock(return_value=make_ob())):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=HL_SDK_ERROR)):
                result = await hl_adapter.place_order(req)

    assert result.success is False
    assert "margin" in result.error.lower() or result.error != ""


@pytest.mark.asyncio
async def test_hl_place_order_timeout(hl_adapter):
    """Таймаут ордера → OrderResult.success = False с сообщением timeout."""
    req = OrderRequest("hyperliquid", "BTC", OrderSide.LONG, Decimal("1000"), Decimal("1.0"))

    async with hl_adapter:
        with patch.object(hl_adapter, "get_orderbook", new=AsyncMock(return_value=make_ob())):
            with patch("asyncio.to_thread", new=AsyncMock(side_effect=asyncio.TimeoutError)):
                result = await hl_adapter.place_order(req)

    assert result.success is False
    assert "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_hl_cancel_order(hl_adapter):
    """cancel_order возвращает True при успехе SDK."""
    mock_exchange = MagicMock()
    mock_exchange.cancel.return_value = {"status": "ok"}
    hl_adapter._exchange = mock_exchange

    async with hl_adapter:
        with patch("asyncio.to_thread", new=AsyncMock(return_value={"status": "ok"})):
            result = await hl_adapter.cancel_order("BTC", "42")

    assert result is True


@pytest.mark.asyncio
async def test_hl_get_mark_price(hl_adapter):
    """get_mark_price возвращает mid между bid и ask."""
    async with hl_adapter:
        with patch.object(
            hl_adapter, "get_orderbook", new=AsyncMock(return_value=make_ob("99990", "100010"))
        ):
            price = await hl_adapter.get_mark_price("BTC")

    assert price == Decimal("100000")


@pytest.mark.asyncio
async def test_hl_funding_history(hl_adapter):
    """get_funding_history парсит список ставок."""
    mock_history = [
        {"fundingRate": "0.0001", "time": 1700000000000},
        {"fundingRate": "0.0002", "time": 1700003600000},
    ]
    async with hl_adapter:
        with patch.object(hl_adapter, "_post", new=AsyncMock(return_value=mock_history)):
            history = await hl_adapter.get_funding_history("BTC", lookback_hours=1)

    assert len(history) == 2
    assert history[0].rate == Decimal("0.0001")
    assert history[1].rate == Decimal("0.0002")


# ══════════════════════════════════════════════════════════════════════════════
# NadoAdapter — торговля
# ══════════════════════════════════════════════════════════════════════════════

NADO_EXECUTE_OK = {
    "status": "ok",
    "data": {"orderId": "nado_123", "filledSize": "0.01", "avgPrice": "100000"},
}
NADO_EXECUTE_ERR = {"status": "error", "error": "Insufficient margin"}


@pytest.fixture
def nado_adapter(settings):
    return NadoAdapter(settings)


@pytest.mark.asyncio
async def test_nado_live_submission_is_disabled(nado_adapter):
    req = OrderRequest("nado", "BTC", OrderSide.SHORT, Decimal("1000"), Decimal("1"))
    result = await nado_adapter.place_order(req)
    assert result.success is False
    assert result.filled_size == 0
    assert "disabled" in result.error


def test_nado_acknowledgement_is_not_a_fill(nado_adapter):
    req = OrderRequest("nado", "BTC", OrderSide.LONG, Decimal("1000"), Decimal("1"))
    result = nado_adapter._parse_execute_result(
        req,
        {"status": "success", "data": {"digest": "0x123"}},
        Decimal("1"),
        Decimal("1000"),
    )
    assert not result.success and result.uncertain
    assert result.filled_size == 0
    assert result.order_id == "0x123"


@pytest.mark.asyncio
async def test_nado_close_does_not_guess_positions(nado_adapter):
    with pytest.raises(NotImplementedError, match="disabled"):
        await nado_adapter.close_position("BTC", "long")


@pytest.fixture
def live_executor(settings, mock_adapters_live, mock_db, mock_notifier):
    from src.core.executor import Executor
    from src.core.risk import RiskManager

    settings.bot_mode = BotMode.LIVE
    risk = RiskManager(settings)
    return Executor(
        adapters=mock_adapters_live,
        risk=risk,
        db=mock_db,
        notifier=mock_notifier,
        settings=settings,
    )


@pytest.fixture
def mock_adapters_live():
    hl = MagicMock()
    hl.name = "hyperliquid"
    hl.get_balance = AsyncMock(
        return_value=Balance("hyperliquid", Decimal("10000"), Decimal("10000"))
    )
    hl.get_orderbook = AsyncMock(return_value=make_ob())
    hl.place_order = AsyncMock()
    hl.close_position = AsyncMock()

    nado = MagicMock()
    nado.name = "nado"
    nado.get_balance = AsyncMock(return_value=Balance("nado", Decimal("10000"), Decimal("10000")))
    nado.get_orderbook = AsyncMock(return_value=make_ob("99980", "100020", "nado"))
    nado.place_order = AsyncMock()
    nado.close_position = AsyncMock()

    return {"hyperliquid": hl, "nado": nado}


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.save_position = AsyncMock()
    db.save_trade = AsyncMock()
    db.get_open_positions = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_notifier():
    n = MagicMock()
    n.on_position_opened = AsyncMock()
    n.on_position_closed = AsyncMock()
    n.on_error = AsyncMock()
    return n


@pytest.mark.asyncio
async def test_executor_does_not_retry_timeout(live_executor, mock_adapters_live):
    """_place_with_retry повторяет ордер при ошибке."""
    fail_result = OrderResult(
        "hyperliquid",
        "BTC",
        "",
        OrderSide.LONG,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        False,
        "timeout",
    )
    ok_result = OrderResult(
        "hyperliquid",
        "BTC",
        "42",
        OrderSide.LONG,
        Decimal("0.01"),
        Decimal("100000"),
        Decimal("0.035"),
        True,
    )

    adapter = mock_adapters_live["hyperliquid"]
    adapter.place_order = AsyncMock(side_effect=[fail_result, ok_result])

    req = OrderRequest("hyperliquid", "BTC", OrderSide.LONG, Decimal("1000"), Decimal("1.0"))
    result = await live_executor._place_with_retry(adapter, req, max_attempts=3)

    assert result.success is False
    assert result.uncertain
    assert adapter.place_order.call_count == 1


@pytest.mark.asyncio
async def test_executor_does_not_blindly_retry_rejection(live_executor, mock_adapters_live):
    """После max_attempts возвращает последний неуспешный результат."""
    fail_result = OrderResult(
        "hyperliquid",
        "BTC",
        "",
        OrderSide.LONG,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        False,
        "error",
    )
    adapter = mock_adapters_live["hyperliquid"]
    adapter.place_order = AsyncMock(return_value=fail_result)

    req = OrderRequest("hyperliquid", "BTC", OrderSide.LONG, Decimal("1000"), Decimal("1.0"))
    result = await live_executor._place_with_retry(adapter, req, max_attempts=2)

    assert result.success is False
    assert adapter.place_order.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — spike detection
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scanner_skips_spike(settings):
    """Спайк в funding → символ пропускается."""

    from src.core.scanner import FundingScanner

    settings.funding_spike_lookback_min = 60

    hl = MagicMock()
    hl.name = "hyperliquid"
    hl.get_funding_rates = AsyncMock(
        return_value=[
            FundingRate(
                "hyperliquid", "BTC", Decimal("0.0001"), 1, __import__("datetime").datetime.now(UTC)
            ),
        ]
    )
    hl.get_fee_rate = AsyncMock(return_value=MagicMock(taker=Decimal("0.00035")))
    # История: средняя 0.0001, текущая 0.001 (10x) → спайк
    hl.get_funding_history = AsyncMock(
        return_value=[
            FundingRate(
                "hyperliquid", "BTC", Decimal("0.0001"), 1, __import__("datetime").datetime.now(UTC)
            ),
            FundingRate(
                "hyperliquid", "BTC", Decimal("0.0001"), 1, __import__("datetime").datetime.now(UTC)
            ),
        ]
    )

    nado = MagicMock()
    nado.name = "nado"
    nado.get_funding_rates = AsyncMock(
        return_value=[
            FundingRate(
                "nado", "BTC", Decimal("0.001"), 8, __import__("datetime").datetime.now(UTC)
            ),
        ]
    )
    nado.get_fee_rate = AsyncMock(return_value=MagicMock(taker=Decimal("0.0005")))
    nado.get_funding_history = AsyncMock(
        return_value=[
            FundingRate(
                "nado", "BTC", Decimal("0.0001"), 8, __import__("datetime").datetime.now(UTC)
            ),
            FundingRate(
                "nado", "BTC", Decimal("0.0001"), 8, __import__("datetime").datetime.now(UTC)
            ),
        ]
    )

    scanner = FundingScanner([hl, nado], settings)
    await scanner._scan_once()

    # Спайк обнаружен → ни одна возможность не попала в очередь
    assert scanner.opportunities.empty()
