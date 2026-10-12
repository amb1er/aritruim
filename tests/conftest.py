"""Общие фикстуры для тестов."""

from decimal import Decimal

import pytest

from src.config import Settings
from src.models import BotMode, ShutdownMode


@pytest.fixture
def settings() -> Settings:
    """Настройки для тестов — dry_run, testnet, маленькие лимиты."""
    return Settings(
        _env_file=None,
        hyperliquid_private_key="",
        hyperliquid_wallet_address="",
        nado_private_key="",
        nado_wallet_address="",
        bot_mode=BotMode.DRY_RUN,
        entry_confirmation_scans=1,
        funding_history_min_hours=Decimal("0"),  # History has separate elapsed-time tests.
        hyperliquid_testnet=True,
        symbols="BTC,ETH,SOL",
        max_position_pct=Decimal("0.10"),
        leverage=Decimal("1.0"),
        min_funding_spread=Decimal("0.0001"),
        exit_funding_spread=Decimal("0.00005"),
        shutdown_mode=ShutdownMode.CLOSE_POSITIONS,
        telegram_token="",
        telegram_chat_id="",
    )
