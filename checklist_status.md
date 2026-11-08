# Checklist status (Nov 8, 2026 10:00 AM ET)
1. Kalshi bid/ask + top-5 depth every 2-5s: NOT DONE. Logger not built yet.
2. Synced timestamp, window ID, seconds left per row: NOT DONE. Depends on #1.
3. Trade logs entry, fee, fill, outcome, after-fee P&L: PARTIAL. Existing trade log has entry, entryFee (paper), fill, outcome, pnl; not yet verified on every row.
4. Rebuilt 60s avg vs floor_strike/expiration_value: NOT DONE. Rebuilt index not built yet; endpoints verified (GET /trade-api/v2/markets?series_ticker=KXBTC15M, fields floor_strike and expiration_value).
5. 300-window stats: NOT DONE. Requires #4 plus 300 windows (~75 h).
6. Hard limits: DONE (live.py). HARD_MAX_USD=2.0 caps every live buy after sizing (flat $1 unchanged). HARD_DAILY_LOSS=3.0 trips the kill switch at -$3 realized live P&L/day even though the server env LIVE_DAILY_LOSS_USD was 0 (disabled). Stale-data guard: live buy skipped if Kalshi WS quote is stale; 3 straight stale attempts trips kill switch. Boot test /api/status shows killSwitch dailyLossLimit 3.0; all checks pass except pre-existing check_reconcile/check_up_tilt.
