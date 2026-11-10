"""Offline checks for the gap Rule A LIMIT-ORDER SHADOW (owner t1062u/t1064u). No network, no real orders, live untouched."""
import os, sys, json, tempfile, subprocess, time
os.environ['LIVE_TRADING']='0'
import urllib.request
def _no_net(*a,**k): raise AssertionError('network call attempted')
urllib.request.urlopen=_no_net
import live as L
CALLS=[]
def _no_api(*a,**k): CALLS.append(a); raise AssertionError('live.api called')
L.api=_no_api
import gapshadow as G, gaplimit as M
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
src=open('gaplimit.py').read()
import re
chk('module has no order path / no live imports',not re.search(r'^\s*(import|from)\s+(live|gaplive|kws|trader_lease|urllib|requests)\b',src,re.M) and all(x not in src for x in ('.buy(','.sell(','api(','urlopen(')))
# live state snapshot (in-memory Live with a fake store) before anything runs
store={'paused':False,'gapLiveUsed':2,'gapLiveTest':'2','gapLiveDisabled':False}
saved=[]
lv=L.Live(lambda:dict(store),lambda st:saved.append(st)); before=json.dumps(lv.state(),sort_keys=True,default=str)
G.QUOTE[0]=None; G.LIVE[0]=None; G.SINK[0]=None; M.QUOTE[0]=None; M.SINK[0]=None
G._path[0]=None; M._path[0]=None
NOW=1_800_000_000.0; CLOSE=NOW+600
def mk(tk,yb,ya): return dict(ticker=tk,close=CLOSE,yesBid=yb,yesAsk=ya,noBid=round(1-ya,2),noAsk=round(1-yb,2))
def mkts(btc=(0.79,0.81),eth=(0.59,0.61),sol=(0.70,0.72),xrp=(0.70,0.72),doge=(0.60,0.62)):
    return dict(BTC=mk('KXBTC15M-W',*btc),ETH=mk('KXETH15M-W',*eth),SOL=mk('KXSOL15M-W',*sol),XRP=mk('KXXRP15M-W',*xrp),DOGE=mk('KXDOGE15M-W',*doge))
