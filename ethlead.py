"""ETH-leads-BTC live entry: buy BTC when ETH prices a side >50c and BTC is 7-10c cheaper on that side."""
import threading
import time
from collections import deque

ETHLEAD_ENABLED = True
ETHLEAD_GAP = (0.07, 0.10)
ETHLEAD_MIN_LEFT = 300
ETHLEAD_ETH_MIN = 0.50
BAND = (0.40, 0.69)
FRESH = 10
# Skip when Coinbase flow imbalance is this strongly against our side (owner request Nov 8)
ETHLEAD_FLOW_FILTER = True
ETHLEAD_MAX_AGAINST_IMB = 0.5


def flow_against(side, fl):
    if not ETHLEAD_FLOW_FILTER or not fl or fl.get('imb') is None:
        return False
    imb = fl['imb']
    return imb <= -ETHLEAD_MAX_AGAINST_IMB if side == 'YES' else imb >= ETHLEAD_MAX_AGAINST_IMB


def side_asks(q):
    return dict(YES=q['yesAsk'], NO=round(1-q['yesBid'], 4))


def evaluate(eth_q, btc_q, left, now=None):
    """Return (side, ethAsk, btcAsk, gap, reason). reason None means a valid signal."""
    now = time.time() if now is None else now
    if not ETHLEAD_ENABLED:
        return None, None, None, None, 'disabled'
    if left <= ETHLEAD_MIN_LEFT:
        return None, None, None, None, '<=5 min left'
    for name, q in (('ETH', eth_q), ('BTC', btc_q)):
        if not q or q.get('yesBid') is None or q.get('yesAsk') is None:
            return None, None, None, None, 'no fresh '+name+' websocket quote'
        if now-(q.get('rx') or 0) > FRESH:
            return None, None, None, None, 'stale '+name+' websocket quote'
    ea, ba = side_asks(eth_q), side_asks(btc_q)
    side = 'YES' if ea['YES'] > ETHLEAD_ETH_MIN else 'NO' if ea['NO'] > ETHLEAD_ETH_MIN else None
    if side is None:
        return None, None, None, None, 'ETH not >50c on either side'
    gap = round(ea[side]-ba[side], 4)
    if not (ETHLEAD_GAP[0]-1e-9 <= gap <= ETHLEAD_GAP[1]+1e-9):
        return side, ea[side], ba[side], gap, 'gap outside 7-10c'
    if not (BAND[0]-1e-9 <= ba[side] <= BAND[1]+1e-9):
        return side, ea[side], ba[side], gap, 'BTC ask outside 40-69c band'
    return side, ea[side], ba[side], gap, None


class EthLead:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = deque(maxlen=200)
        self.attempted = set()
        self.last_error = None
        self.last_reason = None

    def log(self, ev):
        with self.lock:
            self.events.append(ev)

    def snapshot(self, trades):
        with self.lock:
            tr = [t for t in trades if t.get('mode') == 'live' and t.get('signalSource') == 'ethlead']
            closed = [t for t in tr if t.get('status') != 'open']
            ev = [dict(e) for e in self.events]
            return dict(enabled=ETHLEAD_ENABLED, count=len(tr), open=len(tr)-len(closed),
                        wins=sum((t.get('pnl') or 0) > 0 for t in closed),
                        losses=sum((t.get('pnl') or 0) < 0 for t in closed),
                        pnl=round(sum(t.get('pnl') or 0 for t in closed), 2),
                        lastTrigger=ev[-1] if ev else None, triggers=ev[-50:],
                        lastCheckReason=self.last_reason, lastError=self.last_error,
                        thresholds=dict(gapC=[ETHLEAD_GAP[0]*100, ETHLEAD_GAP[1]*100], minLeftSec=ETHLEAD_MIN_LEFT,
                                        ethMinC=ETHLEAD_ETH_MIN*100, bandC=[BAND[0]*100, BAND[1]*100]))

# User asked (Nov 8, 4:44 AM ET) to pause ethlead overnight; resumes 8:00 AM ET Nov 8 2026. Set 0 to disable the pause.
ETHLEAD_RESUME_AT=1794142800
