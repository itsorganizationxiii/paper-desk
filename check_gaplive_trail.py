"""Offline checks for gap-live test #2 (owner t1050): trailing exit -> real sell, 5-trade cap reset. No network, no real orders."""
import os, time
from datetime import datetime
from paper_tz import ET
DAY=datetime(2026,11,10,10,22,tzinfo=ET).timestamp()
os.environ.pop('LIVE_STRATEGIES',None); os.environ['LIVE_TRADING']='0'
import gaplive as GL, live as L
GL.moogle_side_p=lambda *a,**k:0.70
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
class Ex:
    """Fake Kalshi exchange behind live.api: positions + reduce-only IOC sells. fill=None -> fill everything, 0 -> nothing."""
    def __init__(s,pos,fill=None,code=201): s.pos=pos; s.fill=fill; s.code=code; s.posts=[]; s.ords={}
    def __call__(s,method,path,body=None):
        if method=='GET' and path.startswith('/portfolio/positions'):
            return 200,dict(market_positions=[dict(ticker='KXBTC15M-T1',position_fp=str(s.pos))])
        if method=='POST' and path=='/portfolio/events/orders':
            s.posts.append(dict(body))
            if s.code not in (200,201): return s.code,dict(error='rejected')
            fl=s.fill.pop(0) if isinstance(s.fill,list) else s.fill
            n=float(body['count']); f=n if fl is None else min(n,fl); oid=f's{len(s.posts)}'
            yes=float(body['price']); s.pos=round(s.pos-f,2) if body['side']=='ask' else round(s.pos+f,2)
            o=dict(order_id=oid,fill_count_fp=f,status='executed' if f else 'canceled',average_fill_price_dollars=yes if f else None,fees_dollars=0.01 if f else 0)
            s.ords[oid]=o; return 201,dict(order=o)
        if method=='GET' and path.startswith('/portfolio/orders/'):
            oid=path.split('/')[3].split('?')[0]; return 200,dict(order=s.ords.get(oid,{}))
        if method=='GET' and path.startswith('/portfolio/fills'): return 200,dict(fills=[],cursor=None)
        if method=='GET' and path.startswith('/markets/'): return 200,dict(market={})
        return 404,{}
def mklive(ex):
    L.api=ex; lv=L.Live(); lv.allowed=lambda:True; lv.owner=lambda:True; lv._quote=lambda tk,side,k:None
    lv.booked=[]; ob=lv.book; lv.book=lambda p:(lv.booked.append(p),ob(p)); return lv
def T(contracts=1.87,price=0.50,close=None,tk='KXBTC15M-T1'):
    return dict(id=1,gapLive=True,mode='live',shadow=False,status='open',exitMode='trail',holdToSettle=False,gapLiveTest=GL.TEST_ID,
                asset='BTC',side='YES',ticker=tk,price=price,contracts=contracts,stake=round(contracts*price+0.02,4),
                liveOrderId='b1',openedAt=time.time()-60,closesAt=close or time.time()+600)
