# Phoenix RC3F-1: experimental fast reversal strategy

This is the amended strategy authorized by the owner on 10 November 2026.
It supersedes the conservative RC3 draft for this forward paper experiment.
It is implemented in `fast_strategy.py` and selected by the production service.
The old RC2 implementation remains available for offline comparisons. This
document describes actual rules, not a profitability certification or a promise
of trades. No exchange account, credentials or real orders are used.

## What changes, and why

| Observed weakness | Implemented solution | Trade-off |
|---|---|---|
| Several sequential 4h swings can leave direction neutral for days | Use a completed 1h break of an already confirmed swing; 4h is fallback/context | More false breaks and direction changes are possible |
| Waiting for a frozen multi-swing MSS reference delays reversal recognition | Use the latest known, unbroken confirmed 5m opposing swing available before the sweep | A smaller reference can be less meaningful |
| A fast reclaim and break in one candle could never qualify | Add a separately identified FAST_RECLAIM path at that completed 5m close | Requires forward evidence; a strong close can still fail |
| An already-beyond reclaim could permanently disqualify a good setup | Allow a later displacement close beyond the fixed reference without requiring recross | A later entry may have poor reward/risk and be rejected |
| Every feed interruption erased all useful historical zones and pools | Recover only current objects whose state can be proved from closed extrema; never replay setups or fills | Ambiguous current tails are censored, and held liabilities still block risk |
| Next-native-open prices could predate the actual decision | Activate A from the first evidenced post-decision trade, subject to receipt/deadline checks | Small public prints or delayed processing can produce no fill |
| Harmless simultaneous prints caused broad quarantine | Allow only commuting operations; keep material fill/protection/account ambiguities quarantined | Genuine ambiguous intervals still lose entry eligibility |

These changes are a new hypothesis, not a correction that makes RC2 results
equivalent. Shorter pivots, the 1h direction source, raw MSS anchors and same-bar
confirmation go beyond the earlier Sol High conservative proposal. The owner's
latest instruction expressly permits these amendments. There was no parameter
search using winning trades; no winning trade sample exists for this release.

## Market scope and evidence

Monitor the twelve native Bybit linear USDT perpetuals: BTCUSDT, ETHUSDT,
SOLUSDT, BNBUSDT, AVAXUSDT, XRPUSDT, LINKUSDT, ARBUSDT, UNIUSDT, DOGEUSDT,
SHIB1000USDT and 1000PEPEUSDT. No silent substitutions. All prices, tick sizes,
sizes and P&L use native contract denominations. Liquidity/availability checks
remain mandatory; monitoring a coin does not mean it can enter.

Use completed public candles for 5m, 15m, 1h, 4h and Daily context. Every frame
needs at least 250 observations and positive Wilder ATR(14). References cannot
be used before their availability. Confirmed native 1m candles and exact public
trade reconstruction gate price-sensitive processing. The previous-close native
opening anchor is not an invented first trade. Existing OHLCV/volume thresholds
are unchanged. Half-open UTC candle intervals and late-event handling remain.
Repeated cross sequences are not interpreted as missing consecutive packets.

