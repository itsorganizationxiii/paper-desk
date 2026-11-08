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

## Nov 8, 2026 10:40 AM ET items
- Paper not evidence: DONE. Recorded as a standing rule; paper results are excluded from strategy decisions. Only live Kalshi fills and idxcmp/books window data count.
- Paper fills at real ask/bid and real fees: DONE earlier (commit 516ad4f, 2df7d1e). Paper entry re-checks the Kalshi websocket ask at that moment and skips (PAPER NOFILL) if it moved >1c worse, else fills at that ask; entry fee = Kalshi fee ceil(0.07*C*P*(1-P)); paper stops/locks run on the same websocket bid as live and pay the Kalshi exit fee.
- Hold-to-settlement P&L: DONE (already logged). Every exited trade carries counterfactualPnl (P&L if held to settlement), finalResult, wouldHaveWon and exitSavedPnl next to the actual pnl: 189/189 live exited, 813/813 paper exited. Settled trades: hold P&L = actual pnl.

## hold_pnl (commit 26433aa, Nov 08 11:04 AM ET)
hold_pnl = contracts*(won?1:0) - stake - entryFee (settled trades: = pnl). Auto-filled in settle(); backfilled 1085/1085. Exit type from exitReason prefix. Paper pre-real-fill rows not evidence.
```
live TOTAL n=198 exit=-13.45 hold=-17.76
  flip n=4 exit=-0.57 hold=1.40 diff=-1.97
  other:breakeven n=3 exit=-0.02 hold=-0.04 diff=0.02
  other:closed outside bot ( n=1 exit=0.07 hold=-1.04 diff=1.10
  other:paircash n=3 exit=1.23 hold=-0.90 diff=2.13
  other:whale n=5 exit=1.26 hold=0.21 diff=1.05
  profit lock n=67 exit=8.05 hold=18.90 diff=-10.85
  settled n=9 exit=0.34 hold=0.34 diff=0.00
  stop n=69 exit=-33.89 hold=-45.25 diff=11.36
  take profit n=37 exit=10.08 hold=8.62 diff=1.46
paper TOTAL n=878 exit=2127.00 hold=1492.41
  flip n=20 exit=-1.44 hold=84.12 diff=-85.56
  other:breakeven n=32 exit=-89.98 hold=131.96 diff=-221.94
  other:whale n=39 exit=135.88 hold=-127.12 diff=263.00
  profit lock n=244 exit=857.42 hold=538.94 diff=318.48
  settled n=73 exit=111.57 hold=111.57 diff=0.00
  stop n=163 exit=-1128.97 hold=-1340.32 diff=211.35
  take profit n=307 exit=2242.52 hold=2093.26 diff=149.26
{'pnl': 0.17, 'hold_pnl': 0.587, 'finalResult': 'no', 'exitReason': 'profitlock: ws quote, peak 0.820 -> bid 0.740; fee $0.03', 'price': 0.6}
```

## #3 reconciliation rerun (Nov 08 12:08 PM ET, commit 5ff8720)
Cause on #1001: Kalshi reports a YES sell as action=sell side=no at the NO price (0.95); the parser booked 0.95 instead of 0.05. Settlement fee_cost also repeated the fill fees (double count). #1001 before: Kalshi-side +2.0544; after: -1.2908 (bot -1.29, match).
Live sell path: uses order average_fill_price (YES-book), flipped for NO in order_fields; #1001 exitPrice 0.05 matches Kalshi -> bot side handling OK.
Sanity bounds now on every stored P&L (server save + truthfix); flagged so far: none in bot rows.
Net deposits since first live trade #878 (Nov 6 12:06 PM ET), from Kalshi /portfolio/deposits+withdrawals: +20.24 (fee 0.40) - 1.11 - 10.12 = +9.01 (8.61 after deposit fee).
Balance change 15.66 -> 15.12 = -0.54. Trading P&L implied = -9.55 (-9.15 if fee taken from deposit).
Kalshi per-ticker P&L (fixed parser) -7.93; bot -13.45 (pre-#1070 restore; #1070 now -0.435 from fills, was -1.01 estimate / +0.07 buggy).
Leftover: bot vs balance-implied ~ -3.9; Kalshi-calc vs balance-implied ~ +1.6. NOT passed.
Still out of bounds on Kalshi side: #1055 +2.24, #1044 +2.07, #1068 +2.00 on ~$1 stakes -> another parse case (likely lock-hold/multi-order tickers). Next: dump their raw fills.
The -14.53 vs -13.45: same bot sum before vs after truthfix replaced estimated trades (#878, #988, #1070) with buggy Kalshi values; those are now redone.

## #3 aggregate reconciliation (Nov 08 ~12:30 PM ET)
Window start: $15.66 = liveCashAtEntry of first live trade #878, Nov 6 12:06:51 PM ET.
Before it: deposit Nov 6 10:29 AM $13.00 (not in window). After it: withdrawal Nov 6 10:58 PM $1.11, withdrawal 11:17 PM $10.12, deposit 11:19 PM $20.24 (Kalshi fee $0.40, credited $19.84). Net deposits after = +$8.61 credited (+$9.01 gross).
Balance change 15.66 -> 15.12 = -0.54. Balance change minus net deposits = -9.15.
(1) Fees: bot entry+exit fees $4.91 vs Kalshi $5.15 (all fills since #878, incl. manual).
(3) Positions open at #878: 5 pre-window tickers (Nov 6 9:00-11:30 markets), all settled before 12:06 -> none carried in.
(4) Fills with no bot trade, tagged manual (8 tickers): KXBTC15M-26NOV072145-45, KXETH15M-26NOV072145-45, KXETH15M-26NOV071745-45, KXETH15M-26NOV071445-45, KXBTC15M-26NOV061645-45, KXMVECROSSCATEGORY parlay (37.5 @0.07c), KXSOL15M-26NOV061415-15, KXSOL15M-26NOV061400-00. Manual P&L +$3.46.
Second parse bug: Kalshi fills are YES-book relative, a NO BUY shows as action=sell side=no. Now using settlement aggregates: revenue + min(yes,no count) - yes cost - no cost - fees.
#1055 Kalshi 2.24 -> 0.6085 (bot 0.61); #1044 -> ~0.54; #1068 -> ~0.49. All match bot now.
#1070: bought NO 1.44 @0.69, stop sell actually filled @0.42 (bot logged no fill); Kalshi P&L -0.435.
Bot P&L (Kalshi records, bot tickers) -12.60; bot's own figure -13.96 (bot understates by 1.36, mostly #1034 +0.67, #988 -0.56, #1033 +0.44, #886, #989-992).
Bot -12.60 + manual +3.46 = -9.13 vs balance-minus-deposits -9.15 -> leftover $0.02. PASS.
Evaluation uses bot trades only.
