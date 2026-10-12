# Polymarket Perps

The `polymarket_perps` adapter reads public production market data from
`https://api.perpetuals.polymarket.com` and participates in the pairwise funding scanner.

It loads perpetual instruments, current hourly funding, the next funding timestamp,
order books, mark prices, funding history, and the public fee schedule. Polymarket's
instrument `base_asset` is used as the canonical symbol, so `GOOG-USD` is correctly
compared with `GOOGL` on the other connected exchanges.

The public fee endpoint currently publishes an equity schedule. For categories absent
from that response, the adapter uses the conservative values configured by
`POLYMARKET_PERPS_MAKER_FEE` and `POLYMARKET_PERPS_TAKER_FEE`.

No credentials are required in `dry_run` or `paper`. Live order methods intentionally
raise `NotImplementedError`; restarting the process after enabling the adapter cannot
submit a Polymarket order.

Configuration:

```env
POLYMARKET_PERPS_ENABLED=true
POLYMARKET_PERPS_REST_URL=https://api.perpetuals.polymarket.com
POLYMARKET_PERPS_MAKER_FEE=0.000125
POLYMARKET_PERPS_TAKER_FEE=0.0004
```
