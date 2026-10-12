# Hibachi

The `hibachi` adapter reads public perpetual-market data from Hibachi's dedicated
data API.

```env
HIBACHI_ENABLED=true
HIBACHI_API_URL=https://api.hibachi.xyz
HIBACHI_DATA_URL=https://data-api.hibachi.xyz
HIBACHI_TAKER_FEE=0.00045
```

Implemented for `dry_run` and `paper`:

- active perpetual contract discovery;
- hourly estimated funding rates;
- order books using each contract's supported granularity;
- current mark prices;
- maker/taker fees published by Hibachi's exchange-info endpoint;
- pairwise comparison with every enabled adapter.

The official public SDK does not expose historical market funding. The scanner
therefore cannot perform its historical spike check for Hibachi, but still applies
fee, liquidity, slippage, freshness, and expected-profit checks.

Live writes are disabled. They require a Hibachi account ID, API key, private-key
signing, and confirmed-fill reconciliation.

Official reference: [Hibachi Python SDK](https://github.com/hibachi-xyz/hibachi_sdk)
