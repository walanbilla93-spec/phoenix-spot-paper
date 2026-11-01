# Unified two-confirmation paper research 0.8

The strict seven-gate engine is no longer instantiated. One signal engine accepts a pre-existing OB/FVG plus an independent MSS, sweep/reclaim or exact footprint confirmation. It runs a frozen market-close baseline, a zone-retest limit variant and an ATR wider-stop variant on the same deduplicated signals. No exchange orders or private trading keys.

`trial_protocol.json` is the pre-registered, hash-locked rule set. Baseline price-path functions and two-confirmation detection were copied from the preceding all-95 research without retuning. Stops and exits in the baseline remain unchanged. Newly available live footprint evidence can now confirm signals that could not be checked in the old selective archive.

All pre-entry fields are timestamped, with missing/stale data left unknown. Context is selected before the theoretical baseline close entry; a later receipt-time spread is diagnostic only. REST warmup candles are tagged and their availability is never backdated. No outcome-based filters are applied.

The baseline uses a close-price fill proxy, 5bp adverse fills, 6bp taker fees each side and 10bp funding reserve. Limit entry assumes 2bp maker fees and touch fills, activates at the next full minute and expires after 30 minutes. Queue/fills/actual fee tier/future funding are unverified. Wider stop is max(original distance, 3x pre-entry 5m ATR), with unchanged gross USDT risk and smaller size. Partial targets and breakeven logic are common to all variants.

One coin/direction signal per fixed 5m window; coincident counts at signal and after bucket completion are distinct. BTC H1 EMA20/50 and 24h efficiency define up/down/choppy; missing data is unknown. No single market-regime predicate is presented as validated.

No prospective outcome metrics are exposed during collection. At >=28 days and >=150 deduplicated signals, seal a chronological cohort. Hold back newest 30%, keep a boundary time cohort together, and purge development trades overlapping the test start by 24h. Final results require the original filter lock and matured positions. Censored final trades stop paper research as inconclusive. Nonpositive lower-bound final net R or negative lower-bound gross R triggers the registered kill rule per variant. This never controls exchange settings.

`/paper-variants/report` is development-only after sealing; authenticated `/paper-variants/final` is unavailable until release. The default small archive includes the frozen protocol and a locked report until completion; after completion it adds the trial snapshot, per-trade gross/cost/net CSV and split reports. Raw all-history archives contain underlying public evidence and must not be used to inspect the reserved cohort early.

Reports include fill/nonfill/censor counts, deduplicated setup count and distinct temporal cohorts, net-R WR/PF/expectancy/drawdown and the requested splits. A split with fewer than 30 signals or 30 closed fills is warned. Correlated overlapping probes are not independent statistical trials or a constrained account backtest.

Historical strict records remain untouched in research.sqlite. Active capture uses research_capture.sqlite and prospective paper research uses research_trials.sqlite, each with bounded disk/caches. Restart/gaps censor active plans, not fabricate results. No automatic source deletion. The service remains subject to the existing 2GB disk cap and 512MB deployment limit.

Run `python -m unittest test_recorder test_research_engine test_paper_trials -v`. The local reference parity test checks all 92 previously resolved baseline outcomes and skips when its external reference package is absent.

Frozen protocol SHA256: 9c2822f302bc90ee694072c9e48bc3f298bd54584bd52a6887884594442d6a1d