SELL_ACTS=lambda lv:[o['action'] for o in lv.orders]
# --- config
chk('defaults: trail +15c arm / 10c giveback, no cap, $1, test id 4, Moogle 65%',GL.EXIT==('trail',0.15,0.10) and GL.MAX_TRADES==0 and not GL._has_cap() and GL.USD==1.0 and GL.TEST_ID=='4' and GL.MOOGLE_BAR==0.65 and GL.MOOGLE_STALE_SEC==60)
chk('guards unchanged: HARD_MAX_USD $2, MAX_CHASE 5c, daily loss $5',L.HARD_MAX_USD==2.0 and L.MAX_CHASE==0.05 and L.DAILY_LOSS==5.0)
# --- 1. arming
t=T(); now=time.time()
chk('below +15c: not armed',GL.trail_step(t,0.64,now) is False and not t.get('trailArmedAt') and t['peakBid']==0.64)
chk('at +15c: armed, not triggered',GL.trail_step(t,0.65,now) is False and t.get('trailArmedAt') and not t.get('trailTriggeredAt'))
chk('peak tracks higher bids',GL.trail_step(t,0.72,now) is False and t['peakBid']==0.72)
chk('9c giveback: no trigger',GL.trail_step(t,0.63,now) is False)
# --- 2. giveback trigger -> real sell, booked, verified
chk('10c giveback from peak triggers (latched)',GL.trail_step(t,0.62,now) is True and t['trailTrigBid']==0.62 and GL.trail_step(t,0.70,now) is True)
t2=T(); t2u=dict(t2); unarmed=GL.trail_step(t2u,0.55,now); GL.trail_step(t2u,0.45,now)
chk('never armed -> a drop does not trigger (holds)',not t2u.get('trailTriggeredAt'))
ex=Ex(pos=1.87); lv=mklive(ex); saves=[]
g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None); g.save=lambda:saves.append(1)
t=T(); bids=iter([0.60,0.67,0.70,0.66,0.60])
for b in [0.60,0.67,0.70,0.66]: g.manage_exits([t],lambda x,b=b:b)
chk('no sell while armed and above giveback',not ex.posts and t['status']=='open' and t.get('trailArmedAt'))
g.manage_exits([t],lambda x:0.60)
chk('giveback -> one real sell order posted',len(ex.posts)==1)
b=ex.posts[0] if ex.posts else {}
# --- 3. sizing
chk('sell size == held contracts (1.87), reduce_only, IOC, YES sold on ask book',b.get('count')=='1.87' and b.get('reduce_only') is True and b.get('time_in_force')=='immediate_or_cancel' and b.get('side')=='ask')
chk('sell limit = bid - 2c (0.58)',b.get('price')=='0.5800')
chk('trade exited at fill, pnl = proceeds - fee - stake',t['status']=='exited' and t['exitPrice']==0.58 and t['settledBy']=='live-trail-sell' and t['pnl']==round(1.87*0.58-0.01-t['stake'],2))
chk('realized P&L booked to daily-loss tracker',lv.booked and abs(lv.booked[0]-round(1.87*0.58-0.01-t['stake'],2))<1e-9)
chk('position verified closed on Kalshi',t.get('positionAfterExit')==0.0)
acts=SELL_ACTS(lv)
chk('order log: trigger, submitted, sell, verify',all(a in acts for a in ('gap-trail-trigger','order-submitted','sell','gap-trail-verify')))
chk('no further orders after exit',(g.manage_exits([t],lambda x:0.5),len(ex.posts))[1]==1)
ex=Ex(pos=1.0); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
t=T(contracts=1.87); g.manage_exits([t],lambda x:0.70); g.manage_exits([t],lambda x:0.60)
chk('Kalshi holds less than tracked -> sells only what Kalshi holds (1.0)',ex.posts and ex.posts[0]['count']=='1.00')
nt=T(); nt['side']='NO'; ex=Ex(pos=-2.0); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
nt['contracts']=2.0; g.manage_exits([nt],lambda x:0.70); g.manage_exits([nt],lambda x:0.60)
chk('NO position: sold on the bid book at YES 1-(bid-2c)',ex.posts and ex.posts[0]['side']=='bid' and ex.posts[0]['price']=='0.4200' and ex.posts[0]['count']=='2.00')
# partial fill: rest keeps selling
ex=Ex(pos=2.0,fill=[1.2,0,0]); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
t=T(contracts=2.0); g.manage_exits([t],lambda x:0.70); g.manage_exits([t],lambda x:0.60)
chk('partial fill: 0.8 left open, part pnl booked',t['status']=='open' and abs(t['contracts']-0.8)<1e-9 and t.get('partialPnl') is not None and len(lv.booked)==1)
ex.fill=None; t['trailSellAt']=0; g.manage_exits([t],lambda x:0.60)
chk('partial fill: next try sells the remaining 0.80 and exits',ex.posts[-1]['count']=='0.80' and t['status']=='exited' and len(lv.booked)==2)
# --- 4. sell failure -> retries capped -> hold to settlement
ex=Ex(pos=1.87,fill=0); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
t=T(); g.manage_exits([t],lambda x:0.70)
for i in range(GL.SELL_TRIES+2):
    t['trailSellAt']=0; g.manage_exits([t],lambda x:0.60)
