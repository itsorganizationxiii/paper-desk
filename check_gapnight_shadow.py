"""Offline checks for the gap NIGHT-MODE SHADOW (owner t1093u/t1095u). No network, no real orders, live untouched."""
import os, sys, json, re, subprocess, math
os.environ['LIVE_TRADING']='0'
import urllib.request
def _no_net(*a,**k): raise AssertionError('network call attempted')
urllib.request.urlopen=_no_net
import live as L
CALLS=[]
def _no_api(*a,**k): CALLS.append(a); raise AssertionError('live.api called')
L.api=_no_api
import gapshadow as G, gapregime as R, gapnight as N, moogle as M
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
src=open('gapnight.py').read()
chk('module has no order path / no live or network imports',not re.search(r'^\s*(import|from)\s+(live|gaplive|kws|trader_lease|urllib|requests|spec|gaplimit)\b',src,re.M) and all(x not in src for x in ('.buy(','.sell(','.api(','urlopen(','LV.','import server')) and not re.search(r'(?<!def )\bapi\(',src))
store={'paused':False,'gapLiveUsed':5,'gapLiveTest':'2','gapLiveDisabled':True}; saved=[]
lv=L.Live(lambda:dict(store),lambda st:saved.append(st)); before=json.dumps(lv.state(),sort_keys=True,default=str)
G.QUOTE[0]=None; G.LIVE[0]=None; G.SINK[0]=None; G._path[0]=None
R._path[0]=None; N._path[0]=None
SUNK=[]; N.SINK[0]=lambda o:SUNK.append(o['id'])
# 3:05 AM ET on 2027-01-15 = night; 9:05 AM ET same day = day
NIGHT=1_800_000_000.0-1_800_000_000.0%900+300
DAY=NIGHT+6*3600
CLOSE_N=NIGHT+600; CLOSE_D=DAY+600
def rounds(coin,seq,vols=None,end=None):
    end=end or (CLOSE_N-900); n=len(seq); out=[]
    for i,ch in enumerate(seq):
        c=end-(n-1-i)*900
        out.append(dict(ticker=f'{coin}-{int(c)}',result='yes' if ch=='Y' else 'no',close=c,volume_fp=str((vols or [1000]*n)[i]),expiration_value='100'))
    return out
R.add_rounds('BTC',rounds('BTC','YNNYNNNN',vols=[1000]*7+[500]))
R.add_rounds('ETH',rounds('ETH','NNYNNNYN',vols=[1000]*7+[500]))
R.add_rounds('SOL',rounds('SOL','YYYYNYYY',vols=[800]*8))
R.add_rounds('XRP',rounds('XRP','NYNYNYNY',vols=[800]*8))
chk('night clock is 5 PM-6 AM ET (3 AM = night, 9 AM = day, 5 PM = night, 6 AM = day)',
    N.is_night(NIGHT) is True and N.hour_et(NIGHT)==3 and N.is_night(DAY) is False and N.hour_et(DAY)==9
    and N.is_night(NIGHT+(17-3)*3600) is True and N.hour_et(NIGHT+(17-3)*3600)==17
    and N.is_night(NIGHT+(6-3)*3600) is False and N.hour_et(NIGHT+(6-3)*3600)==6)
vc=N.volume_ctx('ETH'); chk('volume reused from gapregime: ETH prev 500 vs median 1000 -> low / ratio 0.5',vc.get('ratio')==0.5 and vc.get('bucket')=='low' and N._low_vol(vc))
vcS=N.volume_ctx('SOL'); chk('SOL volume at median is not low',not N._low_vol(vcS) and vcS.get('ratio')==1.0)
def mk(tk,yb,ya): return dict(ticker=tk,close=CLOSE_N,yesBid=yb,yesAsk=ya,noBid=round(1-ya,2),noAsk=round(1-yb,2))
def mkts(btc=(0.79,0.81),eth=(0.59,0.61),sol=(0.70,0.72),xrp=(0.70,0.72),doge=(0.60,0.62),close=CLOSE_N):
    out=dict(BTC=mk('KXBTC15M-W',*btc),ETH=mk('KXETH15M-W',*eth),SOL=mk('KXSOL15M-W',*sol),XRP=mk('KXXRP15M-W',*xrp),DOGE=mk('KXDOGE15M-W',*doge))
    for v in out.values(): v['close']=close
    return out
