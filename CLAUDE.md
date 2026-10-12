# Funding Rate Arbitrage Bot — Контекст для Claude

## Что это

Delta-neutral арбитраж ставок финансирования между **Hyperliquid** и **Nado DEX** (Ink L2).
Стратегия: LONG на бирже с низким funding + SHORT на бирже с высоким funding.

## Стек

- Python 3.11+, `asyncio`, `aiohttp`
- `pydantic-settings` для конфигурации (всё из `.env`)
- `structlog` для логирования в JSON
- `aiosqlite` для персистентности
- `hyperliquid-python-sdk` для Hyperliquid

## Критические правила

1. **Никогда не коммитить `.env`** — там приватные ключи
2. **Все денежные величины — `Decimal`**, не `float`
3. **Никаких hardcoded ключей, адресов, символов** — всё через `Settings`
4. **Dry-run по умолчанию** — `BOT_MODE=dry_run` в `.env.example`
5. **Формулы P&L комментировать** со ссылками на API документацию

## Архитектура

```
ExchangeAdapter (base.py)  ←  HyperliquidAdapter, NadoAdapter
       ↓
FundingScanner             ←  запрашивает все биржи параллельно
       ↓
RiskManager                ←  проверяет лимиты, circuit breaker
       ↓
Executor                   ←  открывает/закрывает пары (M2+)
       ↓
PnlTracker + SQLite        ←  учёт P&L (M2+)
```

## API эндпоинты

### Hyperliquid
- REST: POST `https://api.hyperliquid.xyz/info` с JSON body `{"type": "..."}`
- SDK: `hyperliquid-python-sdk`

### Nado
- Symbols: GET `https://gateway.prod.nado.xyz/v1/symbols`
- Market queries: GET `/v1/query?type=<query_type>`
- Funding: POST `https://archive.prod.nado.xyz/v1` с `funding_rates`
- REST executes: POST `https://gateway.prod.nado.xyz/v1/execute` (EIP-712 подпись)
- WS: `wss://gateway.prod.nado.xyz/v1/ws`
- Subscriptions: `wss://gateway.prod.nado.xyz/v1/subscribe`
- Docs: https://docs.nado.xyz/developer-resources/api

## Конфиг (ключевые параметры)

| Параметр | Дефолт | Описание |
|---|---|---|
| `BOT_MODE` | `dry_run` | dry_run / paper; live пока намеренно заблокирован |
| `MAX_POSITION_PCT` | `0.10` | % от капитала на одну ногу |
| `LEVERAGE` | `1.0` | кредитное плечо |
| `MIN_FUNDING_SPREAD` | `0.0001` | мин. спред для входа |
| `EXIT_FUNDING_SPREAD` | `0.00005` | закрыть когда спред упал до этого |
| `SHUTDOWN_MODE` | `close_positions` | немедленно закрыть все позиции |

## Milestone план

- **M1** (текущий): сканер + dry-run, только чтение данных
- **M2**: paper trading, executor, risk manager, SQLite
- **M3**: завершить Nado live API, сверку fills/payments и тестовую интеграцию

## Запуск

```bash
cp .env.example .env
pip install -e ".[dev]"
python -m src.main
```

## Тесты

```bash
pytest tests/ -v
```

## Что НЕ делать

- Не добавлять торговлю в M1 (только чтение данных с бирж)
- Не использовать `float` для денег
- Не хардкодить символы — только через `SYMBOLS=auto` или `SYMBOLS=BTC,ETH`
- Не пропускать проверку rate limits при добавлении новых запросов
