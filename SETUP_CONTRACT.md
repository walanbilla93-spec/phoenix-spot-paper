# Setup evidence and review contract v0.6

This is a capture and replay correction. It is not an automatic reproduction of CrypCrab's method and places no orders. Existing trading services and their settings are outside this patch.

## What qualifies as a setup

A provisional OB/FVG approach is a watch, not an entry. A sweep followed by a fresh close break is a review candidate, not proof of MSS. Full qualification requires causally available evidence for Elliott context, higher-timeframe SMC context, the intended POI (OB or FVG as appropriate), inducement/sweep interpretation, MSS, session profile and footprint confirmation. Each evidence item needs its availability time, a reference to the frozen evidence, and a rule version. Unknown, missing, retrospective or revised evidence blocks qualification. Do not invent the trader's unresolved rules or optimize them after seeing outcomes.

Execution needs a separately frozen entry range, stop, target prices, expiry, ladder conditions/sizes and one total risk budget. No price-touch statistic is a trade win. The supplied short videos do not establish all these rules. The review helper deliberately returns no qualified setups until those definitions and annotations exist.

## Capture correction

- Compact exact-traded-price buy/sell volume and trade counts are retained for every observed minute, including outside raw windows. Decimal quantities are stored as strings. This preserves the information needed to reconstruct a session profile and price-level footprint without retaining all raw messages.
- Exact aggregation uses a five-second exchange-minute grace, at most three open minutes per coin, 4,096 levels per minute and 12,000 retained trade IDs per coin. Duplicate IDs inside that retention are removed. Caps, late trades, malformed data, startup and recovery are explicit disqualifying evidence. This is bounded deduplication, not proof of an exchange-complete feed.
- Zero-volume exchange candles account for quiet minutes. Nonzero candle volume must reconcile against footprint volume offline. A missing minute, boot, gap, late correction, cap or incomplete UTC day blocks a complete-session profile. `build_profiles.py` uses an 8 MiB SQLite cache and disk-backed traversal. It does not load whole-history dictionaries or arrays.
- At a provisional watch, store up to 300 already-closed one-minute candles, POI definitions, quote and explicit missing confluences. The archive's hourly metadata supplies additional available context; the local packet alone is not a complete higher-timeframe wave chart.
- Follow each watch for up to sixty minutes, beyond the old ten-minute raw clip. Emit an ordered sweep/close-break review event, POI close invalidation, expiry or gap censoring. Exact minute footprints and continuous candles continue after raw clips end, so subsequent confirmation and price evolution can be reviewed. At most one watch and 300 context candles per coin remain in memory.
- The observable sequence proxy uses two-left/two-right confirmed pivots available before touch, rejects reference levels already broken at touch, then requires a five-bar sweep/reclaim followed on a later closed candle by a fresh close crossing. These are screening definitions, not the trader's Elliott/MSS/inducement rules. All semantic confluences remain visible and unconfirmed.
- Archive parts contain ten sealed files and identify the next part. Full and current-run exports remain available. Streaming payload reads remain bounded at 64 KiB. No old recording is deleted.

## Replay and outcomes

`setup_review.py --clips PATH --output review.json` rechecks the earlier screened clips. Missing context is not fabricated. The outcome helper requires a frozen directional entry, stop, targets and expiry. It flags missing followup, intraminute entries and candles touching both stop and target as unresolved. Its first-target diagnostic is not a full ladder/partial-exit P&L ledger and must not be reported as one. Actual additions require separately confirmed causal evidence and a fill model; neither is inferred from chart touches.

## Existing-record result

The 23 previously screened clips contain three without a zone touch, nine with an already-broken reference, eleven without a temporally unambiguous ordered sweep/close-break proxy. The apparent TNSR sequence is excluded because its sweep candle began before the touch. None is a qualified full-confluence setup. The archived 3,475,413 records contain no new exact-price footprint records; its 13,949 approximate summaries cannot be promoted to exact history. No complete exact session profile is established from this archive by the new validator. Raw clips remain useful for local structure/flow review.

This patch makes future evidence reviewable and prevents counting provisional captures as good setups. It does not establish profitability or complete the unresolved discretionary strategy definitions. No additional collection duration can supply those definitions.