# ---- night Rule A BTC-ETH, MoogleEdge warm above bar, ETH volume low ----
m0=mkts(); npaper0=len(G.PAPER); G.on_tick(m0,NIGHT)
sig=[t for t in G.TRADES if t['rule']=='A' and t['pair']=='BTC-ETH']
chk('Rule A BTC-ETH ETH YES signal (leader BTC 0.80)',len(sig)==1 and sig[0]['side']=='YES' and sig[0]['laggard']=='ETH')
paper_before=json.dumps(G.PAPER,sort_keys=True,default=str)
M.CUR['ETH']=dict(p=0.70,ticker=sig[0]['ticker'],t=NIGHT-2)
N.on_tick(m0,NIGHT,signals=G.TRADES)
it={o['variant']:o for o in N.ITEMS if o['pair']=='BTC-ETH' and o['session']=='night'}
chk('session tagged night at 3 AM ET',it['baseline']['session']=='night' and it['baseline']['tags']['hourET']==3)
chk('baseline enters unfiltered at the signal ask 0.61',it['baseline']['status']=='filled' and it['baseline']['entry']==0.61 and it['baseline']['side']=='YES')
chk('n1 enters when MoogleEdge side p 0.70 >= 0.65',it['n1']['status']=='filled' and it['n1']['tags']['moogleP']==0.70)
chk('n2 skips low volume even when MoogleEdge passes',it['n2']['status']=='skipped' and 'low-volume' in it['n2']['skipReason'])
chk('one set per signal (no duplicates on the next tick)',N.on_tick(m0,NIGHT+1,signals=G.TRADES)==[] and len([o for o in N.ITEMS if o['pair']=='BTC-ETH'])==3)
chk('persisted via SINK',all(f"{it['baseline']['shadowKey']}|{v}" in SUNK for v in ('baseline','n1','n2')))
# MoogleEdge below bar
N.ITEMS.clear(); N._by.clear(); N._seen.clear(); SUNK.clear()
st=dict(sig[0]); st['closesAt']=CLOSE_N+900; st['openedAt']=NIGHT; st['ticker']='KXETH15M-W2'
M.CUR['ETH']=dict(p=0.50,ticker='KXETH15M-W2',t=NIGHT-1)
N.on_tick({},NIGHT,signals=[st]); itb={o['variant']:o for o in N.ITEMS}
chk('n1/n2 skip when MoogleEdge p 0.50 < 0.65; baseline still enters',itb['baseline']['status']=='filled' and itb['n1']['status']=='skipped' and 'moogle-below-bar' in itb['n1']['skipReason'] and itb['n2']['status']=='skipped' and 'moogle-below-bar' in itb['n2']['skipReason'])
# MoogleEdge not warm
stc=dict(st); stc['closesAt']=CLOSE_N+1800; stc['ticker']='KXETH15M-W3'
M.CUR['ETH']=dict(p=0.80,ticker='KXETH15M-W3',t=NIGHT-60)
N.on_tick({},NIGHT,signals=[stc]); itc={o['variant']:o for o in N.ITEMS if o['ticker']=='KXETH15M-W3'}
chk('stale MoogleEdge is not warm -> n1/n2 skip moogle-not-warm, baseline enters',N.moogle_side_p('ETH','KXETH15M-W3','YES',NIGHT) is None and itc['baseline']['status']=='filled' and itc['n1']['skipReason']=='moogle-not-warm' and itc['n2']['skipReason']=='moogle-not-warm')
M.CUR.pop('ETH',None)
stc2=dict(st); stc2['closesAt']=CLOSE_N+2700; stc2['ticker']='KXETH15M-W3b'
N.on_tick({},NIGHT,signals=[stc2]); itc2={o['variant']:o for o in N.ITEMS if o['ticker']=='KXETH15M-W3b'}
chk('missing MoogleEdge cache -> moogle-not-warm',itc2['n1']['skipReason']=='moogle-not-warm' and itc2['n1']['tags']['moogleWarm'] is False)
# n2 enters when volume is not below the median AND MoogleEdge passes (SOL at median)
sts=dict(rule='A',mode='shadow',pair='SOL-XRP',laggard='SOL',leader='XRP',ticker='KXSOL15M-W4',side='YES',entry=0.55,
         closesAt=CLOSE_N+3600,openedAt=NIGHT,gapDown=0.2,leaderYesMid=0.80,laggardYesMid=0.55)
