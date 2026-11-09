"""Offline: Live.buy records a specific skip reason (diagnostics t1022); no network, no real orders."""
import os
os.environ['LIVE_TRADING']='0'
import live as L, gaplive as GL
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
def mk(q=0.50):
    lb=L.Live(); lb.enabled=lambda:True; lb.owner=lambda:True; lb.balance=lambda:50.0; lb._quote=lambda tk,side,k:q
    lb.orders_sent=[]; lb._order=lambda tk,side,act,n,px,confirm=None:(lb.orders_sent.append(n),(200,0,None,0.0,'o',None))[1]
    lb.pending=lambda *a,**k:None; lb.log=lambda **k:None; return lb
# stale Kalshi quote
lb=mk(); lb.ws=type('W',(),{})(); lb.wsq=lambda tk:None; lb.lastBuy='edge'
chk('stale quote -> None',lb.buy('BTC','KXBTC15M-S1','YES',1.0,0.76,usd=1.0) is None)
chk('stale quote -> lastBuy specific',lb.lastBuy=='stale Kalshi quote' and lb.lastBuyDetail=='stale Kalshi quote' and not lb.orders_sent)
# ask above max (ref 0.76 + MAX_CHASE 0.05 = 0.81)
lb=mk(q=0.84)
chk('ask above max -> None, no order',lb.buy('BTC','KXBTC15M-S2','YES',1.0,0.76,usd=1.0) is None and not lb.orders_sent)
chk('ask above max -> reason text',lb.lastBuy=='ask 0.84 above max 0.81 (signal 0.76)')
# already tried
chk('already tried',lb.buy('BTC','KXBTC15M-S2','YES',1.0,0.76,usd=1.0) is None and lb.lastBuy=='already tried this ticker')
# blocked keeps exact code
chk('XRP blocked keeps code',lb.buy('XRP','KXXRP15M-S3','YES',1.0,0.5,usd=1.0) is None and lb.lastBuy=='blocked' and 'XRP' in lb.lastBuyDetail)
# nofill keeps exact code
lb=mk(q=0.50)
chk('IOC no fill keeps nofill code',lb.buy('BTC','KXBTC15M-S4','YES',1.0,0.50,usd=1.0) is None and lb.lastBuy=='nofill' and lb.lastBuyDetail.startswith('no fill'))
# edge keeps exact code
lb=mk(q=0.60)
chk('edge keeps code',lb.buy('BTC','KXBTC15M-S5','YES',1.0,0.50,sizer=lambda px:None) is None and lb.lastBuy=='edge' and 'KXBTC15M-S5' not in lb.tried)
# balance error: type only
lb=mk(); lb.cash=None
def boom(): raise RuntimeError('secret-ish detail')
lb.balance=boom
chk('balance error type only',lb.buy('BTC','KXBTC15M-S6','YES',1.0,0.5,usd=1.0) is None and lb.lastBuy=='balance error: RuntimeError')
# successful buy resets reason
lb=mk(q=0.50); lb._order=lambda tk,side,act,n,px,confirm=None:(200,n,px,0.02,'o',None); lb.verify=lambda *a,**k:None; lb.lastBuy='stale Kalshi quote'
chk('fill resets lastBuy',lb.buy('BTC','KXBTC15M-S7','YES',1.0,0.5,usd=1.0) and lb.lastBuy is None)
# gaplive surfaces reason + ring buffer + safe order log
lb=mk(q=0.84); lb.orders=[dict(at=1,action='buy-skip',ticker='KXBTC15M-S8',side='YES',asset='BTC',error='<0.01 contract affordable (cash $9.99, frac 1, px .8)',orderId='x',clientId='y')]
g=GL.GapLive(lb,lambda p,f,st:None,log=lambda *a:None)
p=dict(id='G',mode='paper',shadow=False,gap=True,rule='A',pair='BTC-ETH',asset='BTC',laggard='BTC',ticker='KXBTC15M-S8',side='YES',price=0.76)
chk('gaplive no fill',g.on_paper(p) is None and g.stat['lastReason']=='no fill: ask 0.84 above max 0.81 (signal 0.76)')
sm=g.summary([])
r=sm['recentAttempts']
chk('recentAttempts record',len(r)==1 and r[0]['reason'].startswith('ask 0.84') and r[0]['filled']==0 and r[0]['signalPrice']==0.76 and r[0]['pair']=='BTC-ETH')
ro=sm['recentOrders']
chk('recentOrders safe fields only, cash scrubbed',ro and set(ro[0])<= set(GL._SAFE) and 'cash' not in str(ro) and 'orderId' not in ro[0])
for i in range(15): g._rec(p,'x',0)
chk('ring buffer capped at 10',len(g.summary([])['recentAttempts'])==10)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
