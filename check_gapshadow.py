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

# ---- Nov 9: gap PAPER promotion (Rule A BTC-ETH / SOL-XRP only), separate book, never live ----
G=importlib.reload(G); G.load(tempfile.mkdtemp()); G.HIST.clear()
chk('paper defaults: rule A on BTC-ETH,SOL-XRP only',G.PAPER_RULES=={'A'} and G.PAPER_PAIRS=={'BTC-ETH','SOL-XRP'} and G.PAPER_STAKE==1.0)
sunk=[]; G.SINK[0]=lambda t:sunk.append(dict(t)); G.QUOTE[0]=None
C2=3700.0
mk_be={'BTC':mm('KXBTC-P',0.46,C2),'ETH':mm('KXETH-P',0.58,C2),'SOL':mm('KXSOL-P',0.6,C2),'XRP':mm('KXXRP-P',0.55,C2),'DOGE':mm('KXDOGE-P',0.3,C2)}
o=G.on_tick(mk_be,C2-400)
pa=G.paper_snapshot()
chk('A BTC-ETH shadow trade still logged',any(t['rule']=='A' and t['pair']=='BTC-ETH' for t in G.TRADES))
be=[t for t in pa if t['pair']=='BTC-ETH']
chk('A BTC-ETH opens one gap PAPER trade',len(be)==1)
p0=be[0] if be else {}
chk('paper trade labeled mode=paper gap signalSource=gap rule/pair, $1',p0.get('mode')=='paper' and p0.get('gap') is True and p0.get('shadow') is False and p0.get('signalSource')=='gap' and p0.get('rule')=='A' and p0.get('pair')=='BTC-ETH' and p0.get('stake')==1.0)
chk('paper trade has asset/price for ledger',p0.get('asset')=='BTC' and p0.get('price')==p0.get('entry') and p0.get('id','').startswith('GP'))
chk('sink called for shadow and paper',any(t.get('gap') for t in sunk) and any(t.get('mode')=='shadow' for t in sunk))
G.on_tick(mk_be,C2-390); chk('one paper trade per rule/pair/window',len([t for t in G.PAPER if t['pair']=='BTC-ETH'])==1)
# gold-silver A, gap20, B, C stay shadow-only
G2c=C2+900
o=G.on_tick({'GOLD':mm('g',0.30,G2c),'SILVER':mm('s',0.38,G2c)},G2c-400)
chk('A GOLD-SILVER stays shadow-only',any(t['pair']=='GOLD-SILVER' for t in o) and not any(t['pair']=='GOLD-SILVER' for t in G.PAPER))
chk('gap20/B/C never paper',not any(t['rule']!='A' for t in G.PAPER))
# SOL-XRP paper
C3=C2+1800
o=G.on_tick({'SOL':mm('KXSOL-Q',0.62,C3),'XRP':mm('KXXRP-Q',0.52,C3),'BTC':mm('KXBTC-Q',0.6,C3),'ETH':mm('KXETH-Q',0.6,C3),'DOGE':mm('KXDOGE-Q',0.4,C3)},C3-400)
chk('A SOL-XRP opens gap PAPER (XRP laggard)',any(t['pair']=='SOL-XRP' and t['asset']=='XRP' for t in G.PAPER))
# fresh quote check: ran >1c away -> no fill; within -> fill at fresh price
st=dict(rule='A',pair='BTC-ETH',closesAt=9999.0,side='YES',entry=0.47,ticker='KXBTC-Z',laggard='BTC',openedAt=1.0)
G.QUOTE[0]=lambda tk:dict(yesBid=0.49,yesAsk=0.50)
chk('fresh ask ran >1c -> no paper fill',G.paper_open(st) is None and G.PSTAT['nofill']>=1)
G.QUOTE[0]=lambda tk:dict(yesBid=0.46,yesAsk=0.475)
pt2=G.paper_open(st); chk('fresh ask within 1c -> fills at fresh price',pt2 is not None and pt2['price']==0.475 and pt2['signalAsk']==0.47)
G.QUOTE[0]=lambda tk:(_ for _ in ()).throw(RuntimeError('ws down'))
st2=dict(st,closesAt=9998.0); chk('quote error -> falls back to signal ask',G.paper_open(st2)['price']==0.47)
G.QUOTE[0]=None
# settlement of paper trades
G.settle(lambda tk:'yes',C3+900)
ps=[t for t in G.PAPER if t['closesAt']<=C3]
chk('paper trades settle via Kalshi result',ps and all(t['status'] in ('won','lost') and t['settledBy']=='kalshi' and t['pnl']==t['hold_pnl'] for t in ps))
sm=G.paper_summary(); chk('paper summary by rule/pair',sm['all']['trades']==len(G.PAPER) and 'A BTC-ETH' in sm['byRulePair'])
# merge_in: reload from Mongo/seed keeps history; settled beats open; dedup by key; ids reassigned in time order
snapS=G.snapshot(); snapP=G.paper_snapshot()
G=importlib.reload(G); G.load(tempfile.mkdtemp())
stale=[dict(t,status='open',pnl=None,hold_pnl=None,closedAt=None) for t in snapS[:2]]
G.merge_in(stale); G.merge_in(snapS+snapP); G.merge_in(snapS)
chk('merge_in restores shadow+paper books',len(G.TRADES)==len(snapS) and len(G.PAPER)==len(snapP))
chk('settled record beats stale open',all(G.TRADES[i]['status']==snapS[i]['status'] for i in range(2)))
chk('dedup keys rebuilt (no reopen same window)',(snapS[0]['rule'],snapS[0]['pair'],snapS[0]['closesAt']) in G._seen and len(G._pseen)==len(snapP))
_pp=[t for t in G.PAPER if t['closesAt']==C2][0]
chk('doc_id distinguishes books',G.doc_id(_pp)!=G.doc_id(next(t for t in G.TRADES if t['pair']==_pp['pair'] and t['closesAt']==_pp['closesAt'] and t['rule']=='A')))
chk('merge_in ignores junk',G.merge_in([None,{},{'pair':'X'},'x'])==[])
src=open('gapshadow.py').read()
chk('gap module has no order path',all(x not in src for x in ('buy(','sell(','openLive','_gbuy','rest_buy','api(')))
ssrc=open('server.py').read()
chk('server never puts gap trades into pt.trades',all(x not in ssrc for x in ('pt.trades.append(g','pt.trades+GAPS','pt.merge(GAPS')))
chk('openLive rejects gap metadata',"meta.get('gap') or str(meta.get('signalSource','')).startswith('gap')" in ssrc)
chk('csv labels gap paper + shadow rule:pair',"'gap:'+str(g.get('rule'))+':'+str(g.get('pair',''))" in ssrc and "'gapshadow:'+str(g.get('rule','gap20'))+':'+str(g.get('pair',''))" in ssrc)
# ---- Nov 9: confidence flag for gap PAPER (stake $1 flat on strong and normal); shadow stays $1 ----
G=importlib.reload(G); G.load(tempfile.mkdtemp()); G.HIST.clear(); G.QUOTE[0]=None; G.SINK[0]=None
chk('strong gate defaults',G.STRONG_STAKE==1.0 and G.PAPER_STAKE==1.0 and abs(G.STRONG_GAP-0.08)<1e-12 and G.STRONG_MIN_OF==3 and (G.STRONG_MIN_LEFT,G.STRONG_MAX_LEFT)==(120.0,780.0))
C4=9000.0
def strongset(c,doge=0.7,sol=0.6,xrp=0.55,btc=0.46,eth=0.58):
    d={'BTC':mm('KXBTC-S%d'%c,btc,c),'ETH':mm('KXETH-S%d'%c,eth,c),'SOL':mm('KXSOL-S%d'%c,sol,c),'XRP':mm('KXXRP-S%d'%c,xrp,c)}
    if doge is not None: d['DOGE']=mm('KXDOGE-S%d'%c,doge,c)
    return d