M.CUR['SOL']=dict(p=0.80,ticker='KXSOL15M-W4',t=NIGHT-1)
N.on_tick({},NIGHT,signals=[sts]); its={o['variant']:o for o in N.ITEMS if o['ticker']=='KXSOL15M-W4'}
chk('SOL-XRP SOL laggard: n1 and n2 both enter (vol at median, moogle 0.80)',its['baseline']['status']=='filled' and its['n1']['status']=='filled' and its['n2']['status']=='filled' and its['n2']['entry']==0.55)
# XRP laggard blocked
stx=dict(sts); stx.update(laggard='XRP',leader='SOL',ticker='KXXRP15M-W5',closesAt=CLOSE_N+4500,entry=0.40)
M.CUR['XRP']=dict(p=0.90,ticker='KXXRP15M-W5',t=NIGHT-1)
N.on_tick({},NIGHT,signals=[stx]); itx={o['variant']:o for o in N.ITEMS if o['ticker']=='KXXRP15M-W5'}
chk('SOL-XRP XRP laggard skipped on every variant',all(itx[v]['status']=='skipped' and itx[v]['skipReason']=='xrp-laggard' for v in N.VARIANTS))
# GOLD-SILVER ignored
n_before=len(N.ITEMS)
stg=dict(rule='A',mode='shadow',pair='GOLD-SILVER',laggard='SILVER',leader='GOLD',ticker='KXSILVER15M-W',side='YES',entry=0.55,closesAt=CLOSE_N+5400,openedAt=NIGHT)
N.on_tick({},NIGHT,signals=[stg])
chk('GOLD-SILVER Rule A is out of scope (no items)',len(N.ITEMS)==n_before)
# day round logged as not-applicable, no entries
std=dict(st); std.update(closesAt=CLOSE_D,openedAt=DAY,ticker='KXETH15M-DAY')
M.CUR['ETH']=dict(p=0.80,ticker='KXETH15M-DAY',t=DAY-1)
N.on_tick({},DAY,signals=[std]); itd={o['variant']:o for o in N.ITEMS if o['ticker']=='KXETH15M-DAY'}
chk('day 9 AM ET logged as not-applicable on all variants (no entries)',all(itd[v]['status']=='skipped' and itd[v]['skipReason']=='day-not-applicable' and itd[v]['session']=='day' for v in N.VARIANTS))
# old signal ignored
chk('old signal (>15s) ignored',N.on_tick({},NIGHT+100,signals=[dict(st,closesAt=CLOSE_N+9000,ticker='OLD',openedAt=NIGHT)])==[] or not any(o['ticker']=='OLD' for o in N.ITEMS))
# trail + settle on the first BTC-ETH night set
N.ITEMS.clear(); N._by.clear(); N._seen.clear(); M.CUR['ETH']=dict(p=0.70,ticker=sig[0]['ticker'],t=NIGHT-1)
N.on_tick(m0,NIGHT,signals=G.TRADES)
it={o['variant']:o for o in N.ITEMS if o['pair']=='BTC-ETH'}
N.on_tick(mkts(eth=(0.80,0.82)),NIGHT+30,signals=[]); chk('trail arms on bid 0.80 >= 0.61+0.15',it['baseline'].get('trailArmedAt') is not None and it['baseline'].get('exit') is None)
N.on_tick(mkts(eth=(0.69,0.71)),NIGHT+60,signals=[])
chk('trail exits baseline/n1 on 10c giveback (bid 0.69 <= 0.80-0.10)',it['baseline']['exit']==0.69 and it['n1']['exit']==0.69 and it['baseline']['pnl'] is not None)
chk('n2 skipped item has no trail exit',it['n2'].get('exit') is None and it['n2']['status']=='skipped')
for t_ in G.TRADES:
    if t_['rule']=='A' and t_['pair']=='BTC-ETH': t_['settle']='no'
N.settle(lambda tk:None,CLOSE_N+10)
chk('settle: trail books keep trail pnl and record hold_pnl -1 (YES lost)',it['baseline']['settle']=='no' and it['baseline']['hold_pnl']==-1.0 and it['baseline']['pnl']!=-1.0)
chk('settle: skipped n2 records the result only',it['n2']['settle']=='no' and it['n2']['pnl'] is None)
# hold-to-settle when trail never arms: reuse SOL fill
for o in N.ITEMS:
    if o['ticker']=='KXSOL15M-W4' and o['status']=='filled':
        o['closesAt']=CLOSE_N  # already in ITEMS from earlier; settle window
