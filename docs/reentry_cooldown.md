# Re-entry protection and loss analysis (2026-09-25)

The saved history contains 17 SAMSUNG and 13 SKHYNIX closed paper positions.
Their holding times were 4-63 seconds: funding totaled approximately $0.08,
fees $36.17, and realized price P&L was approximately -$15.33.
SAGA also incurred approximately -$33.13 in realized price P&L.
These are simulated results, not confirmed exchange payments.

Old positions do not contain a close reason. Their precise exit trigger cannot
be reconstructed from the database alone. The monitor immediately exits on
missing funding data or unavailable mark prices, as well as risk triggers.
The existing entry checks did not validate mark-price availability or the
mark-price divergence used by the monitor's stop rule.

Changes:
- Entry now requires valid positive finite mark prices on both exchanges and
  divergence below DELTA_STOP_LOSS_PCT (the same measure as the monitor).
- REENTRY_COOLDOWN_SEC=1800 blocks a symbol across all exchange pairs for
  30 minutes after a fully completed close, separately for each bot mode.
  The last close is read from SQLite; restart does not bypass the cooldown.
  Partial/failed closes remain under the existing exposure recovery logic.
  A value of zero disables the cooldown.
- The original close reason is persisted in state_json, preserved during close
  retries, and logged with the final P&L.

The running process must be restarted to load the changes. Existing historical
P&L and orders are not rewritten. The cooldown reduces repeated transaction
costs; it cannot prevent losses from future price or funding changes.
