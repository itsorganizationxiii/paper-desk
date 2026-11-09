"""Offline: gap-live WS pre-subscribe, first-quote wait, REST fallback, capped stale retries, staleN rules. No network/orders."""
import os, time
os.environ['LIVE_TRADING']='0'
import live as L, gaplive as GL, kws
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
class FakeWS:
    def __init__(s,connected=True): s.connected=connected; s.q={}; s.want=set(); s.watches=[]; s.arrive=None
    def watch(s,tk): s.watches.append(tk); s.want.add(tk)
    def quote(s,tk,maxage=3.0):
        if s.arrive and time.time()>=s.arrive[0]: s.q[tk]=dict(yesBid=0.49,yesAsk=0.50,rx=time.time(),ts=time.time())
        v=s.q.get(tk)
        return v if v and time.time()-v['rx']<=maxage else None
    def info(s,tk):
        v=s.q.get(tk); return dict(connected=s.connected,watched=tk in s.want,have=bool(v),rxAge=round(time.time()-v['rx'],2) if v else None,tsAge=None,fresh=s.quote(tk) is not None)
    def status(s): return dict(connected=s.connected,subscribed=len(s.want),quotes=len(s.q),lastMsgAge=0.1,reconnects=0,error=None)
def mk(ws=None,rest=None):
    lb=L.Live(); lb.ws=ws or FakeWS(); lb.enabled=lambda:True; lb.owner=lambda:True; lb.balance=lambda:50.0
    lb._rest_ask=lambda tk,side:rest; lb.market=lambda tk:({'yes_ask_dollars':str(rest),'yes_bid_dollars':str(rest-0.01)} if rest else {})
    lb.sent=[]
    def _order(tk,side,act,n,px,confirm=None): lb.sent.append((tk,n,px)); return 200,n,px,0.02,'o1',None
    lb._order=_order; lb.pending=lambda *a,**k:None; lb.log=lambda **k:None; lb.verify=lambda *a,**k:None; lb.persist=lambda:None
    return lb
# 1. pre-subscribe on round roll (read-only watch of BTC/ETH/SOL, never XRP)
lb=mk(); g=GL.GapLive(lb,lambda p,f,st:dict(id=1),log=lambda *a:None)
g.presubscribe({'BTC':{'ticker':'KXBTC15M-R1'},'ETH':{'ticker':'KXETH15M-R1'},'SOL':{'ticker':'KXSOL15M-R1'},'XRP':{'ticker':'KXXRP15M-R1'},'GOLD':None})
chk('presubscribe watches BTC/ETH/SOL current tickers',set(lb.ws.watches)=={'KXBTC15M-R1','KXETH15M-R1','KXSOL15M-R1'})
g.presubscribe({'BTC':{'ticker':'KXBTC15M-R2'},'ETH':{'ticker':'KXETH15M-R2'},'SOL':{'ticker':'KXSOL15M-R2'}})
chk('round roll -> new tickers watched',g.watched=={'BTC':'KXBTC15M-R2','ETH':'KXETH15M-R2','SOL':'KXSOL15M-R2'} and 'KXBTC15M-R2' in lb.ws.want)
chk('presubscribe places no orders',not lb.sent)
wf=g.summary([])['wsQuoteFresh']
chk('wsQuoteFresh exposed per ticker (no prices)',wf['BTC']['ticker']=='KXBTC15M-R2' and wf['BTC']['fresh'] is False and 'yesAsk' not in str(wf) and wf['feed']['connected'])
# 2. first quote arrives during the wait -> trades on it
ws=FakeWS(); ws.arrive=(time.time()+0.6,); lb=mk(ws)
f=lb.buy('BTC','KXBTC15M-W1','YES',1.0,0.50,usd=1.0,quote_wait=3.0)
chk('waits for first WS quote then buys',f and lb.sent and lb.staleN==0)
# 3. first-quote miss (connected, never had a quote), REST down -> skip, retryable, staleN NOT incremented
lb=mk(rest=None); lb.staleN=1
r=lb.buy('BTC','KXBTC15M-F1','YES',1.0,0.50,usd=1.0,quote_wait=0.3)
chk('first-quote miss -> None, no order',r is None and not lb.sent and lb.lastBuy=='stale Kalshi quote' and 'no first WS quote' in lb.lastBuyDetail)
chk('first-quote miss does not count toward kill',lb.staleN==1 and not lb.killed)
chk('stale skip discards ticker for retry',lb.lastBuyRetry and 'KXBTC15M-F1' not in lb.tried)
# 4. genuine stale (quote existed, aged) increments; 3 in a row still kills (STALE_KILL_N unchanged)
ws=FakeWS(); lb=mk(ws,rest=None)
for i in range(3):
    ws.q[f'KXBTC15M-G{i}']=dict(yesBid=0.4,yesAsk=0.41,rx=time.time()-20,ts=time.time()-20)
    lb.buy('BTC',f'KXBTC15M-G{i}','YES',1.0,0.50,usd=1.0,quote_wait=0.1)
