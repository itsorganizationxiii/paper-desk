"""Kalshi-only lead/lag observations. No network or orders on the websocket thread."""
from collections import deque
import threading
import time

MOVE = 0.08
LOOKBACK = 60
MIN_HISTORY = 10
BTC_FRACTION = 1 / 3
MIN_LEFT = 180
FRESH = 10
MAX_SIGNAL_AGE = 20
MAX_SPREAD = 0.10
MAX_RETRACE = 0.02
MAX_LOG = 200


def entry_skip(event, ticker, left, used=False, held=False, cooldown=False):
    if not ticker.startswith('KXBTC15M-') or event['ticker'] != ticker:
        return 'not matching BTC window'
    if left < MIN_LEFT:
        return '<3 min left'
    if used:
        return 'leadlag already traded/attempted this window'
    if held:
        return 'open live BTC position'
    if cooldown:
        return 'BTC loss cooldown'
    return None


class LeadLag:
    def __init__(self):
        self.lock = threading.RLock()
        self.history = {}
        self.active = {}
        self.events = deque(maxlen=MAX_LOG)
        self.pending = deque(maxlen=MAX_LOG)
        self.attempted = set()
        self.last_error = None

    def observe(self, ticker, quote, now=None):
        now = time.time() if now is None else now
        asset = ticker.split('15M-', 1)[0].removeprefix('KX')
        if asset not in ('BTC', 'SOL', 'XRP') or '15M-' not in ticker:
            return
        bid, ask = quote.get('yesBid'), quote.get('yesAsk')
        rx = quote.get('rx', now)
        ts = quote.get('ts') or rx
        if bid is None or ask is None or not (0 < bid <= ask < 1):
            return
        if ask - bid > MAX_SPREAD + 1e-9 or not (0 <= now - rx <= FRESH) or now - ts > FRESH:
            return
        with self.lock:
            old = self.history.get(asset)
            if old is None or old[0] != ticker:
                self.history[asset] = (ticker, deque(maxlen=1024))
                self.active.pop(asset, None)
            hist = self.history[asset][1]
            if hist and rx < hist[-1][0]:
                return
            hist.append((rx, (bid + ask) / 2))
            while hist and hist[0][0] < now - LOOKBACK:
                hist.popleft()
            if asset == 'BTC' or rx - hist[0][0] < MIN_HISTORY:
                return
            base_at, base_mid = hist[0]
            move = hist[-1][1] - base_mid
            direction = 'YES' if move > 0 else 'NO'
            if abs(move) + 1e-9 < MOVE:
                self.active.pop(asset, None)
                return
            key = (ticker, direction)
            if self.active.get(asset) == key:
                return
            self.active[asset] = key
            btc_ticker = 'KXBTC15M-' + ticker.split('15M-', 1)[1]
            event = dict(at=now, triggerCoin=asset, triggerTicker=ticker, ticker=btc_ticker,
                         side=direction, move=round(move, 5), moveC=round(move * 100, 2),
                         spanSec=round(rx-base_at, 2), leaderBase=base_mid,
                         status='pending', skipReason=None)
            btc = self.history.get('BTC')
            reason = None
            if not btc or btc[0] != btc_ticker or not btc[1]:
                reason = 'no same-window BTC history'
            else:
                baseline = min(btc[1], key=lambda x: abs(x[0]-base_at))
                if abs(baseline[0]-base_at) > 5 or now-btc[1][-1][0] > FRESH:
                    reason = 'stale/unaligned BTC history'
                else:
                    event['btcBase'] = baseline[1]
                    event['btcMove'] = round(btc[1][-1][1]-baseline[1], 5)
                    if self.caught_up(event, btc[1][-1][1]):
                        reason = 'BTC already moved >=1/3 of leader'
            if reason:
                event.update(status='skipped', skipReason=reason)
            else:
                self.pending.append(event)
            self.events.append(event)

    @staticmethod
    def caught_up(event, btc_mid):
        sign = 1 if event['side'] == 'YES' else -1
        return sign * (btc_mid-event['btcBase']) + 1e-9 >= abs(event['move']) * BTC_FRACTION

    def drain(self):
        with self.lock:
            events = list(self.pending)
            self.pending.clear()
            return events

    def finish(self, event, reason=None, trade=None):
        with self.lock:
            event.update(status='filled' if trade else 'skipped', skipReason=reason,
                         processedAt=time.time())
            if trade:
                event['tradeId'] = trade['id']

    def snapshot(self, trades):
        with self.lock:
            tr = [t for t in trades if t.get('mode') == 'live' and t.get('leadlag')]
            closed = [t for t in tr if t.get('status') != 'open']
            events = [dict(e) for e in self.events]
            return dict(count=len(tr), open=len(tr)-len(closed), wins=sum((t.get('pnl') or 0)>0 for t in closed),
                        losses=sum((t.get('pnl') or 0)<0 for t in closed),
                        pnl=round(sum(t.get('pnl') or 0 for t in closed), 2),
                        lastTrigger=events[-1] if events else None, triggers=events, lastError=self.last_error,
                        thresholds=dict(moveC=MOVE*100, lookbackSec=LOOKBACK, minHistorySec=MIN_HISTORY,
                                        btcFraction=BTC_FRACTION, minLeftSec=MIN_LEFT, freshSec=FRESH,
                                        maxSignalAgeSec=MAX_SIGNAL_AGE, maxSpreadC=MAX_SPREAD*100))
