"""Offline checks for gap shadow (no network)."""
import tempfile, gapshadow as G
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
now=1000.0
def m(tk,yb,ya,close=1600.0): return dict(ticker=tk,close=close,yesBid=yb,yesAsk=ya,noBid=round(1-ya,4),noAsk=round(1-yb,4))
g=m('KXGOLD-1',0.19,0.21); s=m('KXSILVER-1',0.48,0.51)   # gold Down .80, silver .505 -> gap ~.295
t=G.decide('GOLD','SILVER',g,s,now)
chk('opens on gap>=0.20',t is not None)
chk('leader gold, laggard silver, side NO',t and t['leader']=='GOLD' and t['laggard']=='SILVER' and t['side']=='NO')
chk('entry = silver noAsk 0.52',t and abs(t['entry']-0.52)<1e-9)
chk('fee formula',abs(G.fee(1/0.52,0.52)-0.04)<1e-9 and t['entryFee']==G.fee(1/0.52,0.52))
chk('tagged shadow gapshadow',t['mode']=='shadow' and t['shadow'] and t['signalSource']=='gapshadow')
chk('small gap skipped',G.decide('GOLD','SILVER',m('a',0.40,0.42),m('b',0.48,0.51),now) is None)
chk('different window skipped',G.decide('GOLD','SILVER',g,m('b',0.48,0.51,close=2500),now) is None)
chk('too late skipped',G.decide('GOLD','SILVER',g,s,1600-60) is None)
chk('too early skipped',G.decide('GOLD','SILVER',g,s,1600-800) is None)
chk('wide spread skipped',G.decide('GOLD','SILVER',g,m('b',0.44,0.51),now) is None)
chk('price band skipped',G.decide('GOLD','SILVER',m('a',0.01,0.03),m('b',0.92,0.94),now) is None)
G.load(tempfile.mkdtemp())
o1=G.on_tick({'GOLD':g,'SILVER':s},now); o2=G.on_tick({'GOLD':g,'SILVER':s},now+10)
chk('one per pair per window',len(o1)==1 and len(o2)==0)
G.settle(lambda tk:'no',now+700)
x=G.TRADES[0]
chk('settles won w/ hold_pnl',x['status']=='won' and x['pnl']==x['hold_pnl'] and x['pnl']>0)
chk('garbage input safe',G.on_tick({'BTC':{'close':None}},now)==[])
chk('pairs exactly BTC-ETH,GOLD-SILVER,SOL-XRP',set(G.PAIRS)=={('BTC','ETH'),('GOLD','SILVER'),('SOL','XRP')} and len(G.PAIRS)==3)
chk('no cross pairs',not any(set(p)&{'BTC','ETH'} and set(p)&{'SOL','XRP'} for p in G.PAIRS))
chk('env parser',G._pairs('btc-eth, sol-xrp,bad,x-x')==(('BTC','ETH'),('SOL','XRP')))
o3=G.on_tick({'BTC':m('KXBTC-9',0.19,0.21,close=2600),'SOL':m('KXSOL-9',0.48,0.51,close=2600)},2000.0)
chk('BTC-SOL not traded',o3==[])
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
