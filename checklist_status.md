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
- 2026-11-08 10:14 EST CF constituent list v13.7 (issued 24th August 2026), sha256 263998d7058ed59d; BRTI active: Bitstamp, Coinbase, Kraken, Gemini, Bullish, Crypto.com, LMAX Digital

## #3 verification, Nov 8, 2026 10:30 AM ET (all 198 real live trades, ids 878-1084)
Fields: entry=price, fee=entryFee (+exitFee when exited), fill=contracts, outcome=status + exitPrice/settle (+finalResult), after-fee P&L=pnl.
live trades 198 Counter({'exited': 189, 'won': 6, 'lost': 3})
blank fields {'exitFee': [1070], 'finalResult': [1068, 1059, 1055, 1044, 1036, 1033, 1029, 988, 878]}
partial-fill trades (contracts*price < 90% of stake) 0 []
1059 won kalshi None None 
1029 lost kalshi None None 
1033 won kalshi None None 
1068 won kalshi None None 
1036 won kalshi None None 
1070 exited external None True closed outside bot (no Kalshi position) during: stop: mark 0
878 lost kalshi None None 
1044 won kalshi None None 
988 lost kalshi None None 
1055 won kalshi None None 
order log entries in status 20 Counter({'order-submitted': 4, 'buy-skip': 3, 'rest-buy': 2, 'cancel-own': 2, 'rest-buy-placed': 2, 'sell': 2, 'position-check': 2, 'buy': 1, 'stop-sell': 1, None: 1})
Result: FAILED.
- #1070 (BTC NO, externally closed during a stop): exitFee blank and pnl estimated (pnlEstimated=True); booked -1.01 vs ~-0.44 real cash. Real Kalshi fill was never recorded.
- 9 settled trades (878, 988, 1029, 1033, 1036, 1044, 1055, 1059, 1068) have outcome via status won/lost + settle value but no finalResult field (exited trades have it). Not blank outcome, but inconsistent.
- Partial fills: 0 trades filled under 90% of stake. Cancels/no-fills are not trade rows; they live only in the order log, which the server keeps to the last 200 entries, so older cancels cannot be audited.
- No logic or size changed (owner: collect data only).
Collector: complete-window rule added (windowlog.py): a window counts toward 300 only if samples cover from 60s before open to close with no gap over 5s, 57+ samples in the closing 60s, and Kalshi expiration_value present; otherwise flagged complete=False with incompleteReason and excluded from stats.
