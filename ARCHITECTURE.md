# Funding Rate Arbitrage Bot — Architecture

## Стратегия

**Delta-neutral funding rate arbitrage** между Hyperliquid и Nado DEX.

Когда funding rate на один и тот же актив сильно отличается между биржами:
- LONG на бирже с низким/отрицательным funding (лонгам платят или мало берут)
- SHORT на бирже с высоким положительным funding (шорты получают выплаты)

```
Прибыль = (funding_high - funding_low) × notional
         - open_fee_A - open_fee_B
         - close_fee_A - close_fee_B
         - slippage_A - slippage_B
```

Рыночный риск ≈ 0 (delta-neutral), пока обе ноги открыты.

---

## Биржи

| Биржа       | Тип     | Сеть         | Символы                         | Плечо  |
|-------------|---------|--------------|----------------------------------|--------|
| Hyperliquid | DEX     | L1 (HL)      | auto-discovery через API         | до 50x |
| Nado        | DEX     | Ink L2 (ETH) | BTC, ETH, SOL, BNB, XRP + новые | до 20x |

**Общие символы** обнаруживаются автоматически при старте (intersection).

---

## Структура проекта

```
funding-arb-bot/
├── src/
│   ├── adapters/              # Один модуль на биржу
│   │   ├── base.py            # ABC ExchangeAdapter
│   │   ├── hyperliquid.py     # Hyperliquid REST + WS
│   │   └── nado.py            # Nado public REST/archive market data
│   ├── core/
│   │   ├── scanner.py         # Polling funding rates, поиск спредов
│   │   ├── executor.py        # Атомарное открытие/закрытие пар позиций
│   │   ├── risk.py            # Лимиты, circuit breaker, delta-check
│   │   └── pnl_tracker.py     # Учёт funding payments, fees, net PnL
│   ├── config.py              # pydantic-settings + .env
│   ├── models.py              # Dataclasses: FundingRate, Position, Trade, etc.
│   ├── db.py                  # SQLite через aiosqlite
│   ├── notifier.py            # Telegram алерты (опционально)
│   └── main.py                # asyncio event loop, graceful shutdown
├── tests/
│   ├── adapters/
│   │   ├── test_hyperliquid.py
│   │   └── test_nado.py
│   ├── core/
│   │   ├── test_scanner.py
│   │   └── test_risk.py
│   └── conftest.py
├── data/                      # SQLite файл (gitignored)
├── ARCHITECTURE.md
├── CLAUDE.md
├── .env.example
├── .env                       # gitignored
├── .gitignore
└── pyproject.toml
```

---

## Компоненты

### ExchangeAdapter (base.py)

Абстрактный интерфейс, который должна реализовать каждая биржа:

```
get_funding_rate(symbol)      → FundingRate
get_funding_rates()           → list[FundingRate]   # все символы
get_orderbook(symbol, depth)  → Orderbook
get_balance()                 → Balance
get_positions()               → list[Position]
place_order(order)            → OrderResult
cancel_order(order_id)        → bool
get_fee_rate(symbol)          → FeeRate
subscribe_funding(callback)   → WebSocket subscription
```

### Scanner (scanner.py)

```
Каждые SCAN_INTERVAL_SEC:
  1. Запрашивает funding rates со всех адаптеров параллельно
  2. Строит матрицу спредов по общим символам
  3. Фильтрует: spread > MIN_FUNDING_SPREAD
  4. Проверяет: net_pnl > MIN_NET_PNL_AFTER_FEES (с учётом комиссий)
  5. Отправляет FundingSpread в очередь Executor
```

### Executor (executor.py)

```
При получении FundingSpread:
  1. Проверяет риск-лимиты (risk.py)
  2. Проверяет ликвидность (slippage estimate из orderbook)
  3. Одновременно открывает обе ноги (asyncio.gather)
  4. Если одна нога не исполнилась → откатывает вторую
  5. Записывает позицию в БД
```

### Risk Manager (risk.py)

```
Лимиты:
  - MAX_POSITION_PCT         % от капитала на одну ногу
  - MAX_OPEN_PAIRS           макс. количество пар
  - LEVERAGE                 кредитное плечо (применяется к обеим ногам)
  - DELTA_STOP_LOSS_PCT      закрыть если ноги разошлись > N%
  - CIRCUIT_BREAKER_FAILURES пауза после N ошибок подряд
  - MAX_HOLD_HOURS            принудительное закрытие через N часов

Circuit breaker: exponential backoff (30s → 60s → 120s → ...)
```

### PnL Tracker (pnl_tracker.py)

```
Каждые MONITOR_INTERVAL_SEC:
  - В paper оценивает funding по ставке и фактически прошедшему времени
  - Суммирует fee paid при открытии/закрытии
  - Вычисляет P&L = funding + ценовой P&L обеих ног - комиссии
  - Проверяет delta (цена разошлась между биржами?)
  - Записывает в SQLite
```

---

## Режимы работы

| Режим       | Описание                                              |
|-------------|-------------------------------------------------------|
| `dry_run`   | Только сканирование, ордера не отправляются           |
| `paper`     | Виртуальный портфель, реальные цены, без ордеров      |
| `live`      | Заблокирован до реализации подтверждений и сверки Nado |

---

## База данных (SQLite)

```sql
positions    -- открытые арбитражные пары
trades       -- история всех ордеров
funding_log  -- история funding payments по каждой ноге
pnl_summary  -- дневной/недельный P&L снэпшот
```

---

## Жизненный цикл позиции

```
DISCOVERED → OPENING → OPEN → MONITORING → CLOSING → CLOSED
                 ↓ (одна нога не исполнилась)
             ROLLBACK
```

---

## API эндпоинты

### Hyperliquid
- REST: `https://api.hyperliquid.xyz/info`
- WS:   `wss://api.hyperliquid.xyz/ws`
- SDK:  `hyperliquid-python-sdk`

### Nado
- REST: `https://gateway.prod.nado.xyz/v1`
- WS:   `wss://gateway.prod.nado.xyz/v1/ws`
- Subscriptions WS: `wss://gateway.prod.nado.xyz/v1/subscribe`
- Сеть: Ink L2 (Kraken Ethereum L2)
- Auth: подпись кошельком (EIP-712)

---

## Rate Limits (предварительно)

| Биржа       | REST            | WebSocket           |
|-------------|-----------------|---------------------|
| Hyperliquid | 1200 req/min    | без явных лимитов   |
| Nado        | TBD (из доков)  | 5 WS / кошелёк      |

---

## Итерации (Milestones)

### M1 — Каркас + Сканер (dry-run only)
- Структура проекта, модели, конфиг
- HyperliquidAdapter: funding rates + orderbook (read-only)
- NadoAdapter: funding rates + orderbook (read-only)
- Scanner: печатает таблицу спредов в консоль
- Тесты с моками HTTP

### M2 — Исполнение (paper trading)
- Executor: открытие/закрытие виртуальных позиций
- Risk Manager: все лимиты
- SQLite: персистентность
- PnL Tracker

### M3 — Live trading
- Реальные ордера через оба адаптера
- Graceful shutdown
- Telegram алерты
- Мониторинг delta, ребалансировка
