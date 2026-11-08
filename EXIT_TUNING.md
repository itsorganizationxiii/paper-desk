# Weekend exit tuning — November 7, 2026 (ET)

Up: arm entry +18c, give back 8c. Down: arm entry +20c, give back 7c.
These are code constants. The existing fixed 80c arm ceiling still applies;
paired cash-out, stop thresholds, weekday locks and other strategy settings
are unchanged.

## Replay sanity check

Read the Mongo-backed public status trade snapshot and refreshed local tick
archive. Only three live lock exits have tick coverage from entry (of 61
historical live lock exits). In an isolated quote replay, preserving the
80c fixed arm and 60%-of-entry stop, the old thresholds produce +$0.1743
combined; the proposed thresholds produce +$0.0938, or $0.0805 less.
Nearby Up +17c/7c, Down +19c/6c and Up +19c/9c, Down +21c/8c tie the
proposed values. Thus the available replay does NOT establish improvement,
and there is no clearly better nearby setting. Retain the requested values
as an experiment, not as a backtested edge.

This is a quote-only, isolated exit comparison, not a portfolio backtest:
model probability and paired-exit histories and executable order depth are
unavailable. Last archived quotes precede settlement by 1, 30 and 80 seconds.
All three configurations still lock-exit all three covered trades. The
fixed 80c arm and gaps back near entry explain why later relative arming
does not avoid those exits. Do not infer the claimed 13-of-18 counterfactual
can be reproduced with this archive. Full local replay and JSON comparison
are in /workspace/paper-desk-backups/replay_lock_exits.py and
/workspace/paper-desk-backups/lock_replay_comparison.json.

## Stop execution

A bid below the stop skips the protected limit completely. Otherwise one
protected IOC is allowed. Any remainder immediately goes to a 1c marketable
reduce-only IOC, not the regular three-attempt sell loop. Exchange
reduce-only protection replaces the blocking position query; there are no
REST quote preflights, retry sleeps, or fill pagination on this path. Each
submission uses its returned fill or at most one immediate status poll.
The existing one-sell-in-flight trade guard and stop slippage logging remain;
logs additionally expose elapsed milliseconds and fallback submission time.
Fill averages use YES-book conversion and status fill costs rather than the
extreme aggressive limit for realized accounting.

Offline tests at mocked 300ms per HTTP call take 600ms for direct fallback,
and 1200ms for a missed/partial protected limit plus fallback, with no sleeps.
These are modeled timings, not observed live latency; external HTTP stalls
can still exceed two seconds. All check_*.py scripts and an isolated
LIVE_TRADING=0 boot/curl on PORT=18998 passed.

## Additional owner-approved alt-consensus cap

Alt-consensus stays live, with at most one live entry per rolling 3600 seconds
across assets. If the most recent closed live alt-consensus trade has negative
PnL, block new alt entries until 3600 seconds after that close. Both limits
are reconstructed from the persisted trade history on every check, so they
survive restart without resetting. Paper/shadow and other signal sources do
not count or get blocked. Reasons are deduplicated per ticker/block period,
recorded in the live order log, and exposed with cap state in `/api/status`.

Stop order-submission and slippage logs are appended in memory on the hot
path, then persisted by PaperTrader after recording the fill (or after a
zero-fill result). This prevents synchronous Mongo log writes from delaying
confirmation or the immediate aggressive fallback. Lease checks remain
fail-closed and synchronous; modeled timing is not a guarantee against
external Mongo/HTTP latency.
