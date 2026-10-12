"""Конфигурация через pydantic-settings. Все значения из .env."""

from decimal import Decimal
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.models import BotMode, LogFormat, ShutdownMode, TelegramLevel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Режим ─────────────────────────────────────────────────────────────────
    bot_mode: BotMode = BotMode.DRY_RUN

    # ── Hyperliquid ───────────────────────────────────────────────────────────
    hyperliquid_wallet_address: str = ""
    hyperliquid_private_key: str = ""
    hyperliquid_testnet: bool = True
    hyperliquid_rest_url: str = "https://api.hyperliquid.xyz/info"
    hyperliquid_ws_url: str = "wss://api.hyperliquid.xyz/ws"

    hyperliquid_spot_enabled: bool = True
    hyperliquid_spot_maker_fee: Decimal = Field(default=Decimal("0.0004"), ge=0)
    hyperliquid_spot_taker_fee: Decimal = Field(default=Decimal("0.0007"), ge=0)

    # ── Entropy HIP-3 on Hyperliquid ─────────────────────────────────────────
    entropy_enabled: bool = True
    entropy_dex: str = Field(default="io", min_length=1)
    entropy_maker_fee: Decimal = Field(default=Decimal("0.0003"), ge=0)
    entropy_taker_fee: Decimal = Field(default=Decimal("0.0009"), ge=0)

    # Lighter domain deployed on Robinhood Chain
    lighter_robinhood_enabled: bool = True
    lighter_robinhood_rest_url: str = "https://api.rh.lighter.xyz"
    lighter_robinhood_ws_url: str = "wss://api.rh.lighter.xyz/stream"

    # Aster V3 USDT perpetuals (public data and paper trading)
    aster_enabled: bool = True
    aster_rest_url: str = "https://fapi.asterdex.com"
    aster_maker_fee: Decimal = Field(default=Decimal("0"), ge=0)
    aster_taker_fee: Decimal = Field(default=Decimal("0.0004"), ge=0)
    aster_group_b_taker_fee: Decimal = Field(default=Decimal("0.001"), ge=0)
    aster_rwa_taker_fee: Decimal = Field(default=Decimal("0.000125"), ge=0)

    # Extended Exchange on Starknet
    extended_enabled: bool = True
    extended_rest_url: str = "https://api.starknet.extended.exchange"
    extended_ws_url: str = "wss://api.starknet.extended.exchange"
    extended_maker_fee: Decimal = Field(default=Decimal("0"), ge=0)
    extended_taker_fee: Decimal = Field(default=Decimal("0.00025"), ge=0)

    # Hibachi perpetual exchange
    hibachi_enabled: bool = True
    hibachi_api_url: str = "https://api.hibachi.xyz"
    hibachi_data_url: str = "https://data-api.hibachi.xyz"
    hibachi_taker_fee: Decimal = Field(default=Decimal("0.00045"), ge=0)

    # Polymarket Perps (public data; API is currently experimental)
    polymarket_perps_enabled: bool = True
    polymarket_perps_rest_url: str = "https://api.perpetuals.polymarket.com"
    polymarket_perps_maker_fee: Decimal = Field(default=Decimal("0.000125"), ge=0)
    polymarket_perps_taker_fee: Decimal = Field(default=Decimal("0.0004"), ge=0)

    # TxFlow public market data (orders remain paper-only)
    txflow_enabled: bool = True
    txflow_rest_url: str = "https://api.txflow.com"
    txflow_ws_url: str = "wss://api.txflow.com/ws"
    txflow_api_version: str = "2026-02-04"
    txflow_maker_fee: Decimal = Field(default=Decimal("0.00015"), ge=0)
    txflow_taker_fee: Decimal = Field(default=Decimal("0.00045"), ge=0)

    # Four-venue points farming paper strategy
    farming_enabled: bool = True
    farming_notional_usd: Decimal = Field(default=Decimal("50"), gt=0)
    farming_expected_hold_hours: Decimal = Field(default=Decimal("8"), gt=0)
    farming_max_cash_loss_usd: Decimal = Field(default=Decimal("0"), ge=0)
    farming_min_expected_cash_pnl_usd: Decimal = Field(default=Decimal("0.01"))
    farming_max_open_pairs: int = Field(default=2, ge=1)
    farming_entry_confirmation_scans: int = Field(default=2, ge=1)
    farming_scan_interval_sec: int = Field(default=30, gt=0)
    farming_max_hold_hours: Decimal = Field(default=Decimal("24"), gt=0)
    farming_stop_loss_usd: Decimal = Field(default=Decimal("0.50"), gt=0)
    farming_take_profit_usd: Decimal = Field(default=Decimal("0.10"), gt=0)
    farming_funding_cap_per_hour: Decimal = Field(default=Decimal("0.001"), gt=0)
    farming_reentry_cooldown_sec: int = Field(default=1800, ge=0)
    farming_sqlite_path: Path = Path("data/farming.db")
    farming_log_file: Path = Path("logs/farming.log")

    # ── Nado ──────────────────────────────────────────────────────────────────
    nado_archive_url: str = "https://archive.prod.nado.xyz/v1"
    nado_wallet_address: str = ""
    nado_private_key: str = ""
    nado_rest_url: str = "https://gateway.prod.nado.xyz/v1"
    nado_ws_url: str = "wss://gateway.prod.nado.xyz/v1/ws"
    nado_sub_url: str = "wss://gateway.prod.nado.xyz/v1/subscribe"

    # ── Символы ───────────────────────────────────────────────────────────────
    # "auto" или явный список через запятую: "BTC,ETH,SOL"
    symbols: str = "auto"
    symbols_blacklist: str = ""

    @property
    def symbol_whitelist(self) -> list[str] | None:
        """None означает auto-discovery."""
        if self.symbols.lower() == "auto":
            return None
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def symbol_blacklist_set(self) -> set[str]:
        return {s.strip().upper() for s in self.symbols_blacklist.split(",") if s.strip()}

    # ── Размер позиций и плечо ────────────────────────────────────────────────
    max_position_pct: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    min_position_usd: Decimal = Field(default=Decimal("25"), gt=0)
    position_sizing_iterations: int = Field(default=12, ge=1, le=30)
    position_size_headroom: Decimal = Field(default=Decimal("0.85"), gt=0, le=1)
    exit_liquidity_multiplier: Decimal = Field(default=Decimal("2"), ge=1)
    leverage: Decimal = Field(default=Decimal("1.0"), ge=1)
    max_open_pairs: int = Field(default=5, ge=1)
    paper_balance_usd: Decimal = Field(default=Decimal("10000"), gt=0)
    expected_hold_hours: Decimal = Field(default=Decimal("12"), gt=0)
    funding_history_min_hours: Decimal = Field(default=Decimal("1"), ge=0)
    funding_history_window_hours: int = Field(default=24, ge=1)
    funding_forecast_window_hours: int = Field(default=2, ge=1)
    funding_history_bucket_sec: int = Field(default=300, ge=30)
    funding_forecast_discount: Decimal = Field(default=Decimal("0.75"), gt=0, le=1)
    entry_cost_coverage: Decimal = Field(default=Decimal("1.10"), ge=1)
    basis_risk_buffer_pct: Decimal = Field(default=Decimal("0.0005"), ge=0)
    max_entry_payback_fraction: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    max_funding_age_sec: int = Field(default=7200, gt=0)
    max_book_age_sec: int = Field(default=30, gt=0)
    min_soft_hold_hours: Decimal = Field(default=Decimal("1"), ge=0)
    negative_funding_exit_sec: int = Field(default=900, ge=0)
    max_recovery_hours: Decimal = Field(default=Decimal("8"), gt=0)
    exit_profit_buffer_pct: Decimal = Field(default=Decimal("0.0001"), ge=0)
    take_profit_pct: Decimal = Field(default=Decimal("0.0004"), ge=0)
    take_profit_confirmation_intervals: int = Field(default=2, ge=1)
    trailing_profit_activation_pct: Decimal = Field(default=Decimal("0.00025"), ge=0)
    trailing_profit_giveback_pct: Decimal = Field(default=Decimal("0.25"), ge=0, lt=1)
    max_net_loss_pct: Decimal = Field(default=Decimal("0.005"), gt=0)
    max_daily_loss_usd: Decimal = Field(default=Decimal("100"), ge=0)
    pair_loss_cooldown_sec: int = Field(default=86400, ge=0)
    pair_loss_lookback_hours: int = Field(default=48, ge=1)
    pair_loss_limit: int = Field(default=2, ge=1)
    max_signal_age_sec: int = Field(default=60, gt=0)
    liquidity_reject_limit: int = Field(default=3, ge=1)
    liquidity_cooldown_sec: int = Field(default=120, ge=0)
    exit_funding_spike_multiplier: Decimal = Field(default=Decimal("5"), gt=1)

    @field_validator(
        "max_position_pct",
        "min_position_usd",
        "position_size_headroom",
        "exit_liquidity_multiplier",
        "max_daily_loss_usd",
        "take_profit_pct",
        "trailing_profit_activation_pct",
        "trailing_profit_giveback_pct",
        "exit_funding_spike_multiplier",
        "basis_risk_buffer_pct",
        "max_entry_payback_fraction",
        "leverage",
        "entropy_maker_fee",
        "entropy_taker_fee",
        "extended_maker_fee",
        "extended_taker_fee",
        "hibachi_taker_fee",
        "hyperliquid_spot_maker_fee",
        "hyperliquid_spot_taker_fee",
        "polymarket_perps_maker_fee",
        "polymarket_perps_taker_fee",
        "txflow_maker_fee",
        "txflow_taker_fee",
        "farming_notional_usd",
        "farming_expected_hold_hours",
        "farming_max_cash_loss_usd",
        "farming_min_expected_cash_pnl_usd",
        "farming_max_hold_hours",
        "farming_stop_loss_usd",
        "farming_take_profit_usd",
        "farming_funding_cap_per_hour",
        mode="before",
    )
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))

    # ── Пороги входа ──────────────────────────────────────────────────────────
    # Funding thresholds are per hour on every exchange.
    # Expected profitability is evaluated over expected_hold_hours.
    min_funding_spread: Decimal = Field(default=Decimal("0.0001"))
    min_net_pnl_after_fees: Decimal = Field(default=Decimal("0.00001"))
    max_slippage: Decimal = Field(default=Decimal("0.0035"))
    slippage_check_notional: Decimal = Field(default=Decimal("10000"))

    # ── Выход ─────────────────────────────────────────────────────────────────
    # Закрыть когда спред схлопнулся до этого уровня
    exit_funding_spread: Decimal = Field(default=Decimal("0.00005"))
    reentry_cooldown_sec: int = Field(default=1800, ge=0)
    entry_confirmation_scans: int = Field(default=3, ge=1)
    market_data_grace_sec: int = Field(default=180, ge=0)
    max_hold_hours: int = Field(default=72, gt=0)
    # Закрыть только после N последовательных циклов с низким спредом (защита от шума)
    exit_spread_cooldown_intervals: int = Field(default=3, ge=1)

    # ── Риск ──────────────────────────────────────────────────────────────────
    delta_stop_loss_pct: Decimal = Field(default=Decimal("0.02"))
    circuit_breaker_failures: int = 3
    circuit_breaker_base_delay_sec: int = 30
    funding_spike_lookback_min: int = 60

    # ── Интервалы ─────────────────────────────────────────────────────────────
    scan_interval_sec: int = Field(default=30, gt=0)
    monitor_interval_sec: int = Field(default=15, gt=0)

    # ── Shutdown ──────────────────────────────────────────────────────────────
    shutdown_mode: ShutdownMode = ShutdownMode.CLOSE_POSITIONS
    shutdown_timeout_sec: int = 30

    # ── Персистентность ───────────────────────────────────────────────────────
    sqlite_path: Path = Path("data/arb.db")

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_level: TelegramLevel = TelegramLevel.TRADES_ONLY

    # ── Логирование ───────────────────────────────────────────────────────────
    log_level: str = "info"
    log_format: LogFormat = LogFormat.JSON
    log_file: Path = Path("logs/arb.log")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @field_validator(
        "min_funding_spread",
        "min_net_pnl_after_fees",
        "max_slippage",
        "slippage_check_notional",
        "exit_funding_spread",
        "delta_stop_loss_pct",
        mode="before",
    )
    @classmethod
    def parse_decimal_fields(cls, v: object) -> Decimal:
        return Decimal(str(v))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
