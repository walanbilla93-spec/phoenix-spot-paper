# Phoenix V2 RC2 — Change Log

8 November 2026 (Asia/Colombo) | Full correction candidate; final approval pending.

This log maps every F01–F16 finding to exact changed RC2 sections and physical Markdown line starts. All eight P1 and eight P2 items have textual dispositions. Proposed defaults requiring signoff and V evidence gates remain open; this is not a claim of final approval, successful testing or venue feasibility. No strategy code, backtest, optimization, market research or trade was performed. Document assembly and consistency checks do not constitute strategy implementation.

## 1 Source identity and preservation

| Source | SHA-256 | Treatment |
|---|---|---|
| Phoenix_V2_Strategy_Blueprint.md | `a9e1beac2781dde87a1356b849cf261d4326f746f31ac35e755caef33c3a9f71` | Read only; unchanged; working evidence copy retained |
| Phoenix_V2_Decision_Ledger.md | `b3b345ebfa43271e37bcfbbc29c66c9927e03b076bf8c69222b510a2affe3573` | Read only; unchanged; working evidence copy retained |
| Phoenix_V2_Comprehensive_Audit.md | `52c5ecea9dd4aee5b0bbe9d1d2fde8afaa527f99b104ccce261a3787c373440b` | Read only; unchanged; working evidence copy retained |
| Phoenix_V2_Strategy_Blueprint.pdf | `f4d104ed5152480425afb9785155c7dd6c136e39d07b5ae45602f697182c7459` | Read only; unchanged; working evidence copy retained |

RC1 Markdown and ledger identities match those printed in the supplied audit. The PDF identity also matches the audit; it was retained as secondary evidence, not used to reconstruct normative text. Source hashes were checked again after generation. Full 87-topic ledger text was reviewed. Current retrieval authenticated the attached files' identity, not the completeness of an older unabridged conversation/cache. No ledger excerpt was rewritten.

Corrected blueprint identity: `ab2aa99e1dfa852b29ba4ed37e7b971c681bde1ea049866c685203d7dd0f22c0`. Physical line locators below refer to this exact file; named section/clause IDs remain the main reference if a viewer wraps lines.

## 2 F01–F16 exact change map

### F01 — P1

- **Audit class:** Definite decision conflict.
- **Exact changed sections:** §7 U liquidity availability; §8 D13/Overlaps; §8.1 R2D01.
- **Physical line starts:** L132 (Every participating pool must); L156 (**D13 —); L160 (**U — Overlaps:**); L168 (8.1).
- **Correction:** Restore E074 known-before-original-sweep rule for every later participant. Snapshot pool/version population; later confirmations/versions cannot join. Consumption remains global.
- **Authority/disposition:** U restoration; pool-version exclusion is proposed D. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E017 E023 E032 E074. E-numbers are evidence locations, not historical precedence.

### F02 — P1

- **Audit class:** Definite internal conflict.
- **Exact changed sections:** §13 D36 steps 3, 5, 6, 8; §13.1 R2D02.
- **Physical line starts:** L328 (3. If T is Sri Lanka midnight); L330 (5. Apply all known); L331 (6. Evaluate the just-closed); L333 (8. After step 5); L339 (13.1).
- **Correction:** Receive exact-T evidence without converting unresolved modeled entries before veto, lock, risk and expiry stages. Book existing exits, reset once and preserve proven actual fills.
- **Authority/disposition:** Deterministic D correction; actual liabilities never deleted. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E043 E050 E051 E052 E056 E060 E061 E082. E-numbers are evidence locations, not historical precedence.

### F03 — P1

- **Audit class:** Ambiguity.
- **Exact changed sections:** §12 D29; §12.1 R2D03.
- **Physical line starts:** L262 (**U — Slippage:**); L286 (12.1).
- **Correction:** Define normal/gap stop P, causal availability, bar-only opening-gap reference, missing-data handling and one adverse adjustment.
- **Authority/disposition:** Proposed D; V04/V09 remain open. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E054 E056 E062. E-numbers are evidence locations, not historical precedence.

### F04 — P1

- **Audit class:** Ambiguity / lookahead risk.
- **Exact changed sections:** §12 D29/D31; §12.2 R2D04.
- **Physical line starts:** L262 (**U — Slippage:**); L268 (**D31 —); L294 (12.2).
- **Correction:** Store entry-known stop/ATR/fee estimate separately from actual exit-time P/ATR. Historical size never uses eventual exit information.
- **Authority/disposition:** Proposed D completing U cost-inclusive sizing. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E054 E057. E-numbers are evidence locations, not historical precedence.

