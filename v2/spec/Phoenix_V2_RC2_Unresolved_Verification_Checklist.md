# Phoenix V2 RC2 — Unresolved Verification Checklist

8 November 2026 (Asia/Colombo) | All items below remain open; no execution/testing authorized.

## Signoff gate

- [ ] User records their own RC2 decision, including R2D01–R2D16. In particular review the added restrictions in pool-version eligibility, actual-entry equity, reclaim/MSS timing, structural maturity/leg association, cluster TP eligibility, feasible BE rounding, collateral reservation and research acceptance. These are proposed D defaults, not newly established U approvals. Specification signoff alone does not authorize coding, backtesting, optimization or trading.

## External evidence gates

| Gate | Evidence needed before the affected phase | Closure record |
|---|---|---|
| V01 — contracts/universe | Exact Bybit linear-USDT identities, aliases, listing/delisting history and point-in-time inclusion rule; disclose missing/delisted data | Dated, hashed universe manifest |
| V02 — fees | Effective historical account rates or archived conservative published fallback; justified maker/taker treatment | Fee schedule/version and dates; no guessed rates |
| V03 — ticks/quantity/margin | Historical increments, multipliers, minimum quantity/notional, margin mode/collateral and rounding requirements | Per-contract/per-period rule and feasibility records |
| V04 — fills/data/latency | Chronological execution references, trade/quote/1m coverage, queue/quantity rules for B and TP, gap stops, partial protective fills and amendment latency | Frozen baseline/ambiguity/sensitivity manifest; evidence supporting quantities, not touch-only assumptions |
| V05 — funding | Actual rates, settlement timestamps and valuation basis, including boundary ownership | Signed cashflow records and missing-data reconciliation |
| V06 — stateful data | Immutable hashes, closed-bar calendars/aggregation, gap audit, ATR warmup, prior POI visits and liquidity consumption, timely marks/staleness rule | Data/state audit; 250 bars alone does not certify freshness |
| V07 — untouched window | Actual freeze time, exposure/access inventory, common dates/universe, sealed counts-only extension controls | Preregistered locked manifest; no existing interval certified clean here |
| V08 — causal fidelity | After separate authorization only: event-prefix/reload/future-append checks, audit boundary examples and full accounting reconciliation | Future implementation evidence; none performed |
| V09 — execution/liquidation | Trigger source, real cancel/fill races, reduce-only/OCO-equivalent residual protection, stop availability and liquidation relative to modeled stops | Historical/account-capability evidence; blocks funded feasibility where relevant, not only later operations |
| V10 — capital | Freeze per-arm starting equity, available collateral and order feasibility under minimums and 3×cap | Capital/margin manifest; 1,000 USDT and 3× remain D assumptions |

## Research design and endpoint gates

- [ ] Before any separately authorized observation, freeze §16.1 dates, statistical procedure, resample-index recipe/seed and scheduled-grid artifacts; archive finalized follow-through grid indices mechanically once its length is known. Freeze common scenario/ambiguity treatment and costs/stress. No superiority claim is authorized by the default descriptive A/B comparison; preregister a primary paired estimand first if such a claim is intended.
- [ ] At the scheduled endpoint use the sealed counts-only decision before inspecting performance; take at most the one common six-month extension under R2D15.
- [ ] Report open boundary liabilities and follow only existing position exits under R2D16. No acceptance while positions/cancel races/protective quantities or material scenarios remain unresolved. Missing evidence yields incomplete/inconclusive status, not a zero loss or automatic pass.

The sixteen textual findings have correction clauses in RC2. This checklist records facts and approvals still needed; none has been closed by writing the clauses. RC1 originals remain untouched.
