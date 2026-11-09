# Native public-feed reconciliation

This changes market-data validation and evidence observability only. MSS, sweep,
POI construction, entries, SL/TP, sizing, risk controls, account independence and
the frozen experimental execution policy are not changed. V01–V10 remain open.

## Evidence and root cause

The original validator equated native candle open with the first trade whose
fill timestamp falls inside the minute. Read-only analysis of the saved actual
twelve-market collection found 51 open-only mismatches: all 51 native opens equal
the preceding native close and preceding last observed trade. Seven additional
open/high or open/low mismatches are explained by including that same boundary
anchor in the high/low range. Volume discrepancies and empty paths are separate.

A fresh simultaneous public trade / native 1m WebSocket capture produced 24
fully observed coin-minutes (two per requested contract). All 24 closed native
candles agreed with REST and were exactly reconstructed by:

```
O = previous native close
H = max(previous native close, observed trade prices)
L = min(previous native close, observed trade prices)
C = final observed trade price
V = sum(unique observed trade quantities)
```

Only 18/24 matched the old first-trade-open formula. This is an **empirically
verified venue convention**, not a claim that the official API text explicitly
promises carry-forward opens for every product or historical period. The docs
define native open/high/low/close, base-coin volume, fill timestamp and candle
confirmation; they do not specify this reconstruction formula. Consequently an
anchor-based acceptance requires independent closed native confirmation for
that exact coin and minute, plus exact REST agreement and a contiguous preceding
native close. No epsilon, rounding exception, price overwrite or fabricated
price/volume point is permitted.

Official references:

- https://bybit-exchange.github.io/docs/v5/market/kline
- https://bybit-exchange.github.io/docs/v5/websocket/public/kline
- https://bybit-exchange.github.io/docs/v5/websocket/public/trade
- https://bybit-exchange.github.io/docs/v5/market/recent-trade

The captured public fixture is `tests/fixtures/bybit_native_20261109.json.gz`:
24 native confirmed candles, 8,361 actual public trades, native denominations
unchanged. SHA-256:
`da923edc0862692ce4f9341fa56ce266bb3c83205bc82e9d0f60a9be9ebb93d9`.
It identifies its source wire archive hash and timestamps. It contains market
observations, not simulated fills or invented strategy results.

## Boundaries, ordering and completeness

Trade fill timestamp `T` is bucketed into `[start, start+60000)` milliseconds.
Native WebSocket `end` is inclusive (`start+59999`); only `confirm=true` rows are
accepted. Forming candles never substitute for final evidence. Subscription
coverage begins at the subscription acknowledgment, not socket establishment.

Trade IDs, not `seq`, deduplicate records. Bybit explicitly allows multiple
messages for the same cross sequence; numerical gaps are not assumed to be lost
per-symbol packets. Sequence regressions are quarantined and counted. Equal-time
material price ambiguity keeps its existing conservative quarantine behavior.
Native confirmations are bounded in memory and archived with committed minutes.
RPI/block flags are retained without speculative filtering.

REST backfill repairs missing native bars and marks, not an unavailable complete
trade path. The recent-trades REST endpoint has a bounded most-recent list rather
than an arbitrary historical cursor; it cannot prove complete BTC minute history
after a long outage. Missing/partial trade intervals remain quarantined.

A carried opening anchor is a native chart boundary value, **not an observed
trade at the new boundary**. If there is a pending actual A/B opening activation
and the first trade differs from that reference, the minute remains quarantined
(`opening_trade_reference_ambiguous`). This preserves the prior absence of a fill
on such intervals. The existing entry and execution routines remain unchanged.

## Audit and operational limits

Each minute records reconciliation version/mode, differing OHLCV fields, previous
close and exact reconstruction when used, source sequence summary, native candle
receipt, trade-evidence hash and stream health. Changing reconciliation emits one
`feed_reconciliation_changed` event and preserves the original policy, accounts,
history and structural state. Old evidence is never rewritten or relabeled.

`GET /api/evidence?symbol=BTCUSDT&start=<aligned UTC seconds>` returns at most one
minute of immutable trade, candle, mark and native-confirmation evidence with
stored/canonical hashes. Only configured symbols are allowed; pages over 8 MB
return 413. There is no write endpoint. This supports examination of pre-update
production archives without exporting credentials or opening a shell.

Late trades received after a minute is sealed are retained separately and
quarantine affected prospective state. They never silently reopen a committed
minute or become historical fills. The late queue caps at 1,000 records; overflow
is an explicit fatal resource stop. Trade/native buffer evictions, lag, sequence
regressions and overflow are visible and minute stream-health evidence is saved.
Automatic repair of previously affected closed outcomes is not certified.

Existing native denomination, memory/storage limits, request pacing, cooldowns,
single-writer persistence and independent Arm A/B accounts remain in force.
The short paired capture and live before/after windows do not prove reliability
through every market condition or close any research gate.

## Read-only entry context

Status now exposes `entry_context` for each native contract. The same snapshot is
included in future five-minute `signal_evaluation` events and displayed under
"Why no trade? Current setup progress". It reports the existing 4H/daily regimes,
computed bias, available valid 15m/1H POIs, POIs currently inside a first/second
visit in that bias, unconsumed pools and active sweeps/reclaims. These are current
prerequisites, not a replacement evaluator or a retroactive missed-entry verdict.
No threshold or strategy code is changed. Older evaluation records remain intact
and do not acquire invented historical context.

Repeated quarantine clears price-sensitive POIs, pools and setups prospectively;
closed-candle structures remain warmed. Consequently uptime and feed readiness do
not imply a live qualifying sweep/reclaim/MSS chain. Historical bootstrap POIs
remain excluded because their live visits/consumption were not observed. A 5m
TradingView MSS label alone does not establish this engine's full 4H/daily, live
POI, sweep/reclaim, causal MSS and risk/target requirements. PEPE screenshot prices
are per-token; the monitored 1000PEPEUSDT price is in native thousand-token units.
