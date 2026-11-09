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
chk('one per rule per pair per window',sorted(t['rule'] for t in o1)==['A','gap20'] and len(o2)==0)
G.settle(lambda tk:'no',now+700)
x=G.TRADES[0]
chk('settles won w/ hold_pnl',x['status']=='won' and x['pnl']==x['hold_pnl'] and x['pnl']>0)
chk('garbage input safe',G.on_tick({'BTC':{'close':None}},now)==[])
chk('pairs exactly BTC-ETH,GOLD-SILVER,SOL-XRP',set(G.PAIRS)=={('BTC','ETH'),('GOLD','SILVER'),('SOL','XRP')} and len(G.PAIRS)==3)
chk('no cross pairs',not any(set(p)&{'BTC','ETH'} and set(p)&{'SOL','XRP'} for p in G.PAIRS))
chk('env parser',G._pairs('btc-eth, sol-xrp,bad,x-x')==(('BTC','ETH'),('SOL','XRP')))
o3=G.on_tick({'BTC':m('KXBTC-9',0.19,0.21,close=2600),'SOL':m('KXSOL-9',0.48,0.51,close=2600)},2000.0)
chk('BTC-SOL not traded',o3==[])

# ---- v2 rules ----
import importlib; G=importlib.reload(G); G.load(tempfile.mkdtemp())
C=1900.0
def mm(tk,y,close=C,sp=0.02): return m(tk,round(y-sp/2,4),round(y+sp/2,4),close)
# Rule A: ETH 58 vs BTC 46 (12pt gap) -> ETH leader (Up), buy BTC YES; needs SOL/XRP/DOGE majority Up
base={'BTC':mm('KXBTC',0.46),'ETH':mm('KXETH',0.58)}
t=G.decide_a('BTC','ETH',dict(base,SOL=mm('S',0.6),XRP=mm('X',0.55),DOGE=mm('D',0.3)),C-400)
chk('A: small 12pt gap trades, laggard BTC YES',t and t['rule']=='A' and t['laggard']=='BTC' and t['side']=='YES' and abs(t['entry']-0.47)<1e-9)
chk('A: confirm recorded 2/3',t and t['confirm']['agree']==2 and t['confirm']['of']==3)
chk('A: no confirmation -> skip',G.decide_a('BTC','ETH',dict(base,SOL=mm('S',0.4),XRP=mm('X',0.45),DOGE=mm('D',0.3)),C-400) is None)
chk('A: tie (1/2) -> skip',G.decide_a('BTC','ETH',dict(base,SOL=mm('S',0.6),XRP=mm('X',0.45)),C-400) is None)
chk('A: no other coins -> skip',G.decide_a('BTC','ETH',base,C-400) is None)
chk('A: gap <3pt skip',G.decide_a('BTC','ETH',{'BTC':mm('a',0.56),'ETH':mm('b',0.58),'SOL':mm('S',.6),'XRP':mm('X',.6)},C-400) is None)
chk('A: gold-silver no confirmation needed',G.decide_a('GOLD','SILVER',{'GOLD':mm('g',0.30),'SILVER':mm('s',0.38)},C-400)['side']=='NO')
chk('A: leftSec gate',G.decide_a('GOLD','SILVER',{'GOLD':mm('g',0.30),'SILVER':mm('s',0.38)},C-50) is None and G.decide_a('GOLD','SILVER',{'GOLD':mm('g',0.30),'SILVER':mm('s',0.38)},C-800) is None)
chk('A: 10-90c gate',G.decide_a('GOLD','SILVER',{'GOLD':mm('g',0.02),'SILVER':mm('s',0.95)},C-400) is None)
# Rule B: needs history
G.HIST.clear()
def tickset(sol,xrp,btc,eth=0.6,doge=None,close=C):
    d={'SOL':mm('KXSOL',sol,close),'XRP':mm('KXXRP-B',xrp,close),'BTC':mm('KXBTC',btc,close),'ETH':mm('KXETH',eth,close)}
    if doge is not None: d['DOGE']=mm('KXDOGE',doge,close)
    return d