G.on_tick(strongset(C4),C4-400)
sp=[t for t in G.PAPER if t['closesAt']==C4][0]; sh=[t for t in G.TRADES if t['closesAt']==C4 and t['rule']=='A'][0]
chk('unanimous 3/3 + 12pt gap + 400s -> strong flag, $1 stake',sp['conf']=='strong' and sp['stake']==1.0 and sp['tag']=='gap-paper-strong')
chk('strong contracts sized from $1',abs(sp['contracts']-round((1.0-G.fee(1.0/sp['price'],sp['price']))/sp['price'],4))<1e-9)
chk('shadow twin stays $1',sh['stake']==1.0 and sh['mode']=='shadow')
C5=C4+900; G.on_tick(strongset(C5,doge=0.3),C5-400)
chk('2/3 agree -> normal $1',[t for t in G.PAPER if t['closesAt']==C5][0]['conf']=='normal' and [t for t in G.PAPER if t['closesAt']==C5][0]['stake']==1.0)
C6=C5+900; G.on_tick(strongset(C6,doge=None),C6-400)
chk('no DOGE (of=2) -> normal',[t for t in G.PAPER if t['closesAt']==C6][0]['conf']=='normal')
C7=C6+900; G.on_tick(strongset(C7,btc=0.53,eth=0.58),C7-400)
chk('gap 5pt <8 -> normal',[t for t in G.PAPER if t['closesAt']==C7][0]['conf']=='normal')
C8=C7+900; G.on_tick(strongset(C8),C8-100)
chk('100s left (<120) -> normal',[t for t in G.PAPER if t['closesAt']==C8][0]['conf']=='normal')
chk('confidence() pure edges',G.confidence(dict(confirm=dict(agree=3,of=3),gapDown=-0.08,closesAt=1000,openedAt=880))=='strong' and G.confidence(dict(confirm=None,gapDown=0.5,closesAt=1000,openedAt=500))=='normal' and G.confidence({})=='normal')
G.settle(lambda tk:'yes',C8+900)
sm=G.paper_summary(); bt=sm['byTier']
chk('summary per tier count/W/L/P&L',bt['strong']['trades']==1 and bt['normal']['trades']==len(G.PAPER)-1 and bt['strong']['wins']+bt['strong']['losses']==1 and 'pnl' in bt['normal'] and sm['strongStake']==1.0 and sm['strongGate']['minGap']==G.STRONG_GAP)
chk('strong closed trade was staked $1',abs(bt['strong']['staked']-1.0)<1e-9)
import os as _os; _os.environ['GAP_PAPER_STRONG_STAKE']='2'; _os.environ['GAP_STRONG_GAP']='0.20'
G=importlib.reload(G); G.load(tempfile.mkdtemp()); G.HIST.clear(); G.QUOTE[0]=None; G.SINK[0]=None
chk('env gap still tunable; leftover STRONG_STAKE env ignored',G.STRONG_STAKE==1.0 and G.PAPER_STAKE==1.0 and abs(G.STRONG_GAP-0.20)<1e-12)
Cenv=30000.0
G.on_tick(strongset(Cenv,btc=0.40,eth=0.70),Cenv-400)
pe=[t for t in G.PAPER if t['closesAt']==Cenv][0]
chk('leftover GAP_PAPER_STRONG_STAKE=2 still sizes strong paper at $1',pe['conf']=='strong' and pe['stake']==1.0 and pe['tag']=='gap-paper-strong')
del _os.environ['GAP_PAPER_STRONG_STAKE']; del _os.environ['GAP_STRONG_GAP']; G=importlib.reload(G)
chk('csv tags strong tier',"g.get('tag') or 'gap-paper'" in open('server.py').read() and ":conf='+str(g.get('conf') or 'normal')" in open('server.py').read())
# ---- Nov 9: SHADOW exit variants (trail15/10, tp@75c, time360s) logged per trade; trades unchanged ----
G=importlib.reload(G); G.load(tempfile.mkdtemp()); G.HIST.clear(); G.QUOTE[0]=None; G.SINK[0]=None
chk('default variants',set(G.VARIANTS)=={'trail15/10','tp@75c','time360s','hold'})
chk('variant env parser',set(G._parse_variants('trail:0.2:0.05,tpat:0.8,tpplus:0.3,time:300,bad,tpat:x'))=={'trail20/5','tp@80c','tp+30c','time300s'})
C9=20000.0
def vset(c,btc,eth=0.62,sol=0.7,xrp=0.65,doge=0.7):
    return {'BTC':mm('KXBTC-V',btc,c),'ETH':mm('KXETH-V',eth,c),'SOL':mm('KXSOL-V',sol,c),'XRP':mm('KXXRP-V',xrp,c),'DOGE':mm('KXDOGE-V',doge,c)}
