# Phoenix V2 RC2 — experimental persistent paper worker

This is a **distinct linear-perpetual paper research engine**, separate from
the existing Phoenix Spot DCA simulator. The root Dockerfile and Spot code are
unchanged. Nothing here connects to an exchange account or submits an order.

## Actual status

The deployment starts a persistent public-Bybit worker and a read-only dashboard.
This is **experimental operation, not a complete certified RC2 implementation**.
V01–V10 remain open; synthetic tests and live collection do not certify V08,
an edge, funded feasibility, or profitability. Offline replay still requires
its original reviewed manifest. The worker has a separate, disclosed paper
policy; it does not use the synthetic fixture bypass or manufacture V evidence.

`/healthz` checks worker liveness, `/readyz` checks fresh processing plus a mounted
durable directory, and `/executionz` additionally checks recent complete trade
evidence and unresolved liabilities. A 200 on readiness means the pipeline is
operational, not that a trade qualified. See the separate execution status.

The default experimental scope is **twelve native USDT linear perpetuals**:
BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, AVAXUSDT, XRPUSDT, LINKUSDT, ARBUSDT,
UNIUSDT, DOGEUSDT, SHIB1000USDT and 1000PEPEUSDT. SHIB/PEPE are Bybit's
thousand-token denominations; prices and sizes retain native units. This does
not implement or certify the historical research universe. All
positions, orders, and balances are virtual and independent per arm.
See [TWELVE_MARKETS.md](TWELVE_MARKETS.md) for eligibility and monitoring.

## Components

* `model.py`: exact-decimal arithmetic, UTC candle validation/aggregation,
  Wilder ATR and 250-bar warmup gate.
* `structure.py`: strict 3L/3R pivots, confirmation availability, alternating
  provisional/frozen chains, directional structure and protected anchors.
* `objects.py`, `strategy.py`: OB/FVG objects, visits, liquidity clusters and
  consumption, sweep/reclaim/MSS stages, snapshots and arm execution zones.
* `portfolio.py`: independent A/B virtual accounts; cost-inclusive sizing,
  slots/coin/risk/notional/collateral reservations, actual-entry checks,
  adverse market/stop prices, partial exits, funding and daily/streak locks.
* `engine.py`: synchronized chronological symbol batches and logical stage
  ordering; signals cannot use earlier intrabar confirmation.
* `store.py`: SQLite transactional inputs, decisions and state hashes;
  duplicate protection, rollback and deterministic restart replay.
* `forward.py`, `bybit_public.py`: bounded public REST backfill and trade stream,
  closed own-timeframe history, causal structure updates, explicit experimental
  fill quantities, quarantine, independent arms, and reconnect/error handling.
* `journal.py`, `checkpoint.py`: immutable compressed evidence and hash-chained
  events committed atomically with an allow-listed JSON graph checkpoint.
  No pickle or executable deserialization. A filesystem lock prevents two
  workers writing the dedicated directory.
* `research.py`: exact prescribed SHA-256 block indices and descriptive
  metrics. It does not implement the complete RC2 acceptance procedure.

## Limits that must be resolved before research use

Audited supplied common-market scenario branches can be propagated with local
adverse selection and paired bounds. Automatic OHLC admissible-path construction,
the complete latency/cancel-race model,
full statistical/stress/boundary-cohort orchestration, and independent audit of
all 87 topics are outstanding. OHLC-only batches are quarantined rather than
assigned an invented favorable path. Exact chronology requires independently
archived data and execution quantities, not hand-authored market claims.

There is no certified historical universe, account fee/rule/margin history,
funding coverage, mark audit or queue/quantity model. The worker collects current
public instrument snapshots, mark candles and exact funding rates. Its fee
fallback is the published Standard Crypto regular-user taker rate, 0.055%,
observed 9 November 2026 at https://www.bybit.com/en/fee/futures; taker applies
to every experimental fill, with no MNT discount. This does not close V02.
Current rule snapshots expire after 24 hours and refresh before expiry.
The service does not
silently use Spot's assumed fee, balances, database, keys or data. Synthetic
fixture numbers are expressly invented **test inputs**, never Bybit evidence.

## Verification and running

Python 3.12; the live stream additionally uses pinned websocket-client:

```
cd v2
pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m phoenix_v2.service
```

Build from repository root with `docker build -f v2/Dockerfile .`.
Deploy only branch `phoenix-v2-rc2`, Dockerfile `v2/Dockerfile`, context `/`,
port 8080, one small instance, no exchange credentials and no Spot database.
Mount a **dedicated RC2 directory** at `/data`. The worker checks the mount;
an ordinary ephemeral directory cannot make `/readyz` return 200. Never attach
or reuse a Spot database/volume. For a local development run, set `RC2_DATA_DIR`
to an isolated directory; persistence readiness remains false without a mount.
No web endpoint can change settings, submit a signal, insert a fill, or place an
order. `/api/history` and `/api/events` provide cursor-paginated full trade and
signal/rejection history, with arm/symbol filters. Older bounded raw views remain
available. The mobile dashboard polls status every 15 seconds; research gates,
resource measurements and logs are inside collapsed Advanced Diagnostics.

