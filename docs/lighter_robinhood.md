# Lighter on Robinhood Chain

The `lighter_robinhood` adapter reads the dedicated Lighter Domain deployed on
Robinhood Chain. This domain has separate markets and liquidity from Lighter Core.

```env
LIGHTER_ROBINHOOD_ENABLED=true
LIGHTER_ROBINHOOD_REST_URL=https://api.rh.lighter.xyz
LIGHTER_ROBINHOOD_WS_URL=wss://api.rh.lighter.xyz/stream
```

Implemented for `dry_run` and `paper`:

- active perpetual-market discovery;
- Lighter funding rates, normalized by the scanner from their 8-hour equivalent;
- order books, mark prices, and market-specific maker/taker fees;
- hourly funding history for spike detection;
- pairwise comparison with Hyperliquid, Entropy, and Nado.

Live order placement is intentionally disabled. It requires an account index,
delegated L2 API key, Lighter transaction signing, and confirmed-fill reconciliation.

Official references:

- [Robinhood Chain Lighter Domains](https://docs.robinhood.com/chain/lighter-domains/)
- [Lighter Robinhood API](https://apidocs.rh.lighter.xyz/docs/get-started)
- [Official Lighter Python SDK](https://github.com/elliottech/lighter-python)