### F05 — P1

- **Audit class:** Ambiguity.
- **Exact changed sections:** §12.3 R2D05; §13 D36 step 5.
- **Physical line starts:** L300 (12.3); L330 (5. Apply all known).
- **Correction:** Check each arm against 0.5% actual-entry-time equity, monitor pending B and cancel oversized unfilled quantity prospectively; reconcile atomic/race fills.
- **Authority/disposition:** Approval-sensitive proposed D timing restriction. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E044 E048 E057. E-numbers are evidence locations, not historical precedence.

### F06 — P1

- **Audit class:** Ambiguity.
- **Exact changed sections:** §11.1 R2D06; §12 D31/D32; §14.1.
- **Physical line starts:** L246 (11.1); L268 (**D31 —); L272 (**D32 —); L373 (14.1).
- **Correction:** Define TP one-tick/evidence/quantity predicate; preserve residual protection on TP/stop partials; reconcile sibling orders and one completed position.
- **Authority/disposition:** Proposed mechanical D, preserving U full-intended TP; V04/V09. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E044 E062 E065 E066. E-numbers are evidence locations, not historical precedence.

### F07 — P1

- **Audit class:** Ambiguity.
- **Exact changed sections:** §9 D17; §9.2 R2D07.
- **Physical line starts:** L186 (**D17:**); L200 (9.2).
- **Correction:** Reject reclaim already strictly beyond protected MSS level, while preserving later nonqualifying-break treatment.
- **Authority/disposition:** New restrictive proposed D for signoff; not a proven U requirement. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E034 E068 E069 E076. E-numbers are evidence locations, not historical precedence.

### F08 — P1

- **Audit class:** Ambiguity / approval-sensitive default.
- **Exact changed sections:** §3 D04; §3.1 R2D08; §9 D16; §9.1 R2D08.
- **Physical line starts:** L64 (**D04 —); L70 (3.1); L182 (**D16 —); L192 (9.1).
- **Correction:** Specify Daily/4H/5m maturity separately, exact adjacent governing/swept legs, extreme linkage, unique latest candidate, and prospective terminal replacements.
- **Authority/disposition:** All-frozen Daily/4H default retained; 5m scope/association explicitly proposed restrictive D. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E003 E005 E007 E010 E011 E034 E038 E039 E040 E041 E076 E083 E085. E-numbers are evidence locations, not historical precedence.

### F09 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §8 D13; §8.2 R2D09; §13 D36 step 6.
- **Physical line starts:** L156 (**D13 —); L174 (8.2); L331 (6. Evaluate the just-closed).
- **Correction:** Store reservation at penetration, reject/consume occupied-time evidence permanently and install new objects prospectively.
- **Authority/disposition:** Proposed deterministic D preserving U reservation lifecycle. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E024 E056. E-numbers are evidence locations, not historical precedence.

### F10 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §7.1 R2D10; §10 D19.
- **Physical line starts:** L142 (7.1); L212 (**D19 —).
- **Correction:** Separate member consumption from outer cluster sweep/retirement; preserve outer reference and approved growth; exclude partially consumed clusters from new TP selection.
- **Authority/disposition:** TP exclusion is a new restrictive proposed D; U outer-sweep/growth preserved. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E028 E029 E030 E031 E035 E036 E072. E-numbers are evidence locations, not historical precedence.

### F11 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §2.1 R2D11; §11 D27; §17 D51.
- **Physical line starts:** L37 (2.1); L244 (**D27:**); L451 (**D51 —).
- **Correction:** Declare last-trade trigger table, separate verified mark valuation and quote sensitivities; remove executable-side BE inconsistency.
- **Authority/disposition:** Proposed D source convention; quote/mark evidence stays V. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E050 E056 E066. E-numbers are evidence locations, not historical precedence.

### F12 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §2 D02; §2.1 R2D12; §11 D27; §14.1.
- **Physical line starts:** L35 (**D02 —); L37 (2.1); L244 (**D27:**); L373 (14.1).
- **Correction:** Retain exact analytical values; legal-tick BE rounding long down/short up without backward stop movement; store weighted entry separately.
- **Authority/disposition:** Proposed nearest-feasible D convention, not literal exact-entry/net BE. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E058 E063 E066. E-numbers are evidence locations, not historical precedence.

