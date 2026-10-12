# Funding Rate Arbitrage Bot

> Текущий рабочий контур: `dry_run` и `paper`. Режим `live` заблокирован до
> реализации подтверждённых исполнений и фактических funding-платежей Nado.
> Подробности исправлений и ограничения: [docs/fixes.md](docs/fixes.md).
> Интеграция Entropy: [docs/entropy.md](docs/entropy.md).
> Lighter на Robinhood Chain: [docs/lighter_robinhood.md](docs/lighter_robinhood.md).
> Extended Exchange: [docs/extended.md](docs/extended.md).
> Hibachi: [docs/hibachi.md](docs/hibachi.md).
> Polymarket Perps: [docs/polymarket_perps.md](docs/polymarket_perps.md).
> Spot long + perpetual short: [docs/spot_perp.md](docs/spot_perp.md).

Delta-neutral арбитраж ставок финансирования между **Hyperliquid**, **Entropy
(HIP-3)**, **Lighter** (Robinhood Chain) и **Nado DEX** (Ink L2).

## Стратегия

Когда funding rate на один актив (например SOL) сильно различается между биржами:
- **LONG** на бирже с низким funding (лонгам платят или берут мало)
- **SHORT** на бирже с высоким funding (шорты получают выплаты)

Обе ноги открываются одинаковым количеством базового актива. Если исполненные
количества различаются, пара закрывается; неудачное закрытие остаётся под контролем.

```
P&L = funding + ценовой P&L обеих ног - комиссии
```

## Быстрый старт

```bash
# 1. Клонировать и установить
git clone <repo>
cd funding-arb-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Настроить
cp .env.example .env
# Заполнить .env (минимум для dry_run: ничего не нужно)

# 3. Запустить в режиме наблюдения
python -m src.main
```

## Режимы

| Режим | Описание | Требует ключи |
|-------|----------|---------------|
| `dry_run` | Только сканирование, таблица спредов в консоль | Нет |
| `paper` | Виртуальный портфель, реальные цены, P&L учёт | Нет |
| `live` | Запуск заблокирован: интеграция исполнения Nado не завершена | — |

Устанавливается через `BOT_MODE` в `.env`.

## Ключевые параметры

| Параметр | Дефолт | Описание |
|----------|--------|----------|
| `SYMBOLS` | `auto` | `auto` = все общие символы, или `BTC,ETH,SOL` |
| `MAX_POSITION_PCT` | `0.10` | Размер одной ноги, % от баланса |
| `LEVERAGE` | `1.0` | Кредитное плечо |
| `MIN_FUNDING_SPREAD` | `0.0001` | Минимальный спред для входа за час |
| `EXPECTED_HOLD_HOURS` | `8` | Горизонт оценки окупаемости входа |
| `PAPER_BALANCE_USD` | `10000` | Начальный виртуальный баланс каждой биржи |
| `ENTROPY_ENABLED` | `true` | Добавить Entropy HIP-3 (`io`) в сканер |
| `EXIT_FUNDING_SPREAD` | `0.00005` | Закрыть когда спред упал ниже |
| `MAX_OPEN_PAIRS` | `5` | Макс. число одновременных пар |
| `DELTA_STOP_LOSS_PCT` | `0.02` | Stop-loss по расхождению цен (2%) |
| `MAX_HOLD_HOURS` | `72` | Принудительное закрытие через N часов |
| `SHUTDOWN_MODE` | `close_positions` | При SIGTERM: закрыть всё |

Полный список параметров: [.env.example](.env.example)

В `paper` кошельки и приватные ключи не нужны. Для сравнения production-рынков
задайте `HYPERLIQUID_TESTNET=false`. Funding Nado поступает из archive API как
24-часовая ставка; сканер переводит все ставки и пороги в почасовые единицы.
Существующий `.env` автоматически не меняется: пересмотрите старые funding-пороги.

## Деплой на VPS (Ubuntu 22.04+)

```bash
# Скопировать код на сервер
scp -r . user@server:/opt/funding-arb

# Запустить setup
ssh user@server
cd /opt/funding-arb
sudo bash deploy/setup.sh

# Заполнить .env
sudo nano /opt/funding-arb/.env

# Запустить
sudo systemctl start funding-arb
sudo systemctl status funding-arb

# Логи
journalctl -u funding-arb -f
```

## Мониторинг

### Health Check
```bash
curl http://localhost:8080/health
# {"status": "ok", "uptime_sec": 3600, "open_pairs": 2, ...}

curl http://localhost:8080/metrics
# {"net_pnl": 0.00123, "funding_received": 0.00456, ...}
```

### Telegram
Бот присылает уведомления при:
- Открытии/закрытии позиции
- Ошибках и circuit breaker
- Delta warning (расхождение цен > 70% от stop-loss)
- Ежедневный отчёт в 00:00 UTC

### Логи
```bash
# Реальное время
journalctl -u funding-arb -f

# Последние 100 строк
journalctl -u funding-arb -n 100

# JSON лог файл
tail -f /opt/funding-arb/logs/arb.log | python3 -m json.tool
```

## Обновление

```bash
sudo bash /opt/funding-arb/deploy/update.sh
```

## Тесты

```bash
pytest tests/ -v
pytest tests/ -v -k "test_scanner"     # конкретный файл
pytest tests/ --tb=short               # краткий вывод ошибок
```

## Структура

```
src/
├── adapters/
│   ├── base.py          ABC ExchangeAdapter
│   ├── hyperliquid.py   Hyperliquid REST + WS + SDK
│   ├── entropy.py       Entropy HIP-3 через Hyperliquid API
│   └── nado.py          Nado public REST/archive market data
├── core/
│   ├── scanner.py       Поиск спредов, spike detection
│   ├── executor.py      Открытие/закрытие пар, retry
│   ├── risk.py          Лимиты, circuit breaker, delta alert
│   └── pnl_tracker.py  Мониторинг, funding accrual
├── config.py            pydantic-settings
├── models.py            Dataclasses (Decimal everywhere)
├── db.py                SQLite через aiosqlite
├── health.py            HTTP /health + /metrics
├── notifier.py          Telegram
└── main.py              asyncio orchestrator
```

## Риски

> ⚠️ Funding arbitrage не является безрисковой стратегией.

- **Ликвидация**: резкое движение цены может ликвидировать одну ногу
- **Funding разворот**: ставка может измениться до выплаты
- **Slippage**: при закрытии обеих ног одновременно
- **Межбиржевой depeg**: цены на разных DEX могут временно расходиться
- **Смарт-контракт риск**: Nado работает на Ink L2

Live потребует отдельного завершения интеграции и проверки на тестовой сети;
одного периода paper trading для этого недостаточно.

## Документация

- [ARCHITECTURE.md](ARCHITECTURE.md) — полная архитектура
- [PLAN.md](PLAN.md) — план реализации по фазам
- [docs/risk_management.md](docs/risk_management.md) — риск-параметры
- [docs/pnl_formula.md](docs/pnl_formula.md) — математика P&L