G.on_tick(vset(C9,0.50),C9-700)
ta=[t for t in G.TRADES if t['rule']=='A' and t['pair']=='BTC-ETH'][0]; tp=[t for t in G.PAPER if t['pair']=='BTC-ETH'][0]
e0=ta['entry']; st0=(ta['status'],ta['pnl'],tp['status'],tp['pnl'],tp['stake'])
G.on_tick(vset(C9,0.70),C9-600)   # bid ~0.69: trail armed (>=entry+15), tp@75 not yet
chk('trail armed not exited yet',ta['exitVariants']['trail15/10']['status']=='open' and ta['exitVariants']['trail15/10']['peak']>=e0+0.15)
G.on_tick(vset(C9,0.80),C9-500)   # bid 0.79 -> tp@75 exits
v=ta['exitVariants']['tp@75c']; chk('tp@75 exits at bid w/ fee, leftSec logged',v['status']=='exited' and v['exit']==0.79 and v['leftSec']==500 and abs(v['pnl']-(ta['contracts']*0.79-G.fee(ta['contracts'],0.79)-ta['stake']))<1e-3)
G.on_tick(vset(C9,0.68),C9-450)   # peak 0.79 -> bid 0.67 drop 12c -> trail exits
chk('trail exits on 10c giveback',ta['exitVariants']['trail15/10']['status']=='exited' and ta['exitVariants']['trail15/10']['exit']==0.67)
G.on_tick(vset(C9,0.66),C9-300)   # <=360 left & in profit -> time exits
chk('time360 exits when in profit late',ta['exitVariants']['time360s']['status']=='exited' and ta['exitVariants']['time360s']['leftSec']==300)
chk('paper trade tracked too',tp['exitVariants']['tp@75c']['status']=='exited')
chk('shadow trade unchanged (still open)',(ta['status'],ta['pnl'])==st0[:2])
chk('PAPER trade booked trail exit at bid minus fee, stake unchanged',tp['status']=='exited' and tp['stake']==st0[4] and abs(tp['pnl']-round(tp['contracts']*tp['exit']-G.fee(tp['contracts'],tp['exit'])-tp['stake'],2))<1e-9 and tp['exit']<=tp['peakBid']-0.10+1e-9 and tp['exitVariants']['trail15/10']['exit']==tp['exit'])
chk('hold variant never exits',tp['exitVariants']['hold']['status']=='open')
G.settle(lambda tk:'no',C9+10)
chk('settled as loss; variants keep their exits',ta['status']=='lost' and ta['exitVariants']['tp@75c']['pnl']>0)
chk('exited paper trade keeps booked pnl, records hold at settle',tp['status']=='exited' and tp['settle']=='no' and tp['hold_pnl']==-tp['stake'] and tp['pnl']>tp['hold_pnl'] and tp['exitVariants']['hold']['pnl']==tp['hold_pnl'])
_ps=G.paper_summary(); chk('gappaper reports exit mode + trail exits',_ps['exitMode'].startswith('trail:+15c') and _ps['all']['trailExits']>=1 and _ps['all']['pnlVsHold']>0)
vs=G.variant_summary('shadow')['summary']
chk('variant summary vs hold',vs['tp@75c']['n']>=1 and vs['tp@75c']['vsHold']>0 and 'BTC-ETH' in vs['tp@75c']['byPair'] and 'maxDD' in vs['trail15/10'])
chk('gappaper summary includes exitVariants',G.paper_summary()['exitVariants']['summary']['time360s']['n']>=1)
# a trade that never triggers is marked held at hold pnl
C10=C9+900; G.on_tick(vset(C10,0.50),C10-700); G.on_tick(vset(C10,0.45),C10-300)
tb=[t for t in G.TRADES if t['rule']=='A' and t['closesAt']==C10 and t['pair']=='BTC-ETH'][0]; G.settle(lambda tk:'no',C10+10)
chk('untriggered variants -> held at hold pnl',all(v['status']=='held' and v['pnl']==tb['hold_pnl'] for v in tb['exitVariants'].values()))
tq=[t for t in G.PAPER if t['closesAt']==C10]; chk('untriggered paper trade holds to settlement',tq and all(t['status'] in ('won','lost') and t['pnl']==t['hold_pnl'] for t in tq))
sn=G.snapshot(); sn[0].setdefault('exitVariants',{})['x']={}
chk('snapshot deep-copies variants',('x' not in (G.TRADES[0].get('exitVariants') or {})))
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
