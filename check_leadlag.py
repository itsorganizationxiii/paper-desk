import leadlag as leadlag_mod
leadlag_mod.LEADLAG_PAUSED = False
"""Offline deterministic lead-lag and server-entry tests. Never contacts Kalshi."""
import ast
import threading
from types import SimpleNamespace
from pathlib import Path
from leadlag import LeadLag, entry_skip

NOW=1060.0
BTC='KXBTC15M-TEST'

def quote(mid, at):
    return dict(yesBid=mid-0.01, yesAsk=mid+0.01, rx=at, ts=at)

def scenario(coin='SOL', move=0.09, btc_move=0.01):
    ll=LeadLag()
    for at in (1000.0, 1030.0):
        ll.observe(BTC,quote(0.5,at),at)
        ll.observe('KX'+coin+'15M-TEST',quote(0.5,at),at)
    ll.observe(BTC,quote(0.5+btc_move,NOW),NOW)
    ll.observe('KX'+coin+'15M-TEST',quote(0.5+move,NOW),NOW)
    return ll

ll=scenario(); ev=ll.events[-1]
assert ev['status']=='pending' and ev['triggerCoin']=='SOL' and ev['side']=='YES'
assert ev['moveC']==9 and len(ll.pending)==1
print('PASS 9c SOL move in 60s fires BTC YES when BTC only moved 1c')
ll.observe('KXSOL15M-TEST',quote(0.595,1061),1061)
assert len(ll.events)==1
print('PASS continuous above-threshold quotes do not duplicate triggers')
ll=scenario('XRP',-0.09,-0.01); ev=ll.events[-1]
assert ev['side']=='NO' and ev['ticker']==BTC and ev['triggerCoin']=='XRP'
print('PASS XRP down move targets only BTC NO')
ll=scenario(btc_move=0.04)
assert not ll.pending and 'BTC already moved' in ll.events[-1]['skipReason']
ll=scenario(move=-0.09,btc_move=-0.04)
assert not ll.pending and 'BTC already moved' in ll.events[-1]['skipReason']
print('PASS BTC >=1/3 catch-up skips in both directions and logs reason')
ev=scenario().events[-1]
assert entry_skip(ev,BTC,179)=='<3 min left'
assert entry_skip(ev,BTC,180) is None
assert entry_skip(ev,BTC,400,used=True)
assert entry_skip(ev,BTC,400,held=True)
assert entry_skip(ev,BTC,400,cooldown=True)=='BTC loss cooldown'
assert entry_skip(ev,'KXXRP15M-TEST',400)
print('PASS time boundary, one per window, open position, cooldown, and BTC-only guards')
ll=scenario(move=0.079)
assert not ll.events
print('PASS sub-8c move does not fire')
assert scenario(move=0.08).events[-1]['status']=='pending'
assert not scenario(move=0.09,btc_move=0.03).pending
print('PASS exact 8c fires and exact 1/3 BTC move skips')
ll=scenario(); ll.observe('KXSOL15M-NEW',quote(0.8,1061),1061)
assert len(ll.events)==1
print('PASS history never crosses market windows')

src=Path('server.py').read_text(); tree=ast.parse(src)
code='\n'.join(ast.get_source_segment(src,n) for n in tree.body if getattr(n,'name',None)=='leadlag_entry')
class FakeLV:
    def __init__(self):
        self.tried=set(); self.resting=set(); self.lastBuy=None; self.calls=[]
    def enabled(self): return True
    def wsq(self,tk): return quote(0.51,NOW)
    def buy(self,asset,tk,side,frac,ask,sizer,shadow=False,mode="live"):
        assert not shadow and mode=="live"
        assert asset=='BTC' and tk.startswith('KXBTC15M-')
        assert sizer(ask) is not None
        self.calls.append((asset,tk,side))
        return dict(price=ask)
class FakePT:
    def __init__(self): self.trades=[]
    def size(self,side,ask,mp): return (0.05,0.071,mp) if 0.40<=ask<=0.69 else None
    def openLive(self,asset,side,fill,strike,close,tk,meta,edge,q):
        t=dict(id=len(self.trades)+1,asset=asset,side=side,mode='live',status='open',ticker=tk,closesAt=close,**meta)
        self.trades.append(t); return t

def harness(ll=None,left=400,trades=None,cool=False,mp=0.7):
    L=FakeLV(); pt=FakePT(); pt.trades=list(trades or [])
    g=dict(LL=ll or scenario(),LV=L,pt=pt,time=SimpleNamespace(time=lambda:NOW),lock=threading.Lock(),
           cooldowns=lambda now=None: {'BTC':{}} if cool else {},entry_skip=entry_skip,
           LL_MIN_LEFT=180,LL_MAX_AGE=20,LL_FRESH=10,LL_MOVE=0.08,LL_RETRACE=0.02,is_shadow=lambda t:False,shadow_asset=lambda a:cool,
           WKND_MAX=0.69,wknd_night=lambda:True,need_edge=lambda:0.07,up_tilt_skip=lambda *a:False)
    g['leadlag']=leadlag_mod
    exec(code,g)
    _e=g['leadlag_entry']; g['leadlag_entry']=lambda kk,_e=_e,_m=mp: _e(kk,_m)
    k=dict(ticker=BTC,close=NOW+left,strike=100)
    g['leadlag_entry'](k)
    return g,L,pt,k

g,L,pt,k=harness()
assert len(L.calls)==1 and pt.trades[0]['leadlag'] and pt.trades[0]['signalSource']=='leadlag'
repeat=dict(scenario().events[-1]); g['LL'].pending.append(repeat); g['leadlag_entry'](k)
assert len(L.calls)==1 and 'already traded' in repeat['skipReason']
print('PASS server entry fills, tags, and allows only one leadlag buy per window')
g,L,pt,k=harness(scenario('XRP'))
assert len(L.calls)==1 and L.calls[0][:2]==('BTC',BTC)
print('PASS XRP signal never orders XRP')
for kwargs in (dict(left=179),dict(cool=True),dict(trades=[dict(ticker=BTC,mode='live',status='open')])):
    g,L,pt,k=harness(**kwargs)
    assert not L.calls and g['LL'].events[-1]['status']=='skipped'
print('PASS integration skips too late, cooldown, and existing open BTC')
prior=dict(ticker=BTC,mode='live',status='exited',leadlag=True)
g,L,pt,k=harness(trades=[prior])
assert not L.calls
print('PASS filled-trade history enforces one per window after restart')
ll=scenario(); ll.history['BTC'][1].append((NOW,0.55))
g,L,pt,k=harness(ll)
L.wsq=lambda tk:quote(0.55,NOW)
g['LL'].pending.append(dict(scenario().events[-1])); L.calls.clear(); pt.trades.clear(); g['LL'].attempted.clear()
g['leadlag_entry'](k)
assert not L.calls
print('PASS entry rechecks BTC catch-up on freshest quote')
ll=scenario(); snap=ll.snapshot([])
assert all(x in snap for x in ('count','wins','losses','pnl','lastTrigger','triggers'))
assert snap['lastTrigger']['triggerCoin']=='SOL'
for i in range(300): ll.events.append(dict(at=i))
assert len(ll.snapshot([])['triggers'])==200
print('PASS status fields and bounded 200-trigger log')
g,L,pt,k=harness(mp=0.55); assert not L.calls
g,L,pt,k=harness(mp=None); assert not L.calls
print('PASS leadlag needs EloEdge p >= 60% for our side')
print('ALL LEADLAG TESTS PASSED (offline; no real orders)')
