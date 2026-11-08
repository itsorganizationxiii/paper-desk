"""Offline ethlead tests. Never contacts Kalshi."""
import ast, threading
from types import SimpleNamespace
from pathlib import Path
import ethlead
from ethlead import evaluate, EthLead

NOW=1000.0
BTC='KXBTC15M-TEST'
def q(yb,ya,at=NOW): return dict(yesBid=yb,yesAsk=ya,rx=at,ts=at)

s,ea,ba,g,r=evaluate(q(0.59,0.60),q(0.51,0.52),400,NOW)
assert r is None and s=='YES' and abs(g-0.08)<1e-9
print('PASS ETH YES 60c, BTC YES 52c (8c gap) fires YES')
s,ea,ba,g,r=evaluate(q(0.38,0.40),q(0.46,0.47),400,NOW)
assert r is None and s=='NO' and abs(ba-0.54)<1e-9
print('PASS NO side uses 1-yesBid asks')
assert evaluate(q(0.59,0.60),q(0.53,0.54),400,NOW)[4]=='gap outside 7-10c'
assert evaluate(q(0.59,0.60),q(0.48,0.49),400,NOW)[4]=='gap outside 7-10c'
assert evaluate(q(0.59,0.60),q(0.52,0.53),400,NOW)[4] is None
assert evaluate(q(0.59,0.60),q(0.49,0.50),400,NOW)[4] is None
print('PASS 7c and 10c inclusive, 6c and 11c skip')
assert evaluate(q(0.59,0.60),q(0.51,0.52),300,NOW)[4]=='<=5 min left'
print('PASS needs >5 min left')
assert evaluate(q(0.84,0.85),q(0.76,0.77),400,NOW)[4]=='BTC ask outside 40-69c band'
print('PASS band enforced')
assert 'stale' in evaluate(q(0.59,0.60,NOW-20),q(0.51,0.52),400,NOW)[4]
assert 'no fresh' in evaluate(None,q(0.51,0.52),400,NOW)[4]
print('PASS stale/missing quotes skip')
ethlead.ETHLEAD_ENABLED=False
assert evaluate(q(0.59,0.60),q(0.51,0.52),400,NOW)[4]=='disabled'
ethlead.ETHLEAD_ENABLED=True
print('PASS flag disables')

src=Path('server.py').read_text(); tree=ast.parse(src)
code='\n'.join(ast.get_source_segment(src,n) for n in tree.body if getattr(n,'name',None)=='ethlead_entry')
class FakeLV:
    def __init__(s,btc=q(0.51,0.52)): s.tried=set(); s.resting=set(); s.lastBuy=None; s.calls=[]; s.btc=btc
    def enabled(s): return True
    def wsq(s,tk): return q(0.59,0.60) if 'ETH' in tk else s.btc
    def buy(s,asset,tk,side,frac,ask,sizer,shadow=False,mode='live'):
        assert asset=='BTC' and not shadow and mode=='live' and sizer(ask) is not None
        s.calls.append((asset,tk,side,ask)); return dict(price=ask)
class FakePT:
    def __init__(s): s.trades=[]
    def size(s,side,ask,mp): return (0.05,0.07,mp) if 0.40<=ask<=0.69 else None
    def openLive(s,asset,side,fill,strike,close,tk,meta,e,qq):
        t=dict(id=len(s.trades)+1,asset=asset,side=side,mode='live',status='open',ticker=tk,closesAt=close,**meta); s.trades.append(t); return t
def harness(trades=None,shadow=False,cap=0.69,lv=None):
    L=lv or FakeLV(); pt=FakePT(); pt.trades=list(trades or [])
    g=dict(EL=EthLead(),LV=L,pt=pt,time=SimpleNamespace(time=lambda:NOW),lock=threading.Lock(),el_eval=evaluate,EL_MIN_LEFT=300,
           shadow_asset=lambda a:shadow,is_shadow=lambda t:False,WKND_MAX=cap,wknd_night=lambda:True,need_edge=lambda:0.07)
    exec(code,g); k=dict(ticker=BTC,close=NOW+400,strike=1)
    g['ethlead_entry'](k); return g,L,pt,k
g,L,pt,k=harness()
assert len(L.calls)==1 and pt.trades[0]['signalSource']=='ethlead'
g['ethlead_entry'](k); assert len(L.calls)==1
snap=g['EL'].snapshot(pt.trades); assert snap['count']==1 and snap['open']==1
print('PASS fills BTC, tags ethlead, one per window, status counts')
for kw in (dict(shadow=True),dict(cap=0.50),dict(trades=[dict(ticker=BTC,mode='live',status='open')]),
           dict(trades=[dict(ticker=BTC,mode='live',status='exited',signalSource='ethlead')])):
    g,L,pt,k=harness(**kw); assert not L.calls, kw
    assert g['EL'].events[-1]['status']=='skipped'
print('PASS shadow/cooldown, weekend cap, open position, prior ethlead skip with reasons')
print('ALL ETHLEAD TESTS PASSED')
