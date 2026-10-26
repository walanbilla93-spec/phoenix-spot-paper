# Market Data Recorder 0.2

Prepared 26 October 2026. A separate, public-data-only recorder for the Orayan confirmation-strategy research. **Implementation package; not deployed or live-verified.** No trading, signal generation, account access, AI calls, changes to Orayan, or changes to Phoenix.

## Deployment choice

Use a separate Northflank combined service named `market-data-recorder` in the existing `deployment-service` project in `Blackviper9275's Team`, alongside Phoenix. Do not inject this process into Phoenix or Orayan. Phoenix's observed deployment on 26 October was main commit `7940e6b`, with CI/CD enabled and 0.2 vCPU/512 MB assigned. Consequently, **do not push recorder changes to Phoenix's production main branch**. Use a separate repository, or a dedicated branch and Dockerfile path selected only by the new service.

A new service and persistent volume consume quota and may cost money. Existing free capacity and volume pricing were not established. No service, volume, paid plan, permission, or deployment was created by this package.

## What it records

One Bybit public linear WebSocket connection, with four topics per symbol. Default capture universe (15 active USDT perpetuals checked via the Bybit instruments connector on 26 October 2026):

`SOLUSDT, XRPUSDT, DOGEUSDT, AVAXUSDT, SUIUSDT, SEIUSDT, NEARUSDT, INJUSDT, APTUSDT, ARBUSDT, OPUSDT, WIFUSDT, 1000PEPEUSDT, TNSRUSDT, PIPPINUSDT`

This is a fixed research watchlist, not a measured ranking of today’s fastest movers. Capture volatility and spread prospectively rather than selecting only future winners. Instrument metadata is included in `instruments-checked-20261026.json`; refresh on deployment. PEPE uses the explicit 1000-unit Bybit contract symbol. Topics below use SOL as an example; each is subscribed for every configured symbol:

- `publicTrade.SOLUSDT`: unmodified messages with all executions, taker buy/sell side, price, quantity, exchange time, IDs, sequence values and block/RPI flags where supplied.
- `orderbook.1.SOLUSDT`: best bid/ask prices and quantities, preserving exchange timestamps and snapshot metadata. This is level one, not full market depth or proof of executable depth/queue priority.
- `kline.1.SOLUSDT`: source minute candle updates, including the closed flag.
- `tickers.SOLUSDT`: raw initial snapshot and subsequent ticker deltas. Reconstruct ticker state by applying deltas; do not treat each update as a full snapshot. Funding/mark fields remain source values, not guaranteed actual settlement charges.
- Receive wall-clock and monotonic timestamps, boot ID, connection ID, subscription replies, gap events and metadata failures.
- At each boot: public instrument rules, 200 hourly candles, recent funding history if the public REST requests succeed. The last hourly candle may be unfinished; replay must exclude it until its close. Metadata failures are explicit.

The data contains no private API keys or account history. An export token protects the download; this is a service token, not an exchange credential.

## Persistence and bounds

- Append-only gzip JSONL segments on a dedicated persistent volume. Rotate at about 16 MB uncompressed, five minutes, or an export snapshot.
- Gzip level 1 limits compression work. Queue limited to 8 MB and 1,024 pending messages, WebSocket message limit 2 MB, writer batches up to 32 messages. These are bounds on components, not a claimed exact peak memory bound.
- One serialized disk writer; locks also protect cancellation/export/rotation overlap. No whole-history arrays or dictionaries.
- Default payload storage cap: 2,000,000,000 bytes; file-count cap: 10,000. Reserve headroom on a dedicated volume larger than that cap (proposed 3 GB or more; verify provider units/cost). Evidence is **never automatically deleted**. Capture halts when storage fills; status/export stay available.
- Restart appends new boot-specific files. Existing partial gzip files after a hard kill are preserved; offline validation must detect missing gzip trailers/truncated records and exclude those affected intervals.
- Flush after each writer batch. Flush is not fsync: unexpected host loss can lose recent writes. Clean SIGTERM drains pending writer data and closes segments; a container hard kill cannot be made lossless.
- Storage rate and sustainable CPU are not yet measured against the live 15-symbol feed. Measure hourly disk growth, peak RSS, queue size and CPU with all configured symbols. Do not claim the 2 GB cap retains a complete day until rate has been measured. Capture halts instead of deleting evidence if the cap is reached.

## Data-quality rules

