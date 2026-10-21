# Nado farming live orders

The farming process can place signed Nado perpetual orders for each configured
`NADO_ACCOUNTS` entry. The credential must be the EVM private key for the address.
The account uses the `default` Nado subaccount; `transport_url` is optional.

```env
NADO_ACCOUNTS=0xYourWallet:0xYourPrivateKey:http://user:pass@proxy.example:8080
FARMING_INSTANCE_VENUES=nado|entropy
FARMING_MODE=live
FARMING_LEVERAGE=1
FARMING_MAX_OPEN_PAIRS=1
FARMING_NOTIONAL_USD=100
FARMING_LIVE_MAX_NOTIONAL_USD=100
FARMING_LIVE_ACKNOWLEDGE_RISK=true
FARMING_LIVE_PREFLIGHT_ONLY=true
```

During connection the adapter verifies the private key against the wallet,
loads the Nado chain ID, checks the subaccount, and rejects any resting orders
across tradable perpetual markets. Before an order it checks for resting orders
in that market and calculates an IOC limit from the full
book and configured slippage. It signs the EIP-712 order and reports a fill only
after the archive returns the matching digest, account, product, amount, and
nonce. Network timeouts and missing archive confirmation are uncertain results;
the farming executor halts until the account and journal are reconciled.

`FARMING_LIVE_PREFLIGHT_ONLY=true` performs the account, balance, position,
and book checks without sending an order. A funded Nado order has not been
verified in this repository. The main arbitrage process still blocks live
trading. Public Nado gateway queries worked during development, but the local
configuration has no funded Nado credential, so authenticated account and
order checks still need preflight from the machine that will run the bot.
The live notional must also meet the selected market's `min_size`; Nado's
public BTC-PERP metadata currently reports a 100 USDT0 quote minimum.

Official API references: [place order](https://docs.nado.xyz/developer-resources/api/gateway/executes/place-order),
[EIP-712 signing](https://docs.nado.xyz/developer-resources/api/gateway/signing),
[archived fills](https://docs.nado.xyz/developer-resources/api/archive-indexer/orders),
[subaccount state](https://docs.nado.xyz/developer-resources/api/gateway/queries/subaccount-info).
