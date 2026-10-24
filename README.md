# PHOENIX SPOT — PAPER ONLY

Public spot market data → frozen minute signals → fresh visible-depth hypothetical IOC execution → durable audit ledger. This application cannot create real exchange orders. There is no trading client, key configuration, balance endpoint, funds interface or live-trading switch.

## Fixed / weekly rotation option (24 October amendment)

The dashboard offers three independent 1000-USDT paper accounts: **Main — fixed coins + mover replacement**, **Weekly rotation — original discounted entry**, and **Weekly — five-position comparison**. Switching the view never resets or combines money, positions or ledgers. The main account retains the original discounted-entry policy and existing SOL cycle. The optional five-position comparison fills vacant slots from the frozen weekly selection without the initial discount filter; it still requires completed-hour features, current instruments, fresh books, funds and every risk cap. Its target is five active positions, never more than five, and outages/rejections may leave fewer temporarily. DCA and exit rules are common and unchanged. The comparison has a separate entry-policy hash and is not presented as a proven improvement.

Fixed keeps the original PostgreSQL tables. Weekly uses `weekly_*` tables and the five-position comparison uses `five_*`, each with an independent advisory lock, hash chain, cash and positions. This needs no expansion of the managed database user's permissions. `service_v2.py` is the current entrypoint; `server.py` and its original dashboard remain preserved for historical reference.

By explicit user amendment, replace XMR's unavailable spot slot with the highest eligible mover outside SOL/AVAX/ALGO/HBAR from the earlier researched top-25 cohort. **TRX is excluded** from replacement and weekly rotation. The replacement is frozen after first assignment, recorded as an append-only amendment; no forced purchase or position transfer occurs. XMR's empty historical record remains in the ledger. This supersedes the original XMR-cash-only instruction below.

The weekly selector uses the original dated 25-name research cohort (22 acquired pairs plus TON/LEO/OKB unresolved names), not today's market-cap top 25. Every candidate requires current genuine Bybit spot instrument support. No derivatives, ticker aliases or survivor substitutions. MATIC's inherited migration exclusion remains. Selection runs Monday 00:05 UTC from the prior 30 complete UTC days: >=99.9% native 5-minute continuity, all 720 hours plus warmup, median daily quote turnover >=1m, median hourly Wilder ATR/close in 0.3%-3%. Rank descending median ATR ratio, clipped EMA200 structure, median turnover, then symbol ascending. Top five admit new cycles. Dropped positions continue their existing DCA and exits and occupy slots; rotation never forces a sale. Missing or stale selection prevents new cycles, while fresh-data management of retained positions continues.

At initial deployment of this option, the latest already available Monday boundary is acquired; paper activity starts prospectively now, with no retroactive fills. Raw selector candles, receipt metadata and hashes are retained in the private database and cached inputs are hash-checked. The schedule and fixed replacement are committed atomically with their own event records. The original execution config hash remains unchanged for ledger compatibility; the allocation amendment has a separate `selection_config_hash`.

Read `/api/report?mode=fixed`, `?mode=weekly` or `?mode=weekly_five`; `/api/ledger?mode=...`, `/readyz?mode=...`, and `/api/selection`. The HTTP API remains read-only. The dashboard explains waiting reasons, shows the active count out of five, identifies **persistent PostgreSQL**, and exposes weekly rankings/exclusions. Main/original-entry weekly selections wait for discounted entry; the separately labeled five-position comparison fills vacant slots using the next fresh eligible observation.

## Forward execution amendment (2026-10-24)

The active service now records `execution_policy_hash` separately from the original research config. At startup it appends one deduplicated policy amendment to each account, cancels legacy queued buys, and preserves balances, inventory, dust, cycle ages and historical events. Historical replay without this amendment retains the old execution semantics; production always installs the amendment before observing data. These guards change prospective behavior, so frozen research results do not validate the amended strategy.

Every DCA depth level must be strictly below the previous purchase VWAP and below fee-adjusted existing average cost, rounded conservatively to the instrument tick. A positive fill must reduce the fee-inclusive average; rejection consumes no rung. The ATR trigger and 1.5% old-trough rebound remain unchanged. A falling price keeps updating the trough and does not cause additional buys until a later qualifying rebound. TP arming takes priority over queued/new DCA. Each TP sell level must produce positive modeled proceeds after the assumed sell fee relative to proportional remaining basis. A downward gap below break-even cancels that profit intent and resets extrema, allowing DCA to resume on fresh observations. This is not a stop-loss or guaranteed executable exit. All purchase counts, including the initial buy, may take profit; lot minimums and dust still apply.

Every initial buy, including the five-slot comparison, is capped at 1.01 times the completed-hour prior-24h low at actual book execution. The five-slot comparison continues to bypass the hourly stabilization signal, but no longer bypasses this actual-price ceiling. Five active cycles remain a target, not a forced-buy minimum. No historical buys are rewritten.

Click a coin/position row to view all saved fills: Sri Lanka time (Asia/Colombo, UTC+05:30), price, USDT spent/gross proceeds, net coin quantity, fee in its charged currency plus USDT equivalent, fill status and ledger reference. The modal shows remaining cost, average, TP arm and net break-even. `/api/position?mode=fixed&symbol=SOLUSDT` is read-only and reads the full stored history, unlike the recent-event endpoint. It labels current-cycle versus earlier fills. All human-readable dashboard timestamps use Sri Lanka time; raw epoch timestamps remain unchanged for replay. Cycle age uses days:hours:minutes. Fees are assumptions; current spot rates and account eligibility remain unverified. The 1.6% arm is unchanged and is not a claim of overall profitability.

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

Local manual run: set `LOCAL_PAPER_TEST=1`, `DATA_DIR` to a dedicated local directory, then `python service_v2.py`. This explicit local test flag permits SQLite on a local folder and is never set in the deployment. Local tests do not prove a Northflank volume or database is persistent.

## Preserved research

The local handover includes `research-evidence.zip`: the unchanged 2026-10-24 46-arm report, preregistration, comparison table, frozen engine/auditor source, source manifests and RoyalQ screenshot evidence/interpretation. Its local `RESEARCH_INDEX.json` records hashes and identifies prior full packages. The forensic archive and screenshots are deliberately excluded from this public repository. Original raw minute binaries, complete settings/meta, historical bars, receipts, ledgers and full evidence remain in the recovered frozen local packages. Frozen historical returns retain their candle-execution limitations; no parameters were optimized or backtests rerun here.

Official references: [spot instruments](https://bybit-exchange.github.io/docs/v5/market/instrument), [order book](https://bybit-exchange.github.io/docs/v5/market/orderbook), [klines](https://bybit-exchange.github.io/docs/v5/market/kline), [psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/usage.html).

## Market display

Each coin row shows Bybit spot lastPrice and rolling price24hPcnt x100. A shared display-only worker polls /v5/market/tickers?category=spot approximately every ten seconds. The dashboard refreshes every five seconds; values carry snapshot age and become last-known/stale after 30 seconds or a feed failure. Missing/invalid data is unavailable, never a fabricated zero. Prices are for reference, distinct from saved bid-based valuation marks and fresh executable depth. This worker never modifies the ledger, marks, selection or execution decisions. All historical strategy hashes and balances are preserved.

Phoenix submits no resting orders ahead of a target. Completed one-minute candles generate signals; a later observation uses a fresh spot book for a bounded hypothetical marketable IOC fill. Buys walk asks; sells walk bids. Hourly ATR and entry features use completed hours. A trigger/TP arm is not a fill; insufficient depth or a price guard can leave the position unchanged. This remains a public-data paper simulator, not proof of exchange matching.