G.record(tickset(0.75,0.30,0.60),C-500)
chk('B: fires on SOL74+/XRP rise/BTC55+',(lambda t:t and t['rule']=='B' and t['side']=='YES' and t['laggard']=='XRP' and t['stake']==1.0)(G.decide_b(tickset(0.76,0.33,0.60),C-455)))
chk('B: no XRP rise -> skip',G.decide_b(tickset(0.76,0.30,0.60),C-455) is None)
chk('B: BTC<=55 -> skip',G.decide_b(tickset(0.76,0.33,0.55),C-455) is None)
chk('B: SOL falling -> skip',G.decide_b(tickset(0.74,0.33,0.60),C-455)['solYes']==0.74 if False else G.decide_b(tickset(0.72,0.33,0.60),C-455) is None)
chk('B: XRP>58 -> skip',G.decide_b(tickset(0.80,0.60,0.60),C-455) is None)
chk('B: no history -> skip',G.decide_b(tickset(0.76,0.33,0.60),C-700) is None)
# B exit: open via on_tick then cash out late
G.load(tempfile.mkdtemp()); G.TRADES.clear(); G._seen.clear(); G.HIST.clear()
G.on_tick(tickset(0.75,0.30,0.60,eth=0.4),C-500); o=G.on_tick(tickset(0.76,0.33,0.60,eth=0.4),C-455)
b=[t for t in o if t['rule']=='B']; chk('B: opened via on_tick',len(b)==1)
G.on_tick(tickset(0.9,0.6,0.7,eth=0.4),C-300); chk('B: not cashed while ETH Down',b[0]['status']=='open')
G.on_tick(tickset(0.9,0.6,0.7,eth=0.8),C-290)
chk('B: cashed at bid in last 6min, profit',b[0]['status']=='cashed' and b[0]['exit']==0.59 and b[0]['pnl']>0)
G.settle(lambda tk:'no',C+10)
chk('B: hold_pnl recorded after cash',b[0]['hold_pnl']==-1.0 and b[0]['status']=='cashed' and b[0]['pnl']>0)
# B no cash when XRP>=90
G.TRADES.clear(); G._seen.clear(); G.HIST.clear()
G.on_tick(tickset(0.75,0.30,0.60,eth=0.4),C-500); G.on_tick(tickset(0.76,0.33,0.60,eth=0.4),C-455)
G.on_tick(tickset(0.95,0.92,0.9,eth=0.9),C-200); bb=[t for t in G.TRADES if t['rule']=='B'][0]
chk('B: XRP>=90 -> hold',bb['status']=='open')
G.settle(lambda tk:'yes',C+10); chk('B: held to settle won',bb['status']=='won' and bb['pnl']==bb['hold_pnl']>0)
# Rule C
G.HIST.clear()
G.record(tickset(0.9,0.5,0.9,eth=0.9,doge=0.88),C-300)
tc=G.decide_c(tickset(0.91,0.5,0.92,eth=0.9,doge=0.89),C-250)
chk('C: fires $0.10 XRP YES',tc and tc['rule']=='C' and tc['stake']==0.10 and tc['side']=='YES' and tc['laggard']=='XRP')
chk('C: XRP>=90 skip',G.decide_c(tickset(0.91,0.91,0.92,eth=0.9,doge=0.89),C-250) is None)
chk('C: other >98 skip',G.decide_c(tickset(0.99,0.5,0.92,eth=0.9),C-250) is None)
chk('C: too early (>6min) skip',G.decide_c(tickset(0.91,0.5,0.92,eth=0.9),C-400) is None)
G.HIST.clear(); G.record(tickset(0.80,0.5,0.9,eth=0.9),C-300)
chk('C: not consolidating (moved >5pt) skip',G.decide_c(tickset(0.91,0.5,0.92,eth=0.9),C-250) is None)
src=open('gapshadow.py').read()
chk('module never imports live/server',all(x not in src for x in ('import live','import server','LV.','status()','balance(')))
try:
    import spec as SP
    r1=SP.metal_gap_row(m('g',0.4,0.42),m('s',0.5,0.52),1000.0); r2=SP.crypto_gap_row({'BTC':m('b',0.4,0.42),'ETH':m('e',0.5,0.52),'DOGE':m('d',0.3,0.32)},1000.0)
    chk('metal/crypto rows carry bid/ask (+DOGE)',r1['goldYesBid']==0.4 and r1['silverYesAsk']==0.52 and r2['yesBid']['DOGE']==0.3 and r2['yesAsk']['ETH']==0.52)
except ImportError as e: print('skip spec row check',e)
chk('rules enabled',G.RULES>={'A','B','C','gap20'})
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
