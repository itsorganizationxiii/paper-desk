# 300-window collection rule
Window = BTC 15-min Kalshi market closing at boundary b. Observed span [b-960, b): 60s pre-open (floor_strike avg) + 900s. Expected 960 one-second samples of the rebuilt index.
- COMPLETE: >=957 samples, max gap <=5s, >=57 samples in closing 60s, Kalshi expiration_value present.
- RECOVERED: >=912 samples (95%), max gap <=60s, >=57 in closing 60s and >=57 in opening 60s, expiration_value present (e.g. spanned a deploy or feed drop).
- INCOMPLETE: settlement present, coverage below that. INVALID: no settlement after 30 min, or no data.
Only COMPLETE + RECOVERED count. No interpolation. Every window kept in `idxcmp` (one per boundary, id KXBTC15M-<b>, idempotent upsert) with expected/received, max gap, closing/opening 60s counts, restart flag, per-venue connects/drops/stale/exchange lag.
Samples persist in `idxsec` (_id = unix second, 3-day TTL) from every instance and are restored on boot. Feed drops/boots logged in `feedevents`.
Feeds: 10s silence or 30s stale book forces reconnect + resubscribe with fresh snapshot. Sequence numbers: none of the six public feeds used expose a usable sequence on these channels except via reconnect snapshot, so gaps are detected by time, not sequence.
Tests: check_windowlog.py.