m0=mkts(); G.on_tick(m0,NOW)
sig=[t for t in G.TRADES if t['rule']=='A' and t['pair']=='BTC-ETH']
chk('Rule A BTC-ETH shadow signal fires (ETH YES laggard @ 0.61)',len(sig)==1 and sig[0]['laggard']=='ETH' and sig[0]['side']=='YES' and sig[0]['entry']==0.61)
new=M.on_tick(m0,NOW,signals=G.TRADES)
eo={o['variant']:o for o in M.ORDERS if o['pair']=='BTC-ETH'}
chk('one order per variant: mkt, lim2c, lim4c, lim6c',sorted(eo)==['lim2c','lim4c','lim6c','mkt'])
chk('limits = signal ask - N',abs(eo['lim2c']['limit']-0.59)<1e-9 and abs(eo['lim4c']['limit']-0.57)<1e-9 and abs(eo['lim6c']['limit']-0.55)<1e-9)
chk('mkt reference fills at signal ask at once',eo['mkt']['status']=='filled' and eo['mkt']['fillPrice']==0.61 and eo['mkt']['timeToFill']==0)
chk('limits rest on the signal tick',all(eo[k]['status']=='resting' for k in ('lim2c','lim4c','lim6c')))
chk('same signal never placed twice',M.on_tick(m0,NOW+1,signals=G.TRADES)==[] or all(o.get('shadowKey')!=eo['mkt']['shadowKey'] or o['status']!='resting' or True for o in M.ORDERS) and len([o for o in M.ORDERS if o['pair']=='BTC-ETH'])==4)
# ask 0.60 (> 0.59) with a bid of 0.59 / mid 0.595 -> NO fill (conservative: ask must reach the limit)
M.on_tick(mkts(eth=(0.59,0.60)),NOW+10,signals=[])
chk('no fill when only bid/mid reach the limit (ask 0.60 > 0.59)',eo['lim2c']['status']=='resting')
# ask touches 0.59 -> lim2c fills at 0.59 (not at a better price); lim4c/lim6c keep resting
M.on_tick(mkts(eth=(0.57,0.59)),NOW+20,signals=[])
chk('fill when ask <= limit, filled at the limit',eo['lim2c']['status']=='filled' and eo['lim2c']['fillPrice']==0.59 and eo['lim2c']['timeToFill']==20)
chk('deeper limits still resting',eo['lim4c']['status']=='resting' and eo['lim6c']['status']=='resting')
chk('fill contracts after taker fee ($1 stake)',abs(eo['lim2c']['contracts']-round((1-M.fee(1/0.59,0.59))/0.59,4))<1e-9)
# WS quote preferred over tick market: WS ask 0.57 -> lim4c fills
M.QUOTE[0]=lambda tk:dict(yesBid=0.56,yesAsk=0.57) if tk=='KXETH15M-W' else None
M.on_tick(mkts(eth=(0.60,0.62)),NOW+30,signals=[])
chk('fresh WS quote used for the fill check',eo['lim4c']['status']=='filled' and eo['lim4c']['fillQuote']=='ws')
M.QUOTE[0]=None
# trail after fill: lim2c entry 0.59 -> bid 0.75 arms (>=0.74), bid 0.65 (<=0.75-0.10) exits
M.on_tick(mkts(eth=(0.75,0.77)),NOW+40,signals=[])
chk('trail arms at fill+15c',eo['lim2c'].get('trailArmedAt')==NOW+40 and eo['lim2c'].get('exit') is None)
M.on_tick(mkts(eth=(0.66,0.68)),NOW+50,signals=[])
chk('no exit before 10c giveback (bid 0.66)',eo['lim2c'].get('exit') is None)
M.on_tick(mkts(eth=(0.65,0.67)),NOW+60,signals=[])
c=eo['lim2c']['contracts']; exp=round(c*0.65-M.fee(c,0.65)-1,2)
chk('trail exit at bid 0.65 with exit fee',eo['lim2c'].get('exit')==0.65 and eo['lim2c']['pnl']==exp)
# cutoff: at 180s left the still-resting lim6c expires
M.on_tick(mkts(eth=(0.70,0.72)),CLOSE-181,signals=[])
chk('still resting at 181s left',eo['lim6c']['status']=='resting')
M.on_tick(mkts(eth=(0.70,0.72)),CLOSE-180,signals=[])
chk('cutoff expiry at <=180s left',eo['lim6c']['status']=='expired' and 'cutoff' in eo['lim6c']['expireReason'])
# signal-gone expiry on a second window (SOL-XRP or a new BTC-ETH window)
NOW2=CLOSE+300; CLOSE2=NOW2+600
def mk2(tk,yb,ya): return dict(ticker=tk,close=CLOSE2,yesBid=yb,yesAsk=ya)
def mk2s(btc=(0.79,0.81),eth=(0.59,0.61)):
    return dict(BTC=mk2('KXBTC15M-W2',*btc),ETH=mk2('KXETH15M-W2',*eth),SOL=mk2('KXSOL15M-W2',0.70,0.72),XRP=mk2('KXXRP15M-W2',0.70,0.72),DOGE=mk2('KXDOGE15M-W2',0.6,0.62))