chk('genuine stale increments staleN',lb.staleN==3 and 'WS quote age' in lb.lastBuyDetail)
chk('STALE_KILL_N=3 kill still fires on genuine stale',L.STALE_KILL_N==3 and lb.killed and not lb.sent)
lb=mk(FakeWS(connected=False),rest=None); lb.buy('BTC','KXBTC15M-D1','YES',1.0,0.5,usd=1.0,quote_wait=0.1)
chk('WS disconnected counts as genuine stale',lb.staleN==1 and 'disconnected' in lb.lastBuyDetail)
# 5. REST fallback: WS stale, REST public quote live -> buys, staleN untouched
lb=mk(rest=0.50); lb.staleN=2
f=lb.buy('BTC','KXBTC15M-R9','YES',1.0,0.50,usd=1.0,quote_wait=0.1)
chk('REST fallback buys when WS has no quote',f and lb.sent and lb.staleN==2 and not lb.killed)
lb=mk(rest=0.60); r=lb.buy('BTC','KXBTC15M-R8','YES',1.0,0.50,usd=1.0,quote_wait=0.1)
chk('REST price still bound by MAX_CHASE guard',r is None and not lb.sent and lb.lastBuy.startswith('ask 0.6 above max 0.55'))
# 6. non-gap callers (quote_wait=0) keep the old behavior: no wait, no REST, no retry
lb=mk(rest=0.50); t0=time.time(); r=lb.buy('BTC','KXBTC15M-O1','YES',1.0,0.50,usd=1.0)
chk('default path unchanged (no REST, stays tried)',r is None and not lb.sent and 'KXBTC15M-O1' in lb.tried and not lb.lastBuyRetry and time.time()-t0<0.2)
# 7. live-side retry cap per ticker
lb=mk(rest=None)
for i in range(6): lb.buy('BTC','KXBTC15M-C1','YES',1.0,0.50,usd=1.0,quote_wait=0.05)
chk('per-ticker cap: 5th stale keeps ticker tried',lb.staleRetry['KXBTC15M-C1']==5 and 'KXBTC15M-C1' in lb.tried and lb.lastBuy=='already tried this ticker')
# 8. gaplive retries on later ticks, capped at 5 total tries, then dropped; no cap usage
GL.QUOTE_WAIT=0.05
lb=mk(rest=None); calls=[]
ob=lb.buy
def spy(*a,**k): calls.append(1); return ob(*a,**k)
lb.buy=spy
g=GL.GapLive(lb,lambda p,f,st:dict(id=1),log=lambda *a:None)
P=lambda tk:dict(id='G',mode='paper',shadow=False,gap=True,rule='A',pair='BTC-ETH',asset='BTC',laggard='BTC',ticker=tk,side='YES',price=0.50,openedAt=time.time())
g.on_paper(P('KXBTC15M-T1'))
chk('stale signal queued for retry','KXBTC15M-T1' in g.pending and len(calls)==1)
for i in range(10): g.tick()
chk('retries capped at 5 tries total',len(calls)==5 and not g.pending and g.used()==0)
# 9. retry succeeds once the quote shows up
lb=mk(rest=None); g=GL.GapLive(lb,lambda p,f,st:dict(id=7),log=lambda *a:None)
g.on_paper(P('KXBTC15M-T2'))
lb.ws.q['KXBTC15M-T2']=dict(yesBid=0.49,yesAsk=0.50,rx=time.time(),ts=time.time())
g.tick()
chk('retry fills after quote arrives; counts 1/3',lb.sent and g.used()==1 and not g.pending and g.stat['filled']==1)
chk('recentAttempts carry retry/latency',g.recent[-1]['reason']=='filled' and g.recent[-1]['retry']==1 and 'buySec' in g.recent[-1])
# 10. retry dropped when live is paused meanwhile
lb=mk(rest=None); g=GL.GapLive(lb,lambda p,f,st:dict(id=1),log=lambda *a:None)
g.on_paper(P('KXBTC15M-T3')); lb.enabled=lambda:False; n=len(lb.sent); g.tick()
chk('paused -> pending retry dropped, no order',not g.pending and len(lb.sent)==n and 'Resume' in g.recent[-1]['reason'])
# 11. kws prune keeps the most recently watched (current) tickers, not alphabetical
f=kws.Feed.__new__(kws.Feed); import threading; f.lock=threading.Lock(); f.want=set(); f.wantAt={}; f.ws=None; f.connected=False
for i in range(41): f.watch(f'KXXRP15M-OLD{i:02d}')
f.watch('KXBTC15M-NOW'); f._prune()
chk('kws prune keeps newest (KXBTC current kept)','KXBTC15M-NOW' in f.want and len(f.want)==20)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
