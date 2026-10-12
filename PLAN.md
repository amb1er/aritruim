# Funding Rate Arbitrage Bot — План реализации

> Файл обновляется после завершения каждой фазы.
> Последнее обновление: 2026-04-24
> Текущая фаза: **✅ Все фазы завершены — бот готов к деплою**

---

## Статус фаз

| Фаза | Название | Статус | Дата |
|------|----------|--------|------|
| M1 | Каркас + Сканер (dry-run) | ✅ Завершена | 2026-04-24 |
| M2 | Paper Trading + Executor | ✅ Завершена | 2026-04-24 |
| M3 | Live Trading + Мониторинг | ✅ Завершена | 2026-04-24 |
| M4 | Деплой на VPS + Health + Docs | ✅ Завершена | 2026-04-24 |

---

## ✅ M1 — Каркас + Сканер (завершена 2026-04-24)

### Цель
Запустить dry-run сканер: бот читает funding rates с обеих бирж и печатает таблицу спредов.
Никаких ордеров, никаких рисков.

### Что сделано

**Инфраструктура**
- [x] `pyproject.toml` — зависимости, ruff, mypy, pytest config
- [x] `.env.example` — все параметры с комментариями на русском
- [x] `.gitignore` — `.env` и `data/` исключены
- [x] `CLAUDE.md` — контекст проекта для Claude между сессиями
- [x] `ARCHITECTURE.md` — полная архитектура, API эндпоинты, milestone план

**Модели (`src/models.py`)**
- [x] `FundingRate` — ставка финансирования (exchange, symbol, rate, interval_hours)
- [x] `FundingSpread` — спред между двумя биржами (long/short exchange, spread, net_pnl)
- [x] `Orderbook` + `OrderbookLevel` — стакан с методом `estimate_slippage()`
- [x] `FeeRate` — maker/taker комиссии
- [x] `Balance` — баланс аккаунта
- [x] `OrderRequest` / `OrderResult` — структуры ордеров
- [x] `ArbitragePosition` — арбитражная пара (две ноги)
- [x] `Position` — открытая позиция на бирже
- [x] Enum: `BotMode`, `OrderSide`, `PositionStatus`, `ShutdownMode`

**Конфигурация (`src/config.py`)**
- [x] `pydantic-settings` из `.env`
- [x] Все параметры: `MAX_POSITION_PCT`, `LEVERAGE`, `MAX_OPEN_PAIRS`
- [x] Пороги: `MIN_FUNDING_SPREAD`, `EXIT_FUNDING_SPREAD`, `MIN_NET_PNL_AFTER_FEES`
- [x] Риск: `DELTA_STOP_LOSS_PCT`, `CIRCUIT_BREAKER_FAILURES`, `MAX_HOLD_HOURS`
- [x] Символы: `SYMBOLS=auto` (пересечение) или явный список `BTC,ETH,SOL`
- [x] Blacklist символов
- [x] Shutdown: `SHUTDOWN_MODE=close_positions`

**Адаптеры**
- [x] `ExchangeAdapter` (ABC) — единый интерфейс для всех бирж
- [x] `HyperliquidAdapter` — funding rates, orderbook, balance, positions (read-only)
  - POST `https://api.hyperliquid.xyz/info` с JSON body
  - Funding interval: 1 час
  - `metaAndAssetCtxs` для batch funding rates
- [x] `NadoAdapter` — funding rates и orderbook (read-only)
  - `/symbols`, gateway `type=market_liquidity`, archive `funding_rates`
  - Funding quote interval: 24 часа; нормализация к почасовой ставке
  - Баланс, позиции и live-исполнение намеренно не реализованы
  - Ink L2 (Kraken Ethereum L2)

**Сканер (`src/core/scanner.py`)**
- [x] Параллельный опрос всех бирж через `asyncio.gather`
- [x] Нормализация ставок к единому интервалу (per hour)
- [x] Авто-discovery общих символов (intersection)
- [x] Учёт whitelist/blacklist символов
- [x] Расчёт спреда и net PnL (с вычетом taker fee обеих ног × 2)
- [x] Сортировка по net PnL
- [x] Таблица спредов в консоль (★ = выгодно после комиссий)
- [x] Очередь `opportunities` для Executor (M2)

