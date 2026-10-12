# Математика P&L

## Funding Rate

Funding rate — периодический платёж между лонгами и шортами.

| Знак rate | Кто платит | Кто получает |
|-----------|------------|--------------|
| `rate > 0` | Long | Short |
| `rate < 0` | Short | Long |

### Интервалы котировки

- **Hyperliquid**: API возвращает часовую ставку.
- **Nado**: archive API возвращает ставку за 24 часа. Это период котировки,
  который код использует для нормализации; фактические live-платежи бот пока не учитывает.

### Нормализация к единому интервалу
Для сравнения биржи нормализуем ставки к `per hour`:

```
rate_per_hour = rate / interval_hours
```

## Спред

```
spread = rate_short_per_hour - rate_long_per_hour
```

`spread > 0` означает выгодную возможность (шорт зарабатывает больше, чем платит лонг).

### Аннуализация

```
annualized = spread × 8760   (часов в году)
```

Пример: `spread = 0.0001/h → 0.0001 × 8760 = 87.6%` годовых (до вычета комиссий).

## Оценка funding за время удержания

```
funding_long  = -notional_long × rate_long_per_hour × hours
funding_short = +notional_short × rate_short_per_hour × hours

funding_net = funding_short + funding_long
            ≈ notional × hourly_spread × hours
```

### После комиссий (round-trip)

```
total_fees = (taker_fee_long + taker_fee_short) × 2   # × 2 = open + close
round_trip_slippage_estimate = 2 × (slippage_long + slippage_short)

expected_return = hourly_spread × EXPECTED_HOLD_HOURS
                - total_fees - round_trip_slippage_estimate
```

### Break-even спред

Минимальный спред для покрытия затрат:

```
min_hourly_spread = ((taker_fee_long + taker_fee_short) × 2
                     + round_trip_slippage_estimate) / EXPECTED_HOLD_HOURS
```

Пример (HL taker=0.035%, Nado taker=0.05%):
```
total_cost = (0.00035 + 0.0005) × 2 + 0.001 × 2 = 0.00370
min_hourly_spread при 8h = 0.00370 / 8 = 0.0004625
```

Это 0.04625% в час при сделанных предположениях о комиссиях и проскальзывании.

## Размер позиции

```
notional = min(available_usd_long, available_usd_short) × MAX_POSITION_PCT × LEVERAGE
```

`LEVERAGE` увеличивает notional, но не маржинальное плечо на бирже напрямую.
Реальное плечо на каждой бирже настраивается в `LEVERAGE` параметре.

## Delta (расхождение ног)

```
delta_pct = |mark_price_long - mark_price_short| / avg_mark_price
```

Позиция перестаёт быть delta-neutral когда `delta_pct` растёт.
- **Warning** при `delta_pct ≥ DELTA_STOP_LOSS_PCT × 0.70`
- **Close** при `delta_pct ≥ DELTA_STOP_LOSS_PCT`

## Пример расчёта (реальные числа)

```
Символ: SOL
Hyperliquid funding: +0.0001/h (лонг платит)
Nado funding:        +0.0024/24h = +0.0001/h

Нет, попробуем другой пример:
Hyperliquid: -0.00005/h (лонг ПОЛУЧАЕТ)
Nado:        +0.0018/24h = +0.000075/h

spread = 0.000075 - (-0.00005) = 0.000125/h

Notional = $10,000 (при balance=$100,000, MAX_POSITION_PCT=10%)

funding_net/hour = $10,000 × 0.000125 = $1.25/h = $30/day = $900/month

total_fees = (0.00035 + 0.0005) × 2 × $10,000 = $17.00 (round-trip)
slippage ≈ 0.1% × $10,000 × 2 = $20.00

break_even_periods = $37 / ($10,000 × 0.000125) = 29.6 hours

После 30 часов позиция в прибыли, каждый час после = $1.25 чистой прибыли.
```

## Ссылки на API документацию

- Hyperliquid funding: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Nado symbols and fees: https://docs.nado.xyz/developer-resources/api/symbols
- Nado funding rate: https://docs.nado.xyz/developer-resources/api/archive-indexer/funding-rate