G.on_tick(mk2s(),NOW2); M.on_tick(mk2s(),NOW2,signals=G.TRADES)
e2={o['variant']:o for o in M.ORDERS if o['ticker']=='KXETH15M-W2'}
chk('second window gets its own orders',len(e2)==4)
M.on_tick(mk2s(btc=(0.44,0.46),eth=(0.62,0.64)),NOW2+10,signals=[])   # leader BTC back below 50% -> gap gone
chk('signal gone -> limits expire',all(e2[k]['status']=='expired' and e2[k]['expireReason']=='signal gone' for k in ('lim2c','lim4c','lim6c')))
# settlement: ETH window 1 settles YES via the matching shadow trade's result (no network)
G.settle(lambda tk:'yes' if tk=='KXETH15M-W' else ('no' if tk=='KXETH15M-W2' else 'yes'),CLOSE+10)
M.settle(lambda tk:(_ for _ in ()).throw(AssertionError('result_fn should not be needed')),CLOSE+10)
chk('filled mkt held to settle: hold pnl',eo['mkt']['settle']=='yes' and eo['mkt']['hold_pnl']==round(eo['mkt']['contracts']-1,2) and eo['mkt']['pnl']==eo['mkt']['hold_pnl'])
chk('trail-exited lim2c keeps booked pnl + records hold',eo['lim2c']['pnl']==exp and eo['lim2c']['hold_pnl']==round(eo['lim2c']['contracts']-1,2))
G.settle(lambda tk:'no',CLOSE2+10); M.settle(None,CLOSE2+10)
chk('window 2 settles (NO) from shadow result',e2['mkt']['settle']=='no' and e2['mkt']['pnl']==-1.0)
S=M.summary(); bv=S['byVariant']
chk('summary: lim2c fill rate 1 filled of 2 signals',bv['lim2c']['signals']==2 and bv['lim2c']['filled']==1 and bv['lim2c']['fillRate']==0.5)
chk('summary: unfilled count + what market entry made on them',bv['lim6c']['unfilledSettled']==2 and abs(bv['lim6c']['unfilledMktPnl']-round(eo['mkt']['pnl']+e2['mkt']['pnl'],2))<0.011)
chk('summary: vsMkt = limit pnl - mkt pnl on same settled signals',abs(bv['lim2c']['vsMkt']-round(bv['lim2c']['pnl']-bv['lim2c']['mktPnlSameSignals'],2))<0.011)
chk('summary: time to fill + expiry reasons',bv['lim2c']['timeToFillSec']['avg']==20 and bv['lim6c']['expiredByReason'].get('signal gone')==1)
chk('summary: twin gap paper linked',bv['mkt']['twinPaper']['n']>=1)
chk('brief line renders',isinstance(M.brief(),str) and 'lim2c' in M.brief())
# boot: stale (reloaded) signal is not re-placed; persistence round-trip merges by id
snap=M.snapshot(); M.ORDERS.clear(); M._seen.clear()
d=tempfile.mkdtemp(); json.dump(snap,open(os.path.join(d,'gap_limit.json'),'w'))
M.load(d); chk('file reload restores all orders',len(M.ORDERS)==len(snap))
old=dict(sig[0],openedAt=NOW2-100,closesAt=CLOSE2+900,ticker='KXETH15M-OLD'); old['closesAt']=NOW2+500
n0=len(M.ORDERS); M.on_tick({},NOW2+200,signals=[old]); chk('old reloaded signal not placed after restart',len(M.ORDERS)==n0)
# gap paper book + shadow book untouched by the limit shadow
chk('gap paper trades unchanged by limit shadow (no limit fields leaked)',all('limit' not in t and 'variant' not in t for t in G.PAPER))
# never real orders + live untouched
chk('never called live.api / network',CALLS==[])
chk('live state unchanged (paused, used, test id)',json.dumps(lv.state(),sort_keys=True,default=str)==before and saved==[])
try:
    df=subprocess.run(['git','diff','--name-only','HEAD','--','live.py','gaplive.py','kws.py','trader_lease.py'],capture_output=True,text=True).stdout.strip()
    chk('no diff in live code (live.py/gaplive.py/kws.py/trader_lease.py)',df=='')
    sd=subprocess.run(['git','diff','-U0','HEAD','--','server.py'],capture_output=True,text=True).stdout
    added=[l for l in sd.splitlines() if l.startswith(('+','-')) and not l.startswith(('+++','---'))]
    chk('server.py diff only adds lines (no live lines changed/removed)',all(l.startswith('+') for l in added) and not any(x in l for l in added for x in ('LV.','GAPLIVE','set_paused','gapLiveUsed')))
except Exception as e: chk('git diff check ran: '+str(e),False)
print('ALL PASS' if ok else 'SOME FAILED'); sys.exit(0 if ok else 1)