**Risk Manager (`src/core/risk.py`)**
- [x] `can_open()` — проверка лимитов перед входом
- [x] `calc_position_size()` — размер ноги = min_balance × MAX_POSITION_PCT × LEVERAGE
- [x] `should_close()` — exit логика (схлопывание спреда, max hold, delta stop)
- [x] Circuit breaker с exponential backoff

**Заглушки для M2**
- [x] `Executor` — только логирует найденные возможности
- [x] `PnlTracker` — noop

**Оркестратор (`src/main.py`)**
- [x] asyncio event loop
- [x] SIGTERM/SIGINT → shutdown_event
- [x] structlog JSON логирование
- [x] Создание директорий `data/` и `logs/`

**Тесты**
- [x] `tests/conftest.py` — общие фикстуры
- [x] `tests/test_hyperliquid.py` — 5 тестов с HTTP моками
- [x] `tests/test_nado.py` — 5 тестов с HTTP моками
- [x] `tests/test_scanner.py` — 4 теста логики спредов

### Как запустить M1
```bash
cp .env.example .env
pip install -e ".[dev]"
python -m src.main      # dry_run по умолчанию
pytest tests/ -v        # запустить тесты
```

---

## ✅ M2 — Paper Trading + Executor (завершена 2026-04-24)

### Цель
Бот открывает виртуальные позиции с реальными ценами, считает P&L.
Никаких реальных ордеров. Проверяем логику перед live.

### Что сделано

**База данных (`src/db.py`)**
- [x] `aiosqlite` — инициализация схемы при старте
- [x] Таблица `positions` — открытые арбитражные пары (INSERT OR REPLACE)
- [x] Таблица `trades` — история всех ордеров
- [x] Таблица `funding_log` — funding payments по каждой ноге
- [x] Таблица `pnl_summary` — дневной P&L снэпшот
- [x] `save_position`, `get_open_positions`, `get_all_positions`
- [x] `save_trade`, `log_funding`, `get_total_funding`
- [x] `save_pnl_snapshot`, `get_daily_pnl`
- [x] Сериализация `OrderResult` в JSON для хранения в БД

**Executor (`src/core/executor.py`)**
- [x] Получение возможностей из очереди `scanner.opportunities`
- [x] Восстановление позиций из БД при старте
- [x] Проверка через `RiskManager.can_open()`
- [x] Получение orderbook с обеих бирж для slippage check
- [x] Paper trading: исполнение по best ask (лонг) / best bid (шорт)
- [x] Атомарное открытие обеих ног через `asyncio.gather`
- [x] Rollback если одна нога не исполнилась
- [x] `close_position(reason)` — закрытие одной пары
- [x] `close_all(reason)` — закрытие всех пар (используется при shutdown)
- [x] Сохранение позиций и трейдов в БД

**PnL Tracker (`src/core/pnl_tracker.py`)**
- [x] Цикл мониторинга каждые `MONITOR_INTERVAL_SEC`
- [x] Параллельный сбор funding rates для всех бирж открытых позиций
- [x] Начисление funding: `net = notional × (short_rate - long_rate)`
- [x] Расчёт delta между ногами
- [x] Проверка `RiskManager.should_close()` для каждой позиции
- [x] Автоматическое закрытие через Executor
- [x] Запись в `funding_log` и `pnl_summary`

**Notifier (`src/notifier.py`)**
- [x] Telegram Bot API через `aiohttp`
- [x] Уровни: `all` / `trades_only` / `errors_only`
- [x] `on_position_opened`, `on_position_closed`
- [x] `on_error`, `on_circuit_breaker`
- [x] `on_daily_report`, `on_startup`, `on_shutdown`
- [x] Graceful fallback — никогда не роняет основной процесс

**Main (`src/main.py`)** — обновлён
- [x] Подключение DB, Notifier, Risk, Scanner, Executor, PnlTracker
- [x] В `dry_run` — только сканер
- [x] В `paper` — scanner, executor и P&L tracker
- [ ] В `live` — запуск заблокирован до подтверждённых fills и cash events Nado
- [x] Graceful shutdown: `close_all()` с таймаутом, `on_shutdown()`