### F13 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §12 D32; §12.4 R2D13; §17 D50.
- **Physical line starts:** L272 (**D32 —); L306 (12.4); L449 (**D50 —).
- **Correction:** Reserve gross pending/open notional and verified collateral, size to smallest budget, prioritize ranked commitments, cancel newest unfilled excess, and distinguish submitted versus actual-filled risk reservation.
- **Authority/disposition:** Proposed D capital/margin restrictions; V03/V09/V10 block funded feasibility. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E044 E045 E048 E049 E057. E-numbers are evidence locations, not historical precedence.

### F14 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §13.1 R2D14; §16 D48 gate 6; §16.1 item 7.
- **Physical line starts:** L339 (13.1); L408 (6. **Execution uncertainty:**); L412 (16.1).
- **Correction:** Freeze local adverse objective, propagate material portfolio branches, synchronize paired scenarios, prohibit subtracting independent minima for superiority, and include fill/rejection ambiguity in an added gate.
- **Authority/disposition:** Proposed D research restrictions; no A/B superiority established. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E056 E078. E-numbers are evidence locations, not historical precedence.

### F15 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §16 D48 gate 3; §16.1 R2D15.
- **Physical line starts:** L405 (3. **Uncertainty:**); L412 (16.1).
- **Correction:** Freeze daily grid, overlapping 14-day blocks, exact index generation, percentile method, pooled estimator, PF zero cases, UTC attribution/removal and sealed common extension.
- **Authority/disposition:** Proposed D research procedure; numeric RC1 gates retained. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E045 E053 E055 E056 E062 E078 (research defaults remain D). E-numbers are evidence locations, not historical precedence.

### F16 — P2

- **Audit class:** Ambiguity.
- **Exact changed sections:** §11 D27; §16 D46/D48 gate 6; §16.2 R2D16.
- **Physical line starts:** L244 (**D27:**); L397 (At the boundary allow); L408 (6. **Execution uncertainty:**); L424 (16.2).
- **Correction:** Stop new entries at scheduled end, follow existing positions to natural closure, report scheduled and finalized cohort gates, and prevent acceptance while liabilities are unresolved.
- **Authority/disposition:** New restrictive proposed D acceptance procedure; no strategy time exit. Textually resolved in this candidate; user signoff is not supplied.
- **Ledger evidence:** E051 E059 E060 E065 E066 E078 (boundary policy is D). E-numbers are evidence locations, not historical precedence.

## 3 Additional bounded editorial changes

- §1/§1.1: replace RC1 completion language with candidate status; correct source-access attribution; state U-over-D precedence and unchanged source control.
- §4 D06: remove unsupported statement that the current request separately selected revalidation B. Its policy stays D under the blanket defaults instruction.
- §13 D38/§17 V09: retain the RC1 venue URL as a verification lead, remove an inherited implication of a new RC2 documentation/account check. No current fee, contract, order capability or account fact is asserted.
- §14.1: add records needed to audit the corrections without overwriting historical evidence.
- §17 introductory status/D50/D51: separate proposed-default signoff from V closure; move liquidation feasibility to the funded research gate as well as later operations; align valuation/source references.
- §18.1/signoff: add invented rule walkthroughs and enumerate approval-sensitive defaults; update prepared version to RC2 and leave every approval/signature/next-phase field blank. Existing six consistency examples are retained.

Original strategy U wording remains verbatim except §7 known-before-sweep availability and §8 overlap eligibility, both amended to restore E074 rather than replace approved logic. The U slippage formula and directions are unchanged inside a mixed U/D paragraph whose D29 was revised. The §18 closing authority statement is restated to describe candidate status, without changing a trading rule. All added operational interpretations are labeled D. The historical E063 illustration is not a second sizing formula: a 5 USDT cost-inclusive risk budget need not imply 5 USDT of initial price-risk cash or 10 USDT of gross reward; the approved gross-2R geometry and cost-inclusive sizing remain separate.

## 4 Proposed defaults requiring signoff

