# PHOENIX SPOT — PAPER ONLY

Public spot market data → frozen minute signals → fresh visible-depth hypothetical IOC execution → durable audit ledger. This application cannot create real exchange orders. There is no trading client, key configuration, balance endpoint, funds interface or live-trading switch.

## Frozen research candidate

Start 1000 simulated USDT. Maintain cash >=250, outstanding acquisition basis <=750, <=150 per coin, <=5 simultaneous cycles. Ladder: **8×7, 3×10, 4×16 = 150 USDT** in exactly 15 scheduled purchases if completed; initial 7 and 14 conditional DCA. Rejections do not advance the ladder; a positive partial purchase consumes one rung without topping up. Positions may exit before purchase 15. Dust retains its exact quantity and proportional basis.

Fixed SOLUSDT, AVAXUSDT, ALGOUSDT, HBARUSDT. XMRUSDT stays cash unless the genuine Bybit **spot** instruments endpoint confirms the exact Trading pair, correct base/quote and complete rules. No rotation, derivatives or replacements. Public availability cannot establish the user's account or jurisdiction eligibility.

Entry uses completed-hour close within 1% of the prior 24-hour low and close >= previous close; >=240 contiguous hours and frozen 5-minute feature visibility delay. Wilder ATR14 uses completed hours only. After each fill DCA threshold = fill VWAP - max(.75 ATR, .01 fill). A previously observed trough must rebound 1.5% with no new low. TP arms at 1.016×fee-inclusive average basis and sells on a **later** .2% callback from the old peak. Exit has priority. Minute OHLC is used only for signal semantics; unresolved intraminute ordering remains the frozen original convention.

## Execution contract

Signals arise only from the latest completed minute within 15 seconds of its close. Execution happens at a subsequent observation, during the next actionable minute, using a newly fetched spot book; never candle open. Expired buys are canceled. Sell instructions retry at most once per minute using new books; no historical catch-up fills. Data gaps invalidate old extrema. No same-bar reentry or use of a pre-fill bar to arm the new position.

Books must have correct symbol, ordered positive bid/ask depth, uncrossed prices, and exchange/receipt timestamps <=2 seconds old and not in the future. Walk displayed depth within 15bp of the best quote. The bound, stale limit and partial policy are operational modeling assumptions, not tuned research parameters or guaranteed exchange behavior. Spot base/quote precision, minOrderAmt, maxMarketOrderQty and tickSize are retrieved and frozen in events; refreshed every 30 minutes and expire after one hour. Base-deducted buy fees and quote-deducted sell fees assume 0.1% per side, not actual account fees. Displayed orders may disappear; snapshots do not model matching priority, hidden liquidity or a real IOC acknowledgment. No prior-turnover participation rule is used.

On failure of any supported pair's data, halt new simulated buys globally while continuing fresh-data monitoring and simulated exits of existing inventory. An unsupported XMR does not halt the four supported pairs. A fresh-data position cannot be managed using a missing/stale book. Cash reserve is **not an equity floor**. Floating losses, ladder exhaustion, inventory ages and drawdowns remain visible.

## Deployment

Python 3.12, one service instance, Dockerfile at repository root, public HTTP port **8080**. Use Northflank **Recreate** rollout so the prior worker releases its advisory lock before its replacement starts. Liveness `/healthz` deliberately survives a market-data outage; readiness `/readyz` returns 503 until feeds and durable state are ready. Set Northflank liveness to `/healthz` on 8080. Read-only dashboard `/`, `/api/report`, `/api/state`, `/api/ledger` (latest 50 transaction snapshots), `/metrics`. No mutable HTTP endpoints.

Durable production options:

* Included Northflank private PostgreSQL addon: link its connection URI to runtime variable `DATABASE_URL`. Keep addon private, one replica. PostgreSQL advisory lock ensures one worker. The app stores the ledger/state atomically with synchronous commits. Database secrets never appear in the report.
* A persistent volume mounted at `/data` for SQLite: WAL, FULL synchronous, exclusive process lock, atomic state+ledger commit. Do not deploy SQLite on ephemeral container storage.

If neither option is present, the web dashboard starts but paper processing **fails closed**. `PAPER_ONLY=true` is the only accepted mode; the application refuses supplied exchange keys. The Docker image runs as UID 10001. Database credentials are infrastructure credentials, never exchange credentials. No paid plan or volume is required with the included addon. Current Northflank source uses the public repository; because its GitHub app is not installed on this new repository, source pushes need a manual Northflank build. GitHub Actions still runs on every push.

Each observation transaction has a unique key, hash-chained immutable event row, raw signal bar, hourly feature, book, rules and full after-state. Startup validates the entire hash chain and its terminal state. Duplicate keys do nothing, exceptions roll back, and restarting resumes exact pending orders. A paper position is never reconstructed from an exchange account. The ledger grows continuously: monitor database size in `/api/report`, back up through Northflank, and archive/export before space runs out. No automatic deletion of audit events.

## Validation

`python -m unittest -v` runs synthetic chronology, ladder, budget, partial, stale-data, future-data, rollback and restart tests. CI also starts PostgreSQL 16 and runs the identical durable-store contract against it. `test_postgres` uses `TEST_DATABASE_URL` only for a disposable test database; never set it to the deployment ledger. No network/exchange keys needed for engine tests.

Local manual run: set `LOCAL_PAPER_TEST=1`, `DATA_DIR` to a dedicated local directory, then `python server.py`. This explicit local test flag permits SQLite on a local folder and is never set in the deployment. Local tests do not prove a Northflank volume or database is persistent.

## Preserved research

The local handover includes `research-evidence.zip`: the unchanged 2026-10-24 46-arm report, preregistration, comparison table, frozen engine/auditor source, source manifests and RoyalQ screenshot evidence/interpretation. Its local `RESEARCH_INDEX.json` records hashes and identifies prior full packages. The forensic archive and screenshots are deliberately excluded from this public repository. Original raw minute binaries, complete settings/meta, historical bars, receipts, ledgers and full evidence remain in the recovered frozen local packages. Frozen historical returns retain their candle-execution limitations; no parameters were optimized or backtests rerun here.

Official references: [spot instruments](https://bybit-exchange.github.io/docs/v5/market/instrument), [order book](https://bybit-exchange.github.io/docs/v5/market/orderbook), [klines](https://bybit-exchange.github.io/docs/v5/market/kline), [psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/usage.html).