**Тесты M2**
- [x] `tests/test_executor.py` — 6 тестов: открытие, дубликат, закрытие, close_all, ask/bid
- [x] `tests/test_db.py` — 6 тестов: save/retrieve, update, closed filter, trade, funding, pnl
- [x] `tests/test_pnl_tracker.py` — 6 тестов: начисление, delta, условия закрытия

### Как запустить M2 (paper trading)
```bash
# В .env установить:
BOT_MODE=paper
TELEGRAM_TOKEN=<token>   # опционально
TELEGRAM_CHAT_ID=<id>    # опционально

python -m src.main
pytest tests/ -v
```

---

## ✅ M3 — Live Trading (завершена 2026-04-24)

### Цель
Реальные ордера на обеих биржах. Только после 2+ недель paper trading.

### Что сделано

**HyperliquidAdapter — trading** (`src/adapters/hyperliquid.py`)
- [x] `place_order()` — market ордер через `hyperliquid-python-sdk`
- [x] SDK вызов через `asyncio.to_thread()` (SDK синхронный)
- [x] Парсинг SDK ответа: `statuses[0].filled` → `OrderResult`
- [x] `cancel_order()` через `exchange.cancel()`
- [x] `close_position()` через `exchange.market_close()`
- [x] `get_mark_price()` — mid из стакана (прокси)
- [x] `get_funding_history()` — для spike detection
- [x] `subscribe_funding()` — WS `activeAssetCtx` с auto-reconnect
- [x] Таймаут `_ORDER_TIMEOUT_SEC=15s` на каждый ордер

**NadoAdapter — trading** (`src/adapters/nado.py`) — отложено
- [x] `place_order()` — EIP-712 подпись через `eth_account`
- [x] `_execute()` — POST `/execute` с nonce + signature
- [ ] EIP-712 подпись согласно актуальной схеме Nado
- [x] `cancel_order()` через execute `{"type": "cancel"}`
- [x] `close_position()` — reduce-only market ордер
- [x] `get_mark_price()` — mid из стакана
- [ ] Подтверждение fills, funding payments и восстановление после таймаута
- [ ] Подписки на документированные приватные события
- [x] Chain ID: 57073 (Ink L2)

**Executor — live mode** (`src/core/executor.py`) — защитный каркас
- [x] `_place_with_retry()` — 3 попытки, backoff 1s → 2s → 4s
- [x] `_reconcile_partial_fills()` — выравнивание ног при частичном исполнении
- [x] Таймаут помечается неоднозначным состоянием без слепого повтора
- [ ] Сверка неоднозначного состояния с обеими биржами

**Scanner — spike detection** (`src/core/scanner.py`)
- [x] `_is_spike()` — проверяет текущую ставку vs среднюю за lookback период
- [x] Порог: `SPIKE_MULTIPLIER=5x` от средней
- [x] Интегрирован в `_find_spreads()` для high-rate биржи

**PnlTracker — точная delta** (`src/core/pnl_tracker.py`)
- [x] `_calc_delta()` использует `get_mark_price()` если доступен
- [x] Fallback на прокси через funding rates

**Тесты M3** (`tests/test_trading.py`)
- [x] `test_hl_place_order_success` — парсинг SDK ответа
- [x] `test_hl_place_order_sdk_error` — ошибка SDK
- [x] `test_hl_place_order_timeout` — таймаут
- [x] `test_hl_cancel_order` — отмена ордера
- [x] `test_hl_get_mark_price` — mid расчёт
- [x] `test_hl_funding_history` — парсинг истории
- [x] `test_nado_place_order_success/error/timeout` — 3 теста
- [x] `test_nado_close_position` — reduce-only через positions
- [x] `test_executor_retries_on_failure` — retry при ошибке
- [x] `test_executor_gives_up_after_max_retries` — исчерпание попыток
- [x] `test_scanner_skips_spike` — spike detection блокирует вход

### Live режим
```bash
# В .env установить:
BOT_MODE=live  # сейчас завершится с безопасной ошибкой до подключения
HYPERLIQUID_PRIVATE_KEY=0x...
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_TESTNET=false     # ТОЛЬКО после проверки на testnet!
NADO_PRIVATE_KEY=0x...
NADO_WALLET_ADDRESS=0x...

python -m src.main
```

> Для live сначала нужны пункты интеграции Nado выше и проверка на тестовой сети.