All R2D01–R2D16 are presented for review as a single correction candidate. Particular restrictive choices are: pre-event cluster-version exclusion (R2D01); actual-entry current-equity/cancel policy (R2D05); reclaim-already-beyond rejection (R2D07); all-frozen pre-reclaim 5m maturity/adjacent swept-leg association (R2D08); member-consumed cluster TP exclusion (R2D10); legal-tick BE approximation (R2D12); notional/collateral reservation and partial-entry retained-risk basis (R2D13); broader decision-ambiguity acceptance gate (R2D14); fixed statistical attribution/common extension procedure (R2D15); and two-view/no-unresolved-boundary acceptance (R2D16). They are not individually approved U decisions. Existing D01–D51 also remain defaults, including the 1,000-USDT/3×capital model and all statistical thresholds. No parameter has been optimized or claimed to improve performance.

## 5 Full decision coverage index

All 87 historical topics remain represented by the full corrected blueprint. Locations below use the audit's original section mapping, updated for RC2 correction subsections; original ledger text remains controlling evidence for U substance. This index is coverage/fidelity bookkeeping, not approval or validation.

| Ledger ID | Historical topic | RC2 core sections | RC2 qualification |
|---|---|---|---|
| E001 | POI midpoint placement | §5 | Preserved; general D/V gates apply |
| E002 | Setup range freeze | §5 | Preserved; general D/V gates apply |
| E003 | 4H two-step bias | §3 | F08; see exact map above |
| E004 | 4H neutral transition | §3 | Preserved; general D/V gates apply |
| E005 | Protected 4H anchor | §3 | F08; see exact map above |
| E006 | 4H strict pivots | §3 | Preserved; general D/V gates apply |
| E007 | 4H pivot pairing | §3 | F08; see exact map above |
| E008 | Frozen POI invalidation | §9 | Preserved; general D/V gates apply |
| E009 | 4H location reference | §5 | Preserved; general D/V gates apply |
| E010 | Associated range leg | §5 | F08; see exact map above |
| E011 | Range update timing | §3, §5 | F08; see exact map above |
| E012 | No range, no setup | §4, §5 | Preserved; general D/V gates apply |
| E013 | OB continuous leg | §6 | Preserved; general D/V gates apply |
| E014 | OB dojis | §6 | Preserved; general D/V gates apply |
| E015 | Multiple OB priority | §8 | Preserved; general D/V gates apply |
| E016 | OB/FVG unified priority | §8 | Preserved; general D/V gates apply |
| E017 | POI freeze at reclaim | §8 | F01; see exact map above |
| E018 | First visit penetration | §6 | Preserved; general D/V gates apply |
| E019 | OB origin/break | §6 | Preserved; general D/V gates apply |
| E020 | OB five-candle window | §6 | Preserved; general D/V gates apply |
| E021 | OB fixed boundaries | §6 | Preserved; general D/V gates apply |
| E022 | OB far-wick invalidation | §6, §9 | Preserved; general D/V gates apply |
| E023 | Multi-candle pool merge | §8 | F01; see exact map above |
| E024 | Pre-MSS reservation | §8 | F09; see exact map above |
| E025 | Context FVG displacement | §6, §2 | Preserved; general D/V gates apply |
| E026 | Context 50% invalidation | §6, §9 | Preserved; general D/V gates apply |
| E027 | Two-candle separation | §6 | Preserved; general D/V gates apply |
| E028 | Total cluster width | §7 | F10; see exact map above |
| E029 | Cluster retirement | §7 | F10; see exact map above |
| E030 | Retired cluster TP exclusion | §7, §10 | F10; see exact map above |
| E031 | Individual retirement | §7 | F10; see exact map above |
| E032 | One overlapping event | §8 | F01; see exact map above |
| E033 | Same-type replacement | §3 | Preserved; general D/V gates apply |
| E034 | Frozen protected MSS pivot | §9 | F07, F08; see exact map above |
| E035 | Cluster outer sweep reference | §7, §8 | F10; see exact map above |
| E036 | Cluster membership freeze | §7 | F10; see exact map above |
| E037 | Formation ATR | §7 | Preserved; general D/V gates apply |
| E038 | Protected swing principle | §9 | F08; see exact map above |
| E039 | Protected swing freeze | §9 | F08; see exact map above |
| E040 | Preceding directional structure | §9 | F08; see exact map above |
| E041 | Three highs/three lows | §3, §9 | F08; see exact map above |
| E042 | 5m alternating chain | §3 | Preserved; general D/V gates apply |
| E043 | Five-loss midnight lock | §12 | F02; see exact map above |
| E044 | Pending risk/slot reservation | §12 | F05, F06, F13; see exact map above |
| E045 | Displacement ranking | §9, §12 | F13, F15; see exact map above |
| E046 | Strict execution/liquidity pivots | §3 | Preserved; general D/V gates apply |
| E047 | EQ tolerance | §7 | Preserved; general D/V gates apply |
| E048 | Three slots/1.5% | §12 | F05, F13; see exact map above |
| E049 | One coin per arm | §12 | F13; see exact map above |
| E050 | Marked-equity daily lock | §12 | F02, F11; see exact map above |
| E051 | Cancel entries, retain positions | §12 | F02, F16; see exact map above |
| E052 | Sri Lanka midnight | §2, §13 | F02; see exact map above |
| E053 | Verified fees/fallback | §12 | F15; see exact map above |
| E054 | Adverse slippage | §12 | F03, F04; see exact map above |
| E055 | Actual historical funding | §12, §13 | F15; see exact map above |
| E056 | Finer data/adverse-first | §13 | F02, F03, F09, F11, F14, F15; see exact map above |
| E057 | Cost-inclusive 0.5% sizing | §12 | F04, F05, F13; see exact map above |
| E058 | B frozen midpoint | §11 | F12; see exact map above |
| E059 | B twelve-candle expiry | §11, §13 | F16; see exact map above |
| E060 | B early cancellation | §11 | F02, F16; see exact map above |
| E061 | A next 1m open | §11 | F02; see exact map above |
| E062 | Order-aware costs | §12 | F03, F06, F15; see exact map above |
| E063 | Gross minimum 2R | §10 | F12; see exact map above |
| E064 | MSS ATR TP buffer | §10 | Preserved; general D/V gates apply |
| E065 | Full intended TP exit | §10 | F06, F16; see exact map above |
| E066 | +1.5R entry-price BE | §11 | F06, F11, F12, F16; see exact map above |
| E067 | First FVG/OB fallback | §11 | Preserved; general D/V gates apply |
| E068 | Displacement/MSS timing | §9 | F07; see exact map above |
| E069 | MSS twelve-candle deadline | §9 | F07; see exact map above |
| E070 | Frozen swept extreme invalidation | §9 | Preserved; general D/V gates apply |
| E071 | Structural ATR-buffered SL | §10 | Preserved; general D/V gates apply |
| E072 | Nearest opposing pool | §10 | F10; see exact map above |
| E073 | Reclaim three candles | §8 | Preserved; general D/V gates apply |
| E074 | Known-before-sweep pools | §7, §8 | F01; see exact map above |
| E075 | POI-distance own ATR | §8 | Preserved; general D/V gates apply |
| E076 | Genuine protected MSS | §9 | F07, F08; see exact map above |
| E077 | 1.5 ATR/65% displacement | §2, §9 | Preserved; general D/V gates apply |
| E078 | Independent arms | §2, §§10–12, §16 | F14, F15, F16; see exact map above |
| E079 | First/second POI visit | §6 | Preserved; general D/V gates apply |
| E080 | Daily context/4H direction | §4 | Preserved; general D/V gates apply |
| E081 | Either OB or FVG | §6, §5 | Preserved; general D/V gates apply |
| E082 | Opposing Daily veto | §4 | F02; see exact map above |
| E083 | Daily two-step structure | §3 | F08; see exact map above |
| E084 | Daily neutral transition | §3 | Preserved; general D/V gates apply |
| E085 | Protected Daily anchor | §3 | F08; see exact map above |
| E086 | Daily simultaneous ties | §3, §13 | Preserved; general D/V gates apply |
| E087 | 4H simultaneous ties | §3, §13 | Preserved; general D/V gates apply |

## 6 Review checks and remaining status

Source SHA-256s were unchanged after generation. The full 18-section RC1 body was retained and corrected locally; all original D01–D51 identifiers and all 87 decision topics are present in the review record. All F01–F16 have matching R2D clauses, exact named sections and line locators. The document includes manual rule walkthroughs for the audit counterexamples; no executable strategy tests, data replay or statistical calculation was run. These document checks cannot prove implementation correctness or economic edge.

Open: user signoff on RC2 proposed defaults; V01–V10 evidence; future separately authorized implementation/fidelity checks; separately authorized research. See [Unresolved Verification Checklist](Phoenix_V2_RC2_Unresolved_Verification_Checklist.md). No final approval or next-phase permission is inferred.
