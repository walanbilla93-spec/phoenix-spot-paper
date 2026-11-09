# Twelve-market experimental operation

This update operates a paper experiment, not research certification. All ten V
gates remain open. Neither successful tests nor live observation prove an edge.
The existing Spot application, storage, resources and branch are untouched.

## Exact native universe and eligibility

BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, AVAXUSDT, XRPUSDT, LINKUSDT, ARBUSDT,
UNIUSDT, DOGEUSDT, SHIB1000USDT, 1000PEPEUSDT are the only permitted native
symbols. Bybit lists SHIB and PEPE in thousand-token denominations. Quantity,
price and contract multiplier use the venue's native representation throughout;
the dashboard discloses the denomination rather than silently renaming a symbol.

Public instruments, tickers and depth snapshots verify active linear USDT
perpetual availability and current liquidity. Disclosed experimental thresholds:
24h quote turnover at least 5 million USDT, spread at most 15 basis points,
and at least 10,000 USDT depth on **each** side within 10 basis points of mid.
Refresh every 15 minutes; expire after 30 minutes. A stale or failed check blocks
that coin's new entries. These are operational screens, not historical V01/V02
evidence or a guarantee of future fill liquidity. Raw checks are immutable.

Closed native bars and streamed public trades must reconcile exactly. Each
coin has its own clean/quarantined counters and reason; incomplete evidence
does not become a fabricated fill. Timestamp ambiguity involving shared exposure
is quarantined. An affected held position remains an unresolved liability and
blocks new shared-account risk. Arms retain independent balances and journals.

## History and dashboard

The main view shows both virtual accounts and the twelve-coin watchlist. Positions,
trade history and signal/rejection history have separate views and coin/arm
filters. Trades include entry/exit times, weighted exit price, native quantities,
initial/current stop, targets, partial fills, fees, posted funding and net P&L.
Unknown funding or quarantined outcomes display unavailable P&L, never zero.

`GET /api/history?limit=25&before=<cursor>&arm=A&symbol=BTCUSDT` reads a
rebuildable trade projection of immutable events. `GET /api/events` additionally
accepts `category=signals|rejections|logs`. Both paginate through the full retained
journal, with a maximum page size of 100. No write API exists. Projection updates
and authoritative events commit in the same SQLite transaction.

## Resource boundaries and recovery

Public requests are spaced at least 0.7 seconds apart, with bounded timeouts and
rate-limit cooldown. Ordinary minute processing requests 24 candles (trade and
mark for each coin); additional higher-timeframe candles are requested only when
due. Warmup seeds 300 bars on five timeframes per coin, one committed coin at a
time. All twelve share one worker and one websocket connection. Buffer limits,
overflow, retries and failures are visible; accepted evidence is never silently
removed to meet a resource target.

Status measures process CPU across all threads, current/peak RSS, cycle duration,
database allocation growth, API totals/errors/rate limits, per-coin feed counts
and buffer high-water mark. CPU is expressed as vCPU and percent of the existing
0.1 vCPU allocation. Short samples and cold-seed costs must not be presented as
sustained capacity guarantees. Database growth excludes transient WAL size and
starts a fresh measurement after warmup. A 4 GB software budget fits the existing
6 GB volume; at 90%, collection stops explicitly. No paid resources are added.

Restart validates the immutable evidence chain and checkpoint. The explicit
BTC-to-twelve migration preserves existing accounts, pending risk and evidence,
records `universe_expanded` once, and seeds only the added coins at the saved
causal anchor. Missing restart trade paths are quarantined during bounded
backfill. No balance reset, reseed of existing coins or invented replay fills.