calls=g.xstat['sellCalls']
chk(f'unfilled sell retried {GL.SELL_TRIES} times (each up to 3 IOCs) then stops',calls==GL.SELL_TRIES and len(ex.posts)<=3*GL.SELL_TRIES and len(ex.posts)>=GL.SELL_TRIES)
chk('fallback: hold to settlement, trade still open, nothing booked',t['status']=='open' and t.get('trailFallback')=='hold to settlement' and t.get('holdToSettle') is True and not lv.booked)
chk('fallback logged in order log',any(o['action']=='gap-trail-fallback' for o in lv.orders))
n=len(ex.posts); g.manage_exits([t],lambda x:0.55)
chk('no more sells after fallback',len(ex.posts)==n)
ex=Ex(pos=1.87,code=400); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
t=T(); g.manage_exits([t],lambda x:0.70)
for i in range(GL.SELL_TRIES+1): t['trailSellAt']=0; g.manage_exits([t],lambda x:0.60)
chk('Kalshi rejects (HTTP 400) -> capped retries -> hold',t['status']=='open' and t.get('trailFallback') and g.xstat['sellCalls']==GL.SELL_TRIES and 'HTTP 400' in str(t.get('trailSellError')))
lv=mklive(Ex(pos=1.87)); lv.sell=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('boom')); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
t=T(); g.manage_exits([t],lambda x:0.70); g.manage_exits([t],lambda x:0.60)
chk('sell exception never raises; trade stays open, retry pending',t['status']=='open' and 'boom' in t.get('trailSellError','') and not t.get('exiting'))
ex=Ex(pos=1.87); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
t=T(close=time.time()+5); t.update(peakBid=0.70,trailArmedAt=1,trailTriggeredAt=1,trailTrigBid=0.6)
g.manage_exits([t],lambda x:0.60)
chk('within last 8s of close: no sell, hold to settlement',not ex.posts and t.get('trailFallback'))
lv2=mklive(Ex(pos=1.87)); lv2.owner=lambda:False; g=GL.GapLive(lv2,lambda *a:None,log=lambda *a:None)
t=T(); g.manage_exits([t],lambda x:0.70); g.manage_exits([t],lambda x:0.60)
chk('no lease -> no order (counts as a try)',not lv2.orders or all(o['action']!='order-submitted' for o in lv2.orders)) 
ht=T(); ht['exitMode']='hold'; ex=Ex(pos=1.87); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
g.manage_exits([ht],lambda x:0.70); g.manage_exits([ht],lambda x:0.50)
chk('test #1 hold-mode trades are never sold',not ex.posts and ht['status']=='open')
# --- 5. cap counter reset (5 NEW trades)
saved=[]
lv=L.Live(store_load=lambda:dict(gapLiveUsed=3,gapLiveDisabled=True,paused=False),store_save=saved.append); lv.owner=lambda:True
g=GL.GapLive(lv,lambda p,f,st:dict(id=1),log=lambda *a:None)
chk('before reset: test #1 counters loaded (3, disabled)',lv.gapLiveUsed==3 and lv.gapLiveDisabled)
chk('reset_for_test runs once',g.reset_for_test() is True and g.reset_for_test() is False)
chk('after reset: used 0, auto-disable cleared, no cap',g.used()==0 and not g.disabled() and not GL._has_cap())
lv.allowed=lambda:True
chk('after reset: live PAUSED (owner must Resume)',lv.paused is True and 'Resume' in str(g.block_reason(dict(rule='A',pair='BTC-ETH',asset='BTC',laggard='BTC',ticker='KXBTC15M-Z',mode='paper',gap=True),now=DAY)))
st=saved[-1] if saved else {}
chk('reset persisted with test id + previous test record',st.get('gapLiveTest')==GL.TEST_ID and st.get('gapLiveUsed')==0 and st.get('paused') is True and st.get('gapLivePrev',[{}])[-1].get('used')==3)
lv2=L.Live(store_load=lambda:st); g2=GL.GapLive(lv2,lambda *a:None,log=lambda *a:None)
lv2.gapLiveUsed=2
chk('restart after reset: no second reset, count kept',g2.reset_for_test() is False and g2.used()==2)
class F:
    def __init__(s): s.gapLiveUsed=0; s.gapLiveDisabled=False; s.calls=[]; s.paused=False
    def enabled(s): return True
    def allowed(s): return True
    def persist(s): pass
    def buy(s,asset,tk,side,frac,ref,**kw): s.calls.append(kw); return dict(contracts=2.0,price=0.5,fee=0.02,cost=1.0,orderId=f'o{len(s.calls)}',cash=20,frac=1,side=side)
