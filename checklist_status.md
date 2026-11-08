# Checklist status (Nov 8, 2026 10:00 AM ET)
1. Kalshi bid/ask + top-5 depth every 2-5s: NOT DONE. Logger not built yet.
2. Synced timestamp, window ID, seconds left per row: NOT DONE. Depends on #1.
3. Trade logs entry, fee, fill, outcome, after-fee P&L: PARTIAL. Existing trade log has entry, entryFee (paper), fill, outcome, pnl; not yet verified on every row.
4. Rebuilt 60s avg vs floor_strike/expiration_value: NOT DONE. Rebuilt index not built yet; endpoints verified (GET /trade-api/v2/markets?series_ticker=KXBTC15M, fields floor_strike and expiration_value).
5. 300-window stats: NOT DONE. Requires #4 plus 300 windows (~75 h).
6. Hard limits: DONE (live.py). HARD_MAX_USD=2.0 caps every live buy after sizing (flat $1 unchanged). Daily loss cap is built but OFF per owner (HARD_DAILY_LOSS=0.0, env 0); set HARD_DAILY_LOSS to a dollar amount to enable. Stale-data guard: live buy skipped if Kalshi WS quote is stale; 3 straight stale attempts trips kill switch. Boot test /api/status shows killSwitch dailyLossLimit 3.0; all checks pass except pre-existing check_reconcile/check_up_tilt.

## Update Nov 8, 2026 ~10:15 AM ET
- CF constituent list confirmed from https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf (BRTI): active Bitstamp, Coinbase, Kraken, Gemini, Bullish (30-Dec-24), Crypto.com (31-Mar-25), LMAX Digital (03-May-21); itBit suspended 27-Jul-26; Bitfinex/OKCoin suspended Apr-17. Streams: all active except LMAX (no public feed). Monthly recheck routine created.
- Level-2 order books per venue over public websockets: Coinbase level2_batch (full book), Kraken book depth 10, Bitstamp order_book top 50, Gemini l2, Crypto.com book.BTC_USD.50; Bullish top-of-book only (its order book WS timed out). Consolidated book merges levels within 0.5% of median mid; rebuilt index = mid of 5 BTC depth VWAP; median of venue mids also logged. Local test: 6/6 venues up, consolidated mid 75928.84 vs median 75930.33.
- #6 proof (prove_checklist6.py, output):
    DAILY_CAP enabled=3.0 dayPnl=-3.10 -> killed True | realized live loss $3.10 today >= $3.00
    DAILY_CAP off(0) dayPnl=-3.10 -> killed False
    STALE 3 attempts -> killed True | stale Kalshi data on 3 straight buy attempts | logs ['stale Kalshi quote', 'stale Kalshi quote', 'stale Kalshi quote']
    next buy after kill -> None kill switch: stale Kalshi data on 3 straight buy attempts
- #4 logs: Mongo collection idxcmp per 15-min boundary: rebuilt, rebuiltMedian, floorStrike, expirationValue, errStart/errEnd (+Median), rebuiltResult vs endResult. Stats at /api/idxstats (mean, std, p95 abs error, Up/Down disagreement).
- #1/#2 logs: Mongo collection books every 3s for BTC/ETH/SOL/XRP: ms (Kalshi Date-header clock offset applied), window, secsLeft, yes/no bid/ask, top-5 depth each side.
