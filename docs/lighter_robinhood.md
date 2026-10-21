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

The farming process can use signed live orders through `lighter-sdk==1.1.6` when
each wallet has a Lighter account index and an API-key index:

```env
LIGHTER_ROBINHOOD_ACCOUNTS=0xWalletAddress:api_private_key:http://user:pass@proxy.example:8080
LIGHTER_ROBINHOOD_ACCOUNT_INDICES=101
LIGHTER_ROBINHOOD_API_KEY_INDICES=2
FARMING_INSTANCE_VENUES=entropy|lighter_robinhood
```

The credential is the delegated Lighter API private key, not the Ethereum wallet
private key. The adapter checks that the configured account index belongs to the
wallet, authenticates and checks for open orders before live trading, then sends
an immediate-or-cancel order and polls `accountOrders` for its actual fill.
`FARMING_MODE=live` still requires the farming live risk switches and a second
funded live venue. The main arbitrage process remains paper-only for Lighter.
No funded Lighter order has been verified in this repository yet.

Official references:

- [Robinhood Chain Lighter Domains](https://docs.robinhood.com/chain/lighter-domains/)
- [Lighter Robinhood API](https://apidocs.rh.lighter.xyz/docs/get-started)
- [Official Lighter Python SDK](https://github.com/elliottech/lighter-python)