## Experimental execution and failure contract

The policy is frozen by hash in the database. A policy/scope change fails closed
until an explicit migration or a separate journal is prepared. One narrowly
authorized migration expands the preceding BTC-only experimental policy to the
specified twelve contracts, preserves both accounts and all existing evidence,
and records the old/new policy hashes. Unknown migrations fail closed. Each arm starts
with 1,000 virtual USDT and retains the existing RC2 risk/capacity rules.
On complete, reconciled intervals, modeled fill quantity is at most 1% of an
individual observed public trade, floored to the current quantity increment.
This is a counterfactual participation assumption, **not observed order fills,
queue evidence or guaranteed liquidity**. A has its one next-open attempt;
B requires legal trade-through and rejects marketable activation. Partial entry
remainder cancellation and one-second BE amendment latency are model assumptions;
venue races remain unverified. Baseline market/stop slippage follows RC2.

The trade stream must reproduce exact native OHLCV. A first-trade reconstruction
is accepted as before. An evidenced previous-close opening anchor is also accepted
only when rebuilt OHLCV matches exactly and a closed (`confirm=true`)
Bybit WebSocket candle independently matches REST. No price or volume tolerance
is used; no synthetic anchor trade is inserted. Uncertain opening activation
prices still quarantine pending entries. See [FEED_RECONCILIATION.md](FEED_RECONCILIATION.md).
Disconnects, overflow, late records, missing trades and material timestamp ties
quarantine the interval rather than invent a path. Ties with no possible active
object/execution state transition are irrelevant. Bootstrap warms 300 closed
bars on each own timeframe, then discards all historical POIs/liquidity because
their prior visits/consumption are unknown. After an interruption, the worker
likewise cancels pending risk and clears path-dependent objects prospectively.
It does not relabel unknown historical state as untouched.

The status/dashboard and immutable minute journal include expected versus
observed OHLCV, trade count, first/last trade timestamps and a trade-evidence
hash, so a reconciliation failure is inspectable. Execution readiness is per coin
and requires its latest processed minute to be clean and liquidity-eligible.
An affected held liability blocks shared new risk. An empty quarantined coin
does not erase another coin's valid state. `all_feeds_ready` reports all feeds.

Open liabilities survive missing data and restart. Their affected outcomes are
quarantined, and new risk remains blocked pending explicit evidence repair.
Missing funding never becomes zero: an exact public settlement is required;
the preceding completed minute mark close is only an experimental valuation
approximation. Automatic repair of unresolved liabilities, a full admissible-path
engine, queue/no-fill sensitivity, complete liquidation/margin feasibility and
the original full research protocol remain blockers to certification.

Polling wakes every 10 seconds with <=1.43 REST requests/second, 12-second request
timeouts and capped exponential retry. Backfill processes at most 60 minutes per
cycle with bounded pages. Trade buffers cap at 30,000 trades/symbol/minute and
retain only a short in-memory window, capped at 60,000 records across all coins;
overflow is quarantined. Only due higher-timeframe candles are requested. All accepted
evidence remains immutable on disk. Storage stops before 90% of a 4 GB journal
budget within the existing dedicated 6 GB volume; snapshots cap at 32 MB and
structural objects at 60,000. These ceilings
are explicit operational stops, not silent history truncation. Archive/expand
or prepare an explicit reseed when reached. No auto-purchase or paid scaling.

One instance with 0.1 vCPU/256 MB is the deployment target. SQLite uses WAL and
FULL synchronization. Evidence, per-arm decisions and next state commit together;
on restart the entire evidence hash/decision chain and checkpoint head are checked.
This checks software integrity, not substantive V06 certification. Controlled
restarts can cause brief downtime because the volume is single-writer.

Offline replay, once the affected evidence is reviewed and frozen:

```
python -m phoenix_v2 --manifest verified-manifest.json --input chronology.jsonl --database dedicated-v2.sqlite
```

`manifest.unverified.json` is deliberately unusable for funded simulation.
Each V record must identify source, scope, reviewer, review time, local evidence
path and SHA-256. File presence/hash checks do not certify the evidence's
substance or independently close a gate. The fixture bypass exists only in
direct Python tests and is unavailable through the command line/service.

The JSONL batch interface uses UTC seconds, synchronized closed 1m bars,
independent opening/closing marks, ordered `(at, sequence)` trade points,
explicit evidenced fill quantities and settlement funding. It does not accept
exchange credentials or execute an API call. See tests for synthetic shapes.

The three RC2 source documents are preserved under `spec/`. The user's current
instruction authorizes implementation, testing and deployment; older document
statements about no implementation describe their preparation stage and do
not revoke that authorization. Default authority labels and all external
verification dependencies remain intact.
