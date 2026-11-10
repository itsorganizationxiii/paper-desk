"""Offline: gap-live hot-path speed (owner cache, async log, balance cache/prefetch, stage timings, async dispatch, ws callback decoupling)."""
import os, time, json, threading
os.environ['LIVE_TRADING']='0'
import live as L, gaplive as GL, kws
GL.moogle_side_p=lambda *a,**k:0.70
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
class FakeWS:
    def __init__(s): s.connected=True; s.q={}; s.want=set()
    def watch(s,tk): s.want.add(tk)
    def quote(s,tk,maxage=3.0):
        v=s.q.get(tk); return v if v and time.time()-v['rx']<=maxage and time.time()-v['ts']<=maxage+2 else None
    def info(s,tk):
        v=s.q.get(tk); return dict(connected=True,watched=True,have=bool(v),rxAge=0 if v else None,tsAge=round(time.time()-v['ts'],1) if v else None,fresh=s.quote(tk) is not None)
    def status(s): return dict(connected=True)
def mk(rest=0.50,bal_sleep=0.0,order_sleep=0.0):
    lb=L.Live(); lb.ws=FakeWS(); lb.allowed=lambda:True; lb.paused=False
    lb.lease_ok=lambda:True
    lb.bal_calls=0
    def balance():
        lb.bal_calls+=1; time.sleep(bal_sleep); lb.cash=50.0; lb.cashAt=time.time(); return 50.0
    lb.balance=balance; lb._rest_ask=lambda tk,side:(time.sleep(0.3),rest)[1]
    lb.sent=[]
    def _order(tk,side,act,n,px,confirm=None): time.sleep(order_sleep); lb.sent.append((tk,n,px)); return 200,n,px,0.02,'o1',None
    lb._order=_order; lb.pending=lambda *a,**k:None; lb.verify=lambda *a,**k:None; lb._save=None
    return lb
# 1. owner() lease cache (TTL 45s >> cache 10s); failures not cached
lb=L.Live(); n=[0]
def lease(): n[0]+=1; time.sleep(0.05); return True
lb.lease_ok=lease
t0=time.time()
for i in range(50): lb.owner()
chk('owner cached: 50 calls -> 1 lease check',n[0]==1 and time.time()-t0<0.5 and L.OWNER_CACHE_SEC<=10)
lb._ownOkAt=time.time()-11; lb.owner()
chk('owner re-checks after cache expiry',n[0]==2)
lb.lease_ok=lambda:False; lb._ownOkAt=time.time()-11
chk('lost lease -> False immediately after expiry, not cached',lb.owner() is False and lb.owner() is False and lb._ownOkAt==0)
# 2. log() never blocks on Mongo
lb=L.Live(); ins=[]; saves=[]
class Col:
    def insert_one(s,d): time.sleep(0.5); ins.append(d)
