# Prolonged zero-trade audit, 2026-11-10

This is an operational diagnosis, not profitability research or a parameter optimization.
The prior uninterrupted 27-hour comparison spans epoch 1794222900 to 1794320100:
19,107/19,440 coin-minutes clean (98.287%), but 333 symbol-level context resets.
Those resets comprise 180 same-ms ambiguity, 86 trade-buffer overflow, 54 absent
chronological trade paths, 12 partial reconnect intervals and one contradictory
confirmed native candle. The stream reported 376,875 discarded overflow records.
A high clean-minute percentage did not imply uninterrupted causal setup context.

Across 3,841 clean five-minute coin evaluations, 382 have directional bias, 122
have a live POI and 2,027 have an unconsumed pool. None has a visited POI in the
current bias, or simultaneous bias + live POI + pool at the recorded close.
Ten contracts remain bias-neutral at all recorded clean evaluations. SOL has
319 directional observations, but only three live-POI observations and no
complete observed chain. Snapshot counts are not intrabar rejection attribution;
zero cumulative sweep_start/reclaim/mss/signal/order_reserved events independently
locates the observed bottleneck before order execution.

## Proven implementation correction

The previous ambiguity guard rejected any tied price that touched a live zone.
For example, an OB 100-110 updated by 105.25 and 106.25 has identical valid,
inside, visit, separation and midpoint state in both possible orders. Clearing
all POIs and liquidity for this pair was unnecessary.

Zone.update uses only three predicates: midpoint penetration, outer invalidation,
and visit penetration. If the group's minimum and maximum have the same three
predicate values, every price applies the same idempotent zone operation. All
permutations then have identical zone state. The corrected guard accepts only
that case. Any predicate crossing, any tied unconsumed-pool penetration, existing
sweep/reclaim, pending entry, order or position remains protected. A sweep begun
earlier in the minute at a unique-time pool penetration cannot extend its extreme
inside an accepted tie without also penetrating that same pool, which is still
rejected. Tests cover that prefix and compare both complete paths.

This does not infer an arbitrary ordering, change a POI visit rule, or relax a
strategy threshold. It does not retroactively relabel historical quarantines.
Model-based tests cover all accepted price-pair regions across both sides, OB/FVG,
visit/inside/first-eligible/separation states and verify exact permutation equality
and idempotence. Existing exposure and material boundary cases stay quarantined.

## Bounded buffer headroom

The global in-memory trade ceiling increases from 60,000 to 120,000 records;
the per-symbol minute ceiling increases from 30,000 to 60,000. Both remain hard
limits; overflow is counted and quarantined. A local synthetic resource stress
filled the 120,000-record cap with zero drops, then rejected the extra record.
Incremental process RSS was 48.26 MB. Adding that entire amount conservatively to
the prior production peak gives 245.4 MB, below the existing 512 MB allocation.
This Windows estimate is not a Linux guarantee; deployment RSS must be observed.
No paid resource upgrade, silent pruning or journal reset is authorized/needed.

The feed version changes prospectively and is journaled. Exact native OHLCV,
confirmation, causal timing, strategy, position sizing, targets, stops, account
independence and frozen execution policy remain unchanged. Dashboard readiness
copy now says feed-ready, avoiding a claim that strategy setups are ready.

## Remaining research question

The 4H/Daily context, live 15m/1h POI, sweep/reclaim and frozen 5m MSS chain remains
selective. Operational corrections cannot promise a signal. If frequency remains
inadequate after uninterrupted evidence collection, alternate structural rules
need a separately declared paper experiment and independent evaluation. Selected
chart moves or a desire for activity are not grounds for fabricated fills or
post-hoc profitability claims. V01-V10 remain open.