Official source definitions: [public trades](https://bybit-exchange.github.io/docs/v5/websocket/public/trade)
provide event timestamps, sizes and trade IDs; [confirmed klines](https://bybit-exchange.github.io/docs/v5/websocket/public/kline)
identify completed candles. Neither source proves this simulator's queue
priority or available executable liquidity.

## Direction and structural timing — amended

1. On 5m, 15m, 1h and 4h, confirm strict pivots with two bars on each side.
   Equal highs/lows do not qualify. A pivot becomes available only after the
   second right-hand bar closes. A simultaneous high/low pivot is excluded from
   the alternating directional chain. Daily retains the original strict 3L/3R
   structure policy.
2. Before each 1h candle, sample the latest known raw confirmed unbroken high
   and low. A completed close strictly through the high establishes long
   direction, protected by that known low; reverse for shorts. The completed
   break candle's extreme supplies the known range endpoint. It is not labelled
   a future confirmed pivot. Track previous close breaks so broken references
   cannot be reused as fresh breaks.
3. Keep that regime/range until protected-anchor invalidation or a qualifying
   opposite break. If no valid 1h regime/range exists, use 4h direction. The 4h
   sequence needs the latest two accepted highs and two lows, both rising or
   both falling; no blanket terminal-freezing requirement.
4. An opposing Daily regime vetoes entry. Opposing 4h is context, not a veto
   when 1h direction exists. Premium/discount is measured against the selected
   active 1h range, or the 4h fallback range. A long POI midpoint must be below
   equilibrium; a short midpoint must be above it.

This intentionally trades reversals within an established 1h direction. It
does **not** catch every first countertrend reversal. A strong move that opposes
Daily, has no 1h direction, or lacks a pre-existing POI/pool can still be missed.

## Context, sweep and immutable references

Retain 15m/1h OB or FVG context; both are not required. The original 1.5 ATR
body and 65% body/range displacement test creates context. Context OB breaks
may use a raw confirmed own-frame pivot known before the break candle opened,
without waiting for a frozen multi-swing chain. Previously broken references
are ineligible. OB price bounds, maximum origin distance, FVG formation and
midpoint invalidation are unchanged. POIs need visit one or two; two complete
outside own-frame candles separate visits. A third visit is not eligible.

A long must strictly penetrate eligible sell-side liquidity; a short must
strictly penetrate buy-side liquidity. Touch is insufficient. Pool proximity
to the eligible POI is at most 0.25 own-frame ATR. Snapshot the pre-existing
pool population/version, POI candidate/visit, proximity ATR and latest known
unbroken 5m opposing raw confirmed pivot at the original sweep. A later pivot
or POI cannot retroactively qualify the event. A setup cannot be replaced to
improve its evidence after the fact. The sweep may reclaim within its original
three-candle window. Context/bias changes or a later adverse extreme cancel
or reset the applicable confirmation evidence, as recorded.

## Two confirmation paths — amended

**FAST_RECLAIM:** after an observed sweep, chronological observed trades must
reclaim the reference and break the fixed pre-sweep 5m anchor. Record sweep,
reclaim, break and final adverse-extreme times. The final adverse extreme must
precede the break. At the completed reclaim 5m close, price must be beyond
the fixed anchor; recovery from the sweep extreme must be at least the prior
5m ATR; and the close must be in the terminal 25% of the candle range. The
same post-sweep trade may establish reclaim and break if it crosses both
levels. No intermediate price or historical intrabar fill is fabricated.
The signal exists only after that 5m candle completes.

**DELAYED_MSS:** if fast confirmation fails, preserve the reclaim snapshot.
During the next twelve 5m candles, require a post-reclaim displacement candle
with body at least 1.5 prior ATR and body/range at least 65%, plus a close
beyond the fixed anchor. Displacement can precede the later beyond-level
close. Already being beyond the anchor at reclaim does not veto this path,
and no fresh recross is required. Expiry, POI validity, bias and swept-extreme
protection still apply. No signal is retrospectively inserted.

Both paths use the nearest eligible opposing liquidity as target. Missing
targets reject the setup. Stop is the swept extreme plus a 0.1 current 5m ATR
adverse buffer; target is buffered 0.1 ATR before the liquidity approach.
Neither target nor stop is moved to manufacture 2R. Each arm independently
requires at least 2:1 gross reward/risk at reservation and actual entry.

## Independent Arm A/B paper execution

At the actual decision-completion time, each arm reserves independently using
current eligible native rules and known marked equity. A signal finalized
at or after its 5m boundary plus 60 seconds gets an explicit stale-decision
record and no reservation. No order is backdated to the candle boundary.

Arm A attempts exactly the first observed event strictly after activation.
Its receipt must also follow activation, arrive no later than activation+5s,
precede signal-boundary+60s, and have event-to-receipt age between 0 and 2s.
Both event and receipt must precede the exclusive 60s deadline. Failed timing,
unknown receipt, uncertain first price, insufficient print quantity or poor
geometry means cancellation, not a later favorable retry. Price uses adverse
slippage of max(0.05% of reference, 0.05 ATR) and adverse tick rounding.

Arm B uses an eligible unmitigated 5m execution zone generated by the qualifying
displacement. In the fast path, its displacement must be the reclaim/MSS
candle. In the delayed path, it must follow reclaim. Prefer FVG, then the
existing OB selection policy. Without a B zone, A may operate independently.
B reserves at actual decision time, requires a nonmarketable midpoint limit,
and cannot act on pre-activation events. A touch does not prove a fill:
through-price plus the applicable observed taker side and quantity is required.
The existing one-hour expiry and extreme/zone/target cancellation rules remain.

For both arms, at most 1% of an observed individual public print supplies
experimental quantity, rounded down. A/B are independent counterfactual
accounts, so their quantities are not evidence of combined executable capacity.
Taker fees of 0.055% per side are the disclosed experimental assumption.
Adverse stop fills, partial exits, target through-price, 1.5R break-even request
with one-second amendment latency, and exact funding-rate requirements remain.
Missing funding or held-path evidence remains an unresolved liability.

Event/receipt UTC comparison assumes sufficiently synchronized worker and
exchange clocks. Current server-time skew checks are operational checks, not
a calibrated subsecond clock-uncertainty model. Bid/ask, queue, cancellation
races and real execution capacity remain unverified. These limitations keep
execution research gates open even if a forward paper fill occurs.

## Risk remains unchanged

Each new experiment account starts with 1,000 virtual USDT. Include costs in
sizing; cap individual planned risk at 0.5% of equity, aggregate reserved risk
at 1.5%, and each arm at three occupied/reserved slots. Keep one reservation
per coin/arm, 3x notional/collateral checks, fill-time revalidation and no
cross-arm netting. Lock new risk at a 2% marked-equity daily drawdown or five
consecutive closed net losses, resetting at Sri Lanka midnight. Locks do not
erase exposure. No averaging, leverage upgrade or real orders are introduced.

## Recovery, finite resources and versioned cutover

Reconstruct current valid 15m/1h POIs and untouched standalone pools from at
most 300 closed own-frame bars. Use bar extrema only to prove visits,
invalidation and consumption; do not turn OHLC into an invented tick path.
Preserve known prior visits/mitigation and consumed-member identity across
old/new pivot IDs. Do not guess historical cluster evolution. Censor objects
when a forming own-frame tail lacks complete closed 1m coverage. Preserve
known valid live objects conservatively when their evidence exists.

After quarantine, an affected **flat** coin can recover this provable current
context, but the quarantined minute stays ineligible. Sweeps/setups are never
replayed. Held or pending exposure receives no automatic favorable repair.
Missing held evidence retains the liability and blocks new shared-account
risk until reconciliation. This is a material operational limitation.

The 300-bar context age/lookback is an explicit new restriction. Retire invalid,
consumed and older inactive objects from memory; protect active setup/sweep
POIs and frozen position levels. Preserve Wilder ATR state when clipping
cached bars. Immutable evidence stays in the existing dedicated journal, not
the memory cache. Keep 120,000 global buffered trades, 60,000 per coin-minute,
bounded REST pages, a 4GB journal budget and stop-before-full behavior. A finite
journal cannot archive forever; export/retention needs separate planning.
Do not buy or upgrade resources automatically.

Cutover is allowed only with flat, resolved RC2 books. Archive original balances,
locks, counts, journal head and the exact pre-cutover checkpoint as immutable
evidence. Retain the full historical journal. Start explicitly labelled RC3F
A/B books; never present a reset as recovered P&L. Persist the strategy manifest
hash, and reject an unrecognized profile/hash change. A normal restart must
restore the same books, positions, state and history without another cutover.

## Acceptance and research plan

Software acceptance requires causal long/short fast scenarios, delayed
already-beyond confirmation, failed/ambiguous timing no-fill cases, actual
paper fill/exit/accounting, Arm independence, conservative context recovery,
resource bounds, checkpoint alias preservation and restart tests. These use
synthetic fixtures and never appear in production history.

A same-input public-candle context check compares direction and available
objects. It is **not** a backtest, trade count, missed-trade count or edge test.
Live acceptance checks the exact revision/profile, twelve monitored symbols,
eligible versus quarantined status, current context, persisted prior evidence,
single cutover, fresh minute processing, CPU/RAM/disk/API errors and dashboard.
Real production signals/trades are reported only if they actually occur.

Freeze this version for a prospective observation period. Report every calendar
day, eligible coin-hours, coverage gaps, reset counts, gate rejection counts,
signal delay, qualified distinct setups, reservations, fills and all costs.
The earlier proposal of at least two distinct setups per covered 24h across
the eligible universe is a usefulness objective, not a forced quota. Start
with fourteen fully covered development days (also report elapsed days).
If frequency remains negligible, identify the dominant measured gate before
proposing another named version. Never auto-relax safety or targets to create
activity. Compare net expectancy, drawdown, fill sensitivity and concentration
on adequate independent data; do not select winners from a few daily profits.
Research V01–V10, historical fidelity and profitability remain open.