lb.orderlog=Col(); lb.lease_ok=lambda:True; lb._save=lambda st:(time.sleep(0.5),saves.append(1))
t0=time.time(); lb.log(action='buy-skip',ticker='KXBTC15M-Z',error='x'); dt=time.time()-t0
chk('log() returns without waiting for Mongo (<50ms)',dt<0.05 and lb.orders[-1]['action']=='buy-skip')
lb.flush_log(5); time.sleep(1.2)
chk('deferred orderlog insert + persist still happen',ins and ins[-1]['action']=='buy-skip' and saves)
# 3. warm cash cache: fixed $1 gap stake does not call balance
lb=mk(bal_sleep=5.0); lb.cash=20.0; lb.cashAt=time.time()-120
tk='KXBTC15M-W1'; lb.ws.q[tk]=dict(yesBid=0.49,yesAsk=0.50,rx=time.time(),ts=time.time())
t0=time.time(); f=lb.buy('BTC',tk,'YES',1.0,0.50,usd=1.0,quote_wait=1.5); dt=time.time()-t0
chk('warm cache: buy does not block on balance',f and lb.bal_calls==0 and dt<0.3)
st=lb.lastBuyTiming
chk('stage timings recorded (guards/quote/balance/price/order)',all(k in st for k in ('guards','quote','balance','price','order')))
# cache too old or cash < $2 -> balance called as before
lb=mk(); lb.cash=1.5; lb.cashAt=time.time()-60; tk='KXBTC15M-W2'; lb.ws.q[tk]=dict(yesBid=0.49,yesAsk=0.50,rx=time.time(),ts=time.time())
lb.buy('BTC',tk,'YES',1.0,0.50,usd=1.0,quote_wait=1.5)
chk('cash < $2 -> balance refreshed',lb.bal_calls==1)
lb=mk(); lb.cash=20; lb.cashAt=time.time()-400; tk='KXBTC15M-W3'; lb.ws.q[tk]=dict(yesBid=0.49,yesAsk=0.50,rx=time.time(),ts=time.time())
lb.buy('BTC',tk,'YES',1.0,0.50,usd=1.0,quote_wait=1.5)
chk('cache > 5 min -> balance refreshed',lb.bal_calls==1)
# 4. cold cache: balance runs in parallel with the REST quote (WS quote stale by ts -> no wait, straight to REST)
lb=mk(bal_sleep=0.4); lb.cash=None; tk='KXBTC15M-P1'; lb.ws.q[tk]=dict(yesBid=0.4,yesAsk=0.41,rx=time.time(),ts=time.time()-100)
t0=time.time(); f=lb.buy('BTC',tk,'YES',1.0,0.50,usd=1.0,quote_wait=1.5); dt=time.time()-t0
chk('lagged WS quote (have) -> no 1.5s wait, REST used',f and lb.sent and dt<0.65)
chk('balance prefetched in parallel (total < balance+REST)',lb.bal_calls==1 and dt<0.65)
chk('REST ask reused for pricing (one REST call)',lb.sent[-1][2]==0.5)
# 5. MAX_CHASE unchanged and still enforced on the current ask
lb=mk(rest=0.83); lb.cash=20; lb.cashAt=time.time(); tk='KXBTC15M-M1'
r=lb.buy('BTC',tk,'YES',1.0,0.72,usd=1.0,quote_wait=0.1)
chk('MAX_CHASE 5c still blocks ask 0.83 vs signal 0.72',L.MAX_CHASE==0.05 and r is None and lb.lastBuy=='ask 0.83 above max 0.77 (signal 0.72)' and not lb.sent)
chk('HARD_MAX_USD unchanged',L.HARD_MAX_USD==2.0)
# 6. gaplive async dispatch: tick loop not blocked; recentAttempts carry stages; cap reservation
lb=mk(order_sleep=0.8); lb.cash=20; lb.cashAt=time.time(); lb.persist=lambda:None
emitted=[]
g=GL.GapLive(lb,lambda p,f,st:dict(id=9),log=lambda *a:None); g.async_=True; g.emit=lambda p:emitted.append(p.get('liveTwinId'))
P=lambda tk,asset='BTC':dict(id='G',mode='paper',shadow=False,gap=True,rule='A',pair='BTC-ETH' if asset!='SOL' else 'SOL-XRP',asset=asset,laggard=asset,ticker=tk,side='YES',price=0.50,openedAt=time.time())
for tk in ('KXBTC15M-A1','KXSOL15M-A1'): lb.ws.q[tk]=dict(yesBid=0.49,yesAsk=0.50,rx=time.time(),ts=time.time())
t0=time.time(); g.on_paper(P('KXBTC15M-A1')); g.on_paper(P('KXSOL15M-A1','SOL')); dt=time.time()-t0
chk('on_paper returns immediately (buy on its own thread)',dt<0.1 and g.inflight==2)
time.sleep(1.5)
chk('both buys ran in parallel and filled; cap counted',g.used()==2 and g.inflight==0 and len(lb.sent)==2)
chk('paper twin re-emitted with liveTwinId',emitted==[9,9])
ra=g.summary([])['recentAttempts'][0]
chk('recentAttempts include stage timings',isinstance(ra.get('stages'),dict) and 'order' in ra['stages'] and ra['buySec']<1.5)
lb.gapLiveUsed=GL.MAX_TRADES-1; lb.tried.clear()
for tk in ('KXBTC15M-A2','KXSOL15M-A2'): lb.ws.q[tk]=dict(yesBid=0.49,yesAsk=0.50,rx=time.time(),ts=time.time())
g.on_paper(P('KXBTC15M-A2')); g.on_paper(P('KXSOL15M-A2','SOL'))
chk('cap reservation: max-1 used + 1 in flight blocks the 2nd signal',g.inflight==1 and 'cap reserved' in g.stat['lastReason'])
time.sleep(1.2)
chk('cap reached at max, auto-disabled',g.used()==GL.MAX_TRADES and lb.gapLiveDisabled and len(lb.sent)==3)
# 7. kws: slow on_quote callback no longer delays the quote cache
f=kws.Feed('ws://127.0.0.1:1/x',lambda:{}); f.on_quote=lambda tk,q:time.sleep(0.5)
now=time.time(); t0=time.time()
for i in range(5):
    f._msg(json.dumps({'type':'ticker','msg':{'market_ticker':'KXBTC15M-K','yes_bid_dollars':'0.45','yes_ask_dollars':'0.46','ts_ms':int(time.time()*1000)}}))
dt=time.time()-t0
chk('5 messages with a 0.5s callback processed in <50ms; quote fresh',dt<0.05 and f.quote('KXBTC15M-K') is not None and f.status()['lag'] is not None)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
