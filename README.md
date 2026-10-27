# Market recorder 0.5 — capture-quality repair

Isolated `data/market-recorder` branch only. Do not merge the recorder Dockerfile into Phoenix main. Public Bybit data only; no private exchange keys, live orders, AI calls or trading-setting changes.

- Keep all 15 coins and add ETHUSDT (16 total); capture trades, L1 and 1m candles. Remove the unused ticker subscription.
- Reserve up to 2 MB / 6,000 raw messages per coin for the 120s prebuffer (32 MB payload total). Noisy coins cannot evict quiet ones. Capacity truncation and startup warmup are recorded per coin. Same-millisecond messages are distinguished by ordinal.
- Ordinary connection failures get a localized uncertain boundary from the oldest last-observed configured coin quote minus 2s (bounded by connection start), plus a separate recovery marker after subscription acknowledgement and fresh L1 for every coin. This is not proof of feed completeness: offline minute-volume and quote checks remain required. Unexpected pipeline failures retain conservative full-connection treatment. Old gap records are not rewritten or automatically requalified.
- Active windows end explicitly as incomplete on a disconnect or graceful shutdown. Window IDs and 15m calendar buckets support traceability and grouping; they do not imply independent trades.
- Show observed 60s trade count, buy/sell base volume and delta together. At most 4,000 trades per coin, with truncation/warmup flags; not deduplicated. Minute summaries include counts/volume and cumulative late-trade omission counts. No footprint or absorption permission is inferred.
- Dashboard distinguishes current health from historical errors, shows spreads and buffer coverage, and includes active plus sealed storage. Warnings at 80/90/95%. No automatic offload or deletion is installed; capacity still halts recording visibly.
- Protected export streams all retained files (default) or the current run only (smaller). Scope is explicit in the manifest. Older files remain untouched. `/health` is liveness; `/ready` is capture readiness, not replay certification.

Keep one replica and the existing persistent `/data` mount, token and 2 GB cap. Existing Dockerfile now copies `capture_quality.py` alongside `recorder.py`. ETH capture begins only after successful deployment. No earlier ETH footprint is recovered.

Provisional 1m OB/FVG definitions, 10bp proximity, ten-minute windows and thirty-minute spacing remain capture heuristics. No Elliott/inducement/MSS/footprint strategy rule is removed or treated as automatically satisfied. No full-session profile, fills or strategy edge is established by this patch.

Validation: `python -m unittest discover -p 'test*.py' -v`. Nineteen tests cover storage/export preservation, queue backpressure, per-coin isolation, 32 MB reserved buffer stress, count bounds, equal-millisecond preservation, flow sample size, gap/recovery pairing, conservative pipeline errors and readiness. Offline peak RSS approximately 62.3 MiB; deployed memory/load must still be checked.

Official payload references checked 27 October 2026:
https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
https://bybit-exchange.github.io/docs/v5/websocket/public/trade