f=F(); opened=[]; g=GL.GapLive(f,lambda p,fl,st:(opened.append(dict(p)),dict(id=len(opened)))[1],log=lambda *a:None)
P=lambda i,**k:dict(dict(id=f'G{i}',mode='paper',shadow=False,gap=True,rule='A',pair='BTC-ETH',asset='BTC',laggard='BTC',ticker=f'KXBTC15M-C{i}',side='YES',price=0.5,stake=1.0,conf='normal',closesAt=9e9),**k)
for i in range(5): g.on_paper(P(i),now=DAY)
chk('5 fills do not auto-disable (no cap)',f.gapLiveUsed==5 and not f.gapLiveDisabled and len(f.calls)==5 and g.active())
chk('6th signal still fills',g.on_paper(P(6),now=DAY) is not None and len(f.calls)==6 and f.gapLiveUsed==6 and not f.gapLiveDisabled)
chk('opened trades tagged trail exit + test id',opened and opened[0].get('_gapLiveExit')=='trail' and opened[0].get('_gapLiveTest')==GL.TEST_ID)
# --- 6. strong tier never live
f=F(); g=GL.GapLive(f,lambda *a:dict(id=1),log=lambda *a:None)
g.on_paper(P(1,stake=3.0,conf='strong'),now=DAY)
chk('strong $3 paper signal -> live usd=$1',f.calls and f.calls[-1].get('usd')==1.0)
lb=L.Live(); lb.enabled=lambda:True; lb.owner=lambda:True; lb.balance=lambda:50.0; lb._quote=lambda tk,side,k:0.50
ords=[]; lb._order=lambda tk,side,act,n,px,confirm=None:(ords.append(n),(200,n,px,0.02,'o',None))[1]; lb.pending=lambda *a,**k:None; lb.log=lambda **k:None; lb.verify=lambda *a:None
lb.buy('BTC','KXBTC15M-S1','YES',1.0,0.50,usd=3.0)
chk('even usd=3 is capped at HARD_MAX_USD $2 in Live.buy',ords and ords[-1]==4.0)
# --- 7. XRP laggard block
f=F(); g=GL.GapLive(f,lambda *a:dict(id=1),log=lambda *a:None)
chk('SOL-XRP with XRP laggard blocked, no order',g.on_paper(P(1,pair='SOL-XRP',asset='XRP',laggard='XRP',ticker='KXXRP15M-X1'),now=DAY) is None and not f.calls and 'XRP' in g.stat['lastReason'])
chk('SOL-XRP with SOL laggard allowed',g.on_paper(P(2,pair='SOL-XRP',asset='SOL',laggard='SOL',ticker='KXSOL15M-X2'),now=DAY) is not None and len(f.calls)==1)
ex=Ex(pos=1.0); lv=mklive(ex); g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None)
xt=T(tk='KXXRP15M-T1'); g.manage_exits([xt],lambda x:0.70); g.manage_exits([xt],lambda x:0.60)
chk('XRP ticker never sold by trail; Live.sell XRP hard block intact',not ex.posts and lv.sell('KXXRP15M-T1','YES',1,0.6)[4].startswith('blocked'))
# --- /api/gaplive summary + server wiring
lv=L.Live(store_load=lambda:dict(gapLiveUsed=3,gapLiveDisabled=True)); lv.owner=lambda:True
g=GL.GapLive(lv,lambda *a:None,log=lambda *a:None); g.reset_for_test(); sm=g.summary([])
chk('summary: trail exit, no cap, used 0, live paused, session 4',sm['exitConfig']['mode']=='trail' and sm['exitConfig']['armC']==15 and sm['exitConfig']['givebackC']==10 and sm['max'] is None and sm['used']==0 and sm['liveEnabled'] is False and sm['liveEngine']['paused'] is True and sm['test']=='4' and sm['previousTests'][-1]['used']==3 and sm['moogleGate']['bar']==0.65 and sm['moogleGate']['staleSec']==60 and sm['midnightSkip'] is True and sm['metals']['minGap']==0.06)
src=open('server.py').read()
chk('server: openLiveGap records exitMode/test id',"exitMode=p.get('_gapLiveExit') or 'hold',gapLiveTest=p.get('_gapLiveTest')" in src)
chk('server: manage_exits wired in tick under lock',"with lock: GAPLIVE.manage_exits(pt.trades,lambda t:_gap_live_bid(t,_gm))" in src)
chk('server: reset_for_test + lock/save wiring',"GAPLIVE.reset_for_test()" in src and "GAPLIVE.tlock=lock; GAPLIVE.save=pt.save" in src)
chk('server: generic exit paths still skip gap-live',src.count("if t.get('gapLive'): return  # gap-live holds to settlement")==2)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
