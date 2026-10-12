# Spot long + perpetual short

`HYPERLIQUID_SPOT_ENABLED=true` adds public Hyperliquid spot markets quoted in
USDC to the scanner. It compares each unambiguous spot token with perpetual
markets on the connected exchanges. For a spot/perp pair the spot is always
the long leg, the perpetual is always the short leg, and the perpetual funding
must be positive. The spot funding rate is zero.

The paper entry buys spot at asks and sells the same base quantity of the
perpetual at bids. Exit sells spot at bids and buys back the perpetual at asks.
Both legs use observed order book depth and taker fees. The scanner estimates
funding over `EXPECTED_HOLD_HOURS` and deducts round-trip fees; the risk check
also deducts estimated round-trip slippage. Monitoring follows the existing
funding, price divergence, maximum holding time and re-entry cooldown rules.
The spot leg is fully funded even when `LEVERAGE` is greater than one.

The spot fee defaults, `0.0007` taker and `0.0004` maker, come from the base
Hyperliquid spot tier. They can be overridden using
`HYPERLIQUID_SPOT_TAKER_FEE` and `HYPERLIQUID_SPOT_MAKER_FEE` if the account's
actual fees differ.

Only exact token names are paired automatically. For example, Hyperliquid's
`UBTC/USDC` is not assumed to equal `BTC` without an explicit asset mapping.
Duplicate token names are excluded. The spot adapter only uses public API data
and does not submit live orders.

The displayed projected return is a funding estimate after fees and estimated
slippage. The future closing basis, funding changes and spot/perp price
divergence remain uncertain; paper results are not guaranteed arbitrage profit.