- Subscription must succeed. Recording status requires quotes, trades and candle messages for every configured symbol on the connection. Per-symbol message counts and last receive timestamps are exposed. Each symbol has its own 10-second quote watchdog; one active coin cannot conceal another stale stream.
- Reconnect/startup are quality boundaries. Application ping every 20 seconds; stale L1 quote stream after 10 seconds forces reconnect. L1 publishes snapshots and repeats an unchanged snapshot after roughly three seconds according to current docs.
- On a detected connection failure, conservatively invalidate the **entire affected connection interval** until offline validation can narrow it. This sacrifices usable data rather than implying certainty about a missing interval. Queue overflow is counted and invalidates the connection. Disk failure halts capture.
- Bybit can send multiple trade messages for one `seq`. Do not discard messages just because sequence numbers repeat or infer that jumps equal exact trade loss. Keep raw trade IDs and deduplicate offline with a disk-backed unique index. Block and RPI flags are retained; apply the strategy's exclusion convention during reconstruction.
- Public feed delivery cannot prove perfect capture. A continuous socket is not proof of completeness. Verify counts/turnover against closed source candles where their inclusion conventions match; flag discrepancies rather than automatically correcting them. Check exchange and receive timestamps/clock drift.
- `/status` deliberately reports `replay_ready: false`. This version is a recorder, not a validated replay-readiness classifier.

## When the first replay becomes possible

Collection begins now when deployed; it cannot recover the historical footprint missing from the uploaded candle CSV. Wait for at least one complete, verified UTC session of trades to form the next session's profile, then collect the test interval. Starting mid-day usually requires waiting until the next full UTC day has completed. Successful hourly warm-up reduces the ATR wait; otherwise collect 100 complete hours or supply verified warm-up candles. Do not claim the next 24 hours automatically produce four entries.

Before replay: validate source timestamps and instrument rules, reconstruct true quote-volume profiles and buy/sell footprints from trades, validate bid/ask snapshots, censor gaps, deduplicate, apply latency/cost assumptions, then take the first qualifying setups under frozen rules. The candle diagnostic's entry-zone conflict still needs a separately versioned decision; collecting data does not resolve it automatically.

## HTTP endpoints and dashboard

- `GET /`: small status page, refreshed every 10 seconds, with an export-token password field.
- `GET /health`: process liveness only. Always 200 while server runs, even if recording halted; this avoids a restart loop concealing storage failure.
- `GET /status`: compact public operational status including queued bytes, counts, quality and error information. Read `capture`, not health alone, to determine whether recording is active.
- `POST /export.tar`: token in form body, constant-time comparison; one export at a time. Rotates/seals active file under lock, then streams tar headers and existing gzip members in 64 KB chunks. No archive is rebuilt in RAM or duplicated on disk. New recording continues into a different segment. Native form download avoids loading a multi-GB Blob into dashboard memory.

The manifest lists sealed members and their byte sizes. Each gzip member supplies CRC32 validation; this is accidental-corruption detection, not a cryptographic evidence signature. Export excludes subsequently created segments. No local path or arbitrary file selector is accepted from the client.

## Northflank settings

- Dockerfile: this directory's Dockerfile; one replica only; no autoscaling.
- Suggested initial compute: 0.2 vCPU / 512 MB if available for the 15-symbol stream, otherwise the smallest suitable plan; **verify live load rather than treating this as proven sizing**. Keep below 512 MB. Do not reduce Phoenix's resources to make room.
- Persistent mount `/data`; `DATA_DIR=/data/sol-recorder`.
- `SYMBOLS`: optional comma-separated uppercase USDT symbols, at most 15; defaults to the checked list above. No automatic substitutions.
- `PORT=8080`; expose HTTP port 8080; liveness `/health`.
- `MAX_STORAGE_BYTES=2000000000`.
- Secret `EXPORT_TOKEN`: generate at least 24 random characters through the deployment secret facility. Never commit or log it. No exchange keys required.
- Keep public endpoint access scope deliberate. Status is public and contains operational metadata; only export is token-protected. Put service behind platform access controls if that metadata should be private.

## Local checks and deployment gate

`python -m pip install -r requirements.txt`

`python test_recorder.py`

Five offline tests passed: 15-symbol/60-topic configuration and status separation; segment rotation/restart and capacity; queue bound; HTTP unauthorized export plus valid streamed archive and continued status/recording; graceful HALTED state on storage failure. Observed offline test peak RSS about 33 MiB. Tests use synthetic fixtures; they are not trading evidence or live-feed certification. No Docker build, external WebSocket capture, long-run CPU/RSS or deployed dashboard test is claimed.

Before considering live capture successful, verify real trade and quote counts increase, raw IDs/sides/timestamps are present, reconnects are marked, compressed members validate, a real dashboard export downloads and reopens, and CPU/RSS/storage growth fit the chosen plan. The package remains undeployed until that work is performed.

## Sources checked

- https://bybit-exchange.github.io/docs/v5/websocket/public/trade
- https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
- https://bybit-exchange.github.io/docs/v5/ws/connect

Official API documentation defines payload mechanics, not capture completeness or strategy profitability.