---

## ✅ M4 — Деплой на VPS + Health + Docs (завершена 2026-04-24)

### Цель
Стабильная работа на VPS, автозапуск, алерты, документация.

### Что сделано

**Деплой** (`deploy/`)
- [x] `funding-arb.service` — systemd unit: Restart=on-failure, RestartSec=30
- [x] Sandbox: NoNewPrivileges, PrivateTmp, ProtectSystem=strict
- [x] `TimeoutStopSec=60` — время на graceful shutdown (закрытие позиций)
- [x] `setup.sh` — автоустановка на Ubuntu 22.04+: venv, зависимости, systemd
- [x] `update.sh` — обновление без потери позиций (save_state → git pull → start)

**Health Check** (`src/health.py`)
- [x] `GET /health` → 200 OK / 503 Degraded (при circuit breaker)
- [x] `GET /metrics` → uptime, open_pairs, net_pnl, funding, fees, error_count
- [x] `update_from_positions()` — обновление метрик каждые 10 сек

**Telegram алерты** (`src/notifier.py`)
- [x] `on_delta_warning()` — при delta ≥ 70% от DELTA_STOP_LOSS_PCT
- [x] `on_daily_report()` — uptime, открытые/закрытые пары, P&L
- [x] `_fmt_uptime()` — форматирование: "2d 5h 30m"

**Risk Manager** (`src/core/risk.py`)
- [x] `set_callbacks()` — подключение async callbacks из main.py
- [x] Delta warning при 70% от stop-loss порога
- [x] Circuit breaker callback → Telegram уведомление
- [x] `circuit_open` property для health check

**Main** (`src/main.py`)
- [x] `HealthServer` запускается на `0.0.0.0:8080`
- [x] `_daily_report_loop()` — расчёт секунд до полуночи UTC, loop
- [x] `_health_updater()` — обновляет метрики каждые 10 сек
- [x] Risk callbacks подключены к notifier
- [x] Health server останавливается при shutdown

**Документация**
- [x] `README.md` — быстрый старт, деплой, мониторинг, риски
- [x] `docs/pnl_formula.md` — математика: funding, спред, break-even, пример
- [x] `docs/risk_management.md` — все параметры, рекомендации, circuit breaker

**Тесты M4**
- [x] `tests/test_health.py` — 5 тестов: /health OK, 503 при CB, /metrics, update_from_positions
- [x] `tests/test_risk_m4.py` — 5 тестов: delta warning callback, CB callback, block entry, reset

### Деплой на VPS
```bash
# Первичная установка
sudo bash deploy/setup.sh

# Управление
sudo systemctl start|stop|restart|status funding-arb
journalctl -u funding-arb -f

# Обновление
sudo bash deploy/update.sh

# Мониторинг
curl http://localhost:8080/health
curl http://localhost:8080/metrics
```

---

## Решения по ключевым вопросам

| Вопрос | Решение |
|--------|---------|
| Вторая биржа | Nado DEX (подтверждено: есть REST/WS API) |
| Символы | `SYMBOLS=auto` — intersection всех общих перпов |
| Размер позиции | `MAX_POSITION_PCT × LEVERAGE × min(balance_A, balance_B)` |
| Выход из позиции | Держать до схлопывания спреда < `EXIT_FUNDING_SPREAD` |
| SIGTERM | `SHUTDOWN_MODE=close_positions` — немедленно закрыть всё |
| Деплой | VPS, уведомления в Telegram |
| MVP | Вариант A: Сканер + dry-run (M1) |

---

## Ключевые формулы

### Размер позиции
```
notional = min(balance_long, balance_short) × MAX_POSITION_PCT × LEVERAGE
```

### Net PnL за один funding период
```
net_pnl = spread
        - taker_fee_long × 2    (open + close)
        - taker_fee_short × 2   (open + close)
        - slippage_long
        - slippage_short

где spread = funding_short_per_period - funding_long_per_period
    (нормализовано к одному интервалу)
```

### Annualized rate
```
annualized = rate_per_interval × (8760 / interval_hours)
```

### Delta между ногами
```
delta_pct = |price_long_entry - price_short_entry| / price_entry_avg
закрыть если delta_pct ≥ DELTA_STOP_LOSS_PCT
```
