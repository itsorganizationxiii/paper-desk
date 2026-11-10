"""Offline checks for gap-LIVE (no network, no real orders)."""
import os, ast, types
os.environ.pop('LIVE_STRATEGIES',None); os.environ['LIVE_TRADING']='0'
import gaplive as GL, live as L, gapshadow as G
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
class FakeLV:
    def __init__(s,enabled=True,fill=1.0):
        s.on=enabled; s.calls=[]; s.saved=[]; s.gapLiveUsed=0; s.gapLiveDisabled=False; s.paused=not enabled; s.killed=False; s.lastBuy=None; s.fill=fill
    def enabled(s): return s.on
    def allowed(s): return True
    def buy(s,asset,tk,side,frac,ref,**kw):
        s.calls.append(dict(asset=asset,tk=tk,side=side,ref=ref,**kw))
        return None if s.fill is None else dict(contracts=s.fill,price=0.5,fee=0.02,cost=round(s.fill*0.5,4),orderId=f'o{len(s.calls)}',cash=20,frac=frac,side=side)
    def persist(s): s.saved.append((s.gapLiveUsed,s.gapLiveDisabled))
def P(rule='A',pair='BTC-ETH',asset='BTC',close=1000,stake=1.0,conf='normal'):
    return dict(id='GP1',mode='paper',shadow=False,gap=True,rule=rule,pair=pair,asset=asset,laggard=asset,ticker=f'KX{asset}15M-X{close}',side='YES',price=0.5,entry=0.5,stake=stake,conf=conf,closesAt=close)
opened=[]
def opener(p,fill,strike): t=dict(id=len(opened)+1,gapLive=True,mode='live',status='open',pnl=None,tag='gap-live',strike=strike); opened.append(t); return t
# defaults
chk('defaults: enabled, max 5 (test #2), $1, LIVE_STRATEGIES=gap',GL.ENABLED and GL.MAX_TRADES==5 and GL.USD==1.0 and GL.LIVE_STRATEGIES=={'gap'})
chk('rules A only, pairs BTC-ETH/SOL-XRP',GL.RULES=={'A'} and GL.PAIRS=={'BTC-ETH','SOL-XRP'})
# gating: paused -> no buy
lv=FakeLV(enabled=False); g=GL.GapLive(lv,opener,log=lambda *a:None)
chk('paused live -> no order',g.on_paper(P()) is None and not lv.calls and 'Resume' in g.stat['lastReason'])
lv=FakeLV(); g=GL.GapLive(lv,opener,log=lambda *a:None)
chk('rule B blocked',g.on_paper(P(rule='B')) is None and not lv.calls)
chk('gap20 blocked',g.on_paper(P(rule='gap20')) is None and not lv.calls)
chk('GOLD-SILVER blocked',g.on_paper(P(pair='GOLD-SILVER',asset='SILVER')) is None and not lv.calls)
chk('XRP laggard blocked',g.on_paper(P(pair='SOL-XRP',asset='XRP')) is None and not lv.calls)
x=P(); x['shadow']=True
chk('shadow trade blocked',g.on_paper(x) is None and not lv.calls)
# $1 override of the $3 strong tier
t=g.on_paper(P(stake=3.0,conf='strong'),dict(ticker='KXBTC15M-X1000',strike=100.0))
chk('strong $3 paper -> live order usd=$1',lv.calls and lv.calls[-1]['usd']==1.0 and t is not None)
chk('strike passed from laggard market',opened[-1]['strike']==100.0)
chk('count persisted after fill 1/3',lv.gapLiveUsed==1 and lv.saved[-1]==(1,False))
# SOL laggard on SOL-XRP is allowed
chk('SOL leg of SOL-XRP allowed',g.on_paper(P(pair='SOL-XRP',asset='SOL',close=1900)) is not None and lv.gapLiveUsed==2)
# partial fill counts
lv.fill=0.3
chk('partial fill counts toward cap',g.on_paper(P(close=2800)) is not None and lv.gapLiveUsed==3)
chk('fills 4 and 5',g.on_paper(P(close=3700)) is not None and g.on_paper(P(close=4600)) is not None and lv.gapLiveUsed==5)
chk('auto-disabled at cap and persisted',lv.gapLiveDisabled and lv.saved[-1]==(5,True))
n=len(lv.calls); lv.fill=1.0
chk('6th signal blocked: no order',g.on_paper(P(close=5500)) is None and len(lv.calls)==n and 'cap reached' in g.stat['lastReason'])
# no fill does not count
lv2=FakeLV(fill=None); g2=GL.GapLive(lv2,opener,log=lambda *a:None)
chk('no fill does not count',g2.on_paper(P()) is None and lv2.gapLiveUsed==0 and len(lv2.calls)==1)
# cap survives restart: Live loads/stores counters in its state doc
lvr=L.Live(store_load=lambda:dict(gapLiveUsed=5,gapLiveDisabled=True,gapLiveTest=GL.TEST_ID))
chk('Live loads gapLiveUsed/disabled from state doc',lvr.gapLiveUsed==5 and lvr.gapLiveDisabled)
chk('Live.state() carries counters',lvr.state()['gapLiveUsed']==5 and lvr.state()['gapLiveDisabled'] is True)
g3=GL.GapLive(lvr,opener,log=lambda *a:None); g3.reset_for_test(); lvr.enabled=lambda:True
chk('restored cap blocks after restart',g3.block_reason(P()).startswith('cap reached'))
# LIVE_STRATEGIES gate
chk('Spec not live even when engine enabled',GL.strategy_live('spec',FakeLV())==False)
chk('leadlag/ethlead not live',not GL.strategy_live('leadlag',FakeLV()) and not GL.strategy_live('ethlead',FakeLV()))
chk('gap live only when engine enabled',GL.strategy_live('gap',FakeLV()) and not GL.strategy_live('gap',FakeLV(enabled=False)))
# real Live.buy sizing with usd= (stubbed quote/order; no network)
lb=L.Live(); lb.enabled=lambda:True; lb.owner=lambda:True; lb.balance=lambda:50.0; lb._quote=lambda tk,side,k:0.50
orders=[]
def _order(tk,side,act,n,px,confirm=None): orders.append(n); return 200,n,px,0.02,'oid1',None
lb._order=_order; lb.pending=lambda *a,**k:None; lb.log=lambda **k:None
f=lb.buy('BTC','KXBTC15M-T1','YES',1.0,0.50,usd=1.0)
chk('Live.buy usd=1 -> 2.0 contracts @0.50 ($1)',f and orders[-1]==2.0)
lb.tried.clear(); f=lb.buy('BTC','KXBTC15M-T2','YES',1.0,0.50,usd=3.0)
chk('usd capped by HARD_MAX_USD ($2)',orders[-1]==4.0)
lb.tried.clear(); n=len(orders)
chk('Live.buy XRP hard block intact',lb.buy('XRP','KXXRP15M-T3','YES',1.0,0.5,usd=1.0) is None and len(orders)==n)
# gapshadow hook wiring: live hook gets the paper twin
G.TRADES.clear(); G.PAPER.clear(); G._seen.clear(); G._pseen.clear()
seen=[]; G.LIVE[0]=lambda p,m:seen.append((p,m))
orig=G.paper_open; G.paper_open=lambda t,m=None:dict(P(),id='GP9')
G._add(dict(rule='A',pair='BTC-ETH',laggard='BTC',side='YES',entry=0.5,closesAt=5000.0),[],dict(ticker='x'))
G.paper_open=orig; G.LIVE[0]=None
chk('gapshadow calls LIVE hook with paper twin',len(seen)==1 and seen[0][0]['id']=='GP9')
# server: hold-only exits + Spec gate (source + extracted functions)
src=open('server.py').read(); tree=ast.parse(src)
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(isinstance(f,ast.FunctionDef) and f.name=='openLiveGap' for f in n.body))
fns={f.name:f for f in cls.body if isinstance(f,ast.FunctionDef)}
ns={}
for nm in ('exit','exitLive','_sellLive'):
    m=ast.Module(body=[fns[nm]],type_ignores=[]); exec(compile(m,'srv','exec'),ns)
