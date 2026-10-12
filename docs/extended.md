# Extended Exchange

The `extended` adapter reads public perpetual-market data from Extended on Starknet.

```env
EXTENDED_ENABLED=true
EXTENDED_REST_URL=https://api.starknet.extended.exchange
EXTENDED_WS_URL=wss://api.starknet.extended.exchange
EXTENDED_MAKER_FEE=0
EXTENDED_TAKER_FEE=0.00025
```

Implemented for `dry_run` and `paper`:

- active perpetual-market discovery (spot markets are excluded);
- hourly funding rates and history;
- order books and current mark prices;
- configurable maker/taker fee assumptions;
- pairwise comparison with every enabled adapter.

For RFQ markets, Extended's REST endpoint returns an indicative quote book rather
than resting user orders. Paper slippage on such markets is therefore an estimate.

Live writes are disabled. Extended order submission is asynchronous and requires
an API key, a Stark private key signature, and order-stream fill reconciliation.

Official references:

- [Extended API documentation](https://api.docs.extended.exchange/)
- [Trading fees](https://docs.extended.exchange/extended-resources/trading/trading-fees-and-rebates)
