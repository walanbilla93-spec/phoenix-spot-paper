# Market recorder 0.6 — setup evidence

Isolated `data/market-recorder` branch. Do not merge this Dockerfile into Phoenix main. Public Bybit data only: no exchange keys, orders, AI calls or trading-setting changes.

The capture-quality bounds from v0.5 remain. The correction adds bounded exact-price minute footprints, frozen review packets and sixty-minute setup watches, strict offline session validation, and small streamed archive parts. See [SETUP_CONTRACT.md](SETUP_CONTRACT.md) for definitions, unknown confluences, bounds and limitations.

**A provisional zone or a sweep/break review candidate is not a qualified setup.** Elliott/SMC context, intended POI, inducement, MSS, profile, footprint and the execution plan must be frozen causally. No rule is silently treated as satisfied. No profitable strategy is established by recording.

Validate the recorder in isolation:

```sh
python3 -m unittest test_recorder test_selection test_quality test_setup_evidence test_export_parts -v
```

The 28 focused tests cover capture, gaps, bounded aggregation, duplicate/late trade flags, exact Decimal volumes, session eligibility, causal annotations, conservative outcome censoring and archive pagination. A full sixteen-coin buffer and footprint stress check peaked at 117.86 MiB offline. Live resource usage must still be verified.

The earlier 23 screened clips produce one observable sweep/fresh-break proxy and zero qualified full-confluence setups. Run `setup_review.py --clips PATH --output review.json` to reproduce that review. `build_profiles.py capture.tar --database NEW.sqlite --output audit.json` streams archive records through an 8 MiB SQLite cache. Old approximate summaries are not exact-price footprints and are not silently upgraded.

Keep one replica, existing `/data` persistent volume, export token and 2 GB cap. Storage warnings and the visible halt remain; automatic offload/deletion is not installed. Preserve recordings before capacity is exhausted. `/health` is liveness and `/ready` is capture readiness, not strategy or replay certification.