t=dict(gapLive=True,mode='live',shadow=False,status='open',ticker='KXBTC15M-Q',contracts=2,stake=1,price=0.5)
class Boom:
    def __getattr__(s,k): raise AssertionError('exit path touched '+k)
r=[ns['exit'](Boom(),dict(t),0.9,'takeprofit'),ns['exit'](Boom(),dict(t),0.1,'stop: x'),ns['exitLive'](Boom(),dict(t),0.1,'trail'),ns['_sellLive'](Boom(),dict(t),0.1,'stop')]
chk('gap-live exit/exitLive/_sellLive are no-ops (generic exits never touch gap-live; trail sells via gaplive.manage_exits)',r==[None]*4)
chk('monitor + ws exit loops skip gapLive',src.count("not t.get('exiting') and not t.get('gapLive')")==2)
chk('Spec main live site gated by live_for(spec)',"if live_for('spec') and not shd:" in src and "if LV and LV.enabled() and not shd:" not in src)
chk('reversal/alt live sites gated',src.count("if live_for('spec')")>=3)
chk('leadlag/ethlead gated',"paper=not live_for('leadlag')" in src and "paper=not live_for('ethlead')" in src)
chk('openLive still rejects gap metadata',"meta.get('gap') or str(meta.get('signalSource','')).startswith('gap')" in src)
chk('openLiveGap tags mode=live gapLive tag=gap-live',"mode='live',shadow=False,gapLive=True" in src and "tag='gap-live'" in src)
chk('cooldowns ignore gapLive',"not t.get('gapLive') and t.get('status') in ('won','lost','exited')" in src)
chk('/api/gaplive route present',"p=='/api/gaplive'" in src)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
