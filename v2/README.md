# Phoenix V2 RC2 — development implementation

This is a **distinct linear-perpetual paper research engine**, separate from
the existing Phoenix Spot DCA simulator. The root Dockerfile and Spot code are
unchanged. Nothing here connects to an exchange account or submits an order.

## Actual status

Offline chronological replay components and synthetic tests are available.
This is **not a complete, certified RC2 implementation**. V01–V10 remain open;
partial synthetic fidelity evidence does not close V08. There are no verified
backtest results, funded feasibility claims, or active forward paper worker.

The deployment command serves a read-only status page. `/healthz` returns 200
for the web process; `/readyz` intentionally returns 503. A running web process
must not be described as a running paper strategy. Do not configure readiness
as a requirement for this blocked-status deployment or it will never route.

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
* `research.py`: exact prescribed SHA-256 block indices and descriptive
  metrics. It does not implement the complete RC2 acceptance procedure.

## Limits that must be resolved before research use

The complete feasible-path ambiguity propagation, latency/cancel-race model,
full statistical/stress/boundary-cohort orchestration, and independent audit of
all 87 topics are outstanding. OHLC-only batches are quarantined rather than
assigned an invented favorable path. Exact chronology requires independently
archived data and execution quantities, not hand-authored market claims.

There is no bundled historical universe, fee schedule, rule/margin history,
funding stream, mark-price feed or queue/quantity model. The service does not
silently use Spot's assumed fee, balances, database, keys or data. Synthetic
fixture numbers are expressly invented **test inputs**, never Bybit evidence.

## Verification and running

Python 3.12, standard library only:

```
cd v2
python -m unittest discover -s tests -v
python -m phoenix_v2.service
```

Build from repository root with `docker build -f v2/Dockerfile .`.
Deploy only branch `phoenix-v2-rc2`, Dockerfile `v2/Dockerfile`, context `/`,
port 8080, one small instance, no exchange credentials and no Spot database.
The status service stores no trading state and requires no shared volume.

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
