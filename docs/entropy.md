# Entropy adapter

Entropy is a builder-deployed HIP-3 perpetual venue on Hyperliquid. It uses the
same HyperCore info, order-book and exchange APIs as native Hyperliquid perps,
with dex identifier `io`. Wire symbols are prefixed, for example `io:XYZ100`.

The adapter exposes the venue as `entropy` and removes the `io:` prefix before
passing symbols to the scanner. This lets the scanner compare an Entropy market
with the same plain symbol on Hyperliquid or Nado. Symbol intersections are
calculated separately for every venue pair, so an instrument does not need to
exist on all configured venues.

Configuration:

```env
ENTROPY_ENABLED=true
ENTROPY_DEX=io
ENTROPY_MAKER_FEE=0.0003
ENTROPY_TAKER_FEE=0.0009
```

The adapter validates `io` through the Hyperliquid `perpDexs` endpoint during
startup. Metadata and funding use `metaAndAssetCtxs` with `dex=io`; books and
funding history use prefixed wire symbols.

Fee values are explicit paper-trading assumptions. Hyperliquid's user-fee query
does not provide a stable venue-specific HIP-3 breakdown suitable for this bot,
so they are configurable and intentionally conservative.

Entropy inherits the account and order implementation from the Hyperliquid
adapter, including SDK initialization with `perp_dexs=["io"]`. The application
still blocks global `BOT_MODE=live` because the Nado side lacks confirmed fills,
funding payments and reconciliation. Entropy support is therefore enabled for
`dry_run` and `paper`; no real Entropy order is sent by the current application.

References:

- [Hyperliquid HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals)
- [Hyperliquid perpetual info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
- [Entropy](https://entropy.io/)
