# Confluence paper research 0.7

Isolated `data/market-recorder` branch; Phoenix and Orayan unchanged. No exchange keys or real orders.

This release implements measured seven-layer qualification and a durable hypothetical execution ledger. `research_policy.json` freezes our explicit research interpretation, not the trader's private method. Elliott, SMC, OB/FVG, inducement, MSS, previous-session profile and footprint remain separate mandatory gates. Unknown evidence rejects the setup.

Paper execution includes next-quote latency, spread and displayed-size checks, a fixed 2.50 USDT campaign risk budget, separately confirmed middle/far additions, partial targets, stop gaps, 24h expiry and explicit model costs. Closed, non-censored net-positive campaigns determine WR. No closed eligible trades means null WR. Restart, feed gaps and invalidated evidence censor or retract outcomes; they never become invented losses or wins.

The dashboard displays measured qualification, fills, outcomes, blockers and per-coin coverage. Its default authenticated download contains a consistent SQLite snapshot, policy and manifest. Other download scopes retain capture archives. `research_replay.py --audit snapshot.sqlite --output audit.json` verifies the ledger. `research_replay.py capture.tar --work NEW_DIRECTORY --output replay.json` replays archive inputs using bounded disk-backed sorting. Supply all required archive parts for a complete replay.

Only eligible contexts and open campaigns retain detailed trades/quotes. Elsewhere, compact candles, reconciled bars and minute receipts remain. The rolling exact profile index covers two UTC days; frozen profiles and decisions persist. No source captures are automatically deleted. RAM buffers, price levels and database sizes are capped. The 2 GB budget includes research files and a 128 MB working reserve.

A complete prior UTC session is required by this model. Startup and partial sessions do not become complete by assertion. This is a known readiness condition visible per coin, not an omitted implementation. Operational success and a synthetic test win do not establish market profitability.

Run `python -m unittest discover -v` for the focused recorder and research tests. The standalone sixteen-coin bound test peaked at 131.78 MiB with all 32 MB prebuffers and 192,000 retained IDs filled; production resource usage must also be checked.

Policy SHA256: 792c37cea32353cd5543a563c614e5006b31b3809ca98f4ffdb60de74587bbed