# reset SOL items into a clean hold path
N.ITEMS[:]=[o for o in N.ITEMS if o['ticker']!='KXSOL15M-HOLD']
sth=dict(sts); sth.update(ticker='KXSOL15M-HOLD',closesAt=CLOSE_N,openedAt=NIGHT)
M.CUR['SOL']=dict(p=0.80,ticker='KXSOL15M-HOLD',t=NIGHT-1)
# already-seen would block if we reuse shadowKey; new ticker / closesAt already CLOSE_N used? use unique closesAt then force
sth['closesAt']=CLOSE_N+30
N.on_tick({},NIGHT,signals=[sth])
ith={o['variant']:o for o in N.ITEMS if o['ticker']=='KXSOL15M-HOLD'}
for o in ith.values(): o['closesAt']=CLOSE_N
N.settle(lambda tk:'yes',CLOSE_N+10)
chk('hold-to-settle: no trail -> pnl equals hold_pnl and win on YES',ith['baseline']['heldToSettle'] and ith['baseline']['pnl']==ith['baseline']['hold_pnl'] and ith['baseline']['hold_pnl']>0 and ith['n2']['pnl']==ith['n2']['hold_pnl'])
# re-log a day signal so the summary still has a day bucket after the trail-test reset
std2=dict(st); std2.update(closesAt=CLOSE_D+900,openedAt=DAY,ticker='KXETH15M-DAY2')
M.CUR['ETH']=dict(p=0.80,ticker='KXETH15M-DAY2',t=DAY-1)
N.on_tick({},DAY,signals=[std2])
s=N.summary(); json.dumps(N.api(),default=str)
chk('summary: three books + skip reasons + persist TTL note',set(s['books'])=={'baseline','n1','n2'} and s['books']['n2']['skipReasons'] and s['persist']['collection']=='gap_night' and s['persist']['ttlDays']==120)
chk('summary W-L counts night entries only (day logged separately)',s['dayLogged']>=3 and 'day-not-applicable' in s['daySkipReasons'] and all(o['status']!='filled' or o.get('session')=='night' for o in N.ITEMS))
chk('brief is a short string with Night mode variant names',isinstance(N.brief(),str) and 'unfiltered' in N.brief() and 'moogle-65' in N.brief())
sess=N.session_now(NIGHT); chk('session_now reports night at 3 AM ET',sess['isNight'] is True and sess['label']=='night' and sess['window']=='5 PM-6 AM ET')
# persistence roundtrip
snap=N.snapshot(); n0=len(snap); N.ITEMS.clear(); N._by.clear(); N._seen.clear(); N.merge_in(snap+[dict(snap[0],settle=None)])
chk('merge_in restores items (settled beats open, no dupes)',len(N.ITEMS)==n0 and N._by[snap[0]['id']]['settle']==snap[0].get('settle'))
# isolation
chk('gap paper book untouched by the night shadow',json.dumps(G.PAPER,sort_keys=True,default=str)==paper_before and len(G.PAPER)>=npaper0)
chk('never called live.api / network',CALLS==[])
chk('live state unchanged (paused, used, test id)',json.dumps(lv.state(),sort_keys=True,default=str)==before and saved==[])
# server + dashboard wiring
ss=open('server.py').read(); gl=[l.strip() for l in ss.splitlines() if 'GNIGHT.' in l]
chk('server uses GNIGHT only for boot/persist hooks, tick, brief and /api/gapnight',all(any(k in l for k in ('GNIGHT.load','GNIGHT.merge','GNIGHT.SINK','GNIGHT.QUOTE','GNIGHT.MAX_KEEP','GNIGHT.on_tick','GNIGHT.brief','GNIGHT.api','GNIGHT.settle')) for l in gl) and len(gl)>=6)
chk('persist hook upserts lease-gated into gap_night',"GNIGHT.SINK[0]=lambda o:GP.upsert('gap_night',o['id'],o)" in ss)
chk('dashboard line present',"🌙 Night mode shadow" in open('index.html').read())
gp=open('gappersist.py').read()
chk('gappersist stamps openedAtDt and TTL index on gap_night',"col in ('gap_trades','gap_night')" in gp or "col in ('gap_night','gap_trades')" in gp)
chk("gappersist ensure_indexes creates gap_night openedAtDt TTL","db['gap_night'].create_index('openedAtDt'" in gp)
try:
    df=subprocess.run(['git','diff','--name-only','HEAD','--','live.py','gaplive.py','kws.py','trader_lease.py','spec.py','moogle.py','gaplimit.py','gapshadow.py','gapregime.py'],capture_output=True,text=True).stdout.strip()
    chk('no diff in live/Spec/MoogleEdge/limit/gap/regime code',df=='')
except Exception as e: chk('git diff check ran: '+str(e),False)
print('ALL PASS' if ok else 'SOME FAILED'); sys.exit(0 if ok else 1)
