"""Offline checks for the gap REGIME SHADOW (owner t1086u). No network, no real orders, live untouched."""
import os, sys, json, re, subprocess, math
os.environ['LIVE_TRADING']='0'
import urllib.request
def _no_net(*a,**k): raise AssertionError('network call attempted')
urllib.request.urlopen=_no_net
import live as L
CALLS=[]
def _no_api(*a,**k): CALLS.append(a); raise AssertionError('live.api called')
L.api=_no_api
import gapshadow as G, gapregime as R, moogle as M
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
src=open('gapregime.py').read()
chk('module has no order path / no live or network imports',not re.search(r'^\s*(import|from)\s+(live|gaplive|kws|trader_lease|urllib|requests|spec|gaplimit)\b',src,re.M) and all(x not in src for x in ('.buy(','.sell(','.api(','urlopen(','LV.','import server')) and not re.search(r'(?<!def )\bapi\(',src))
store={'paused':False,'gapLiveUsed':2,'gapLiveTest':'2','gapLiveDisabled':False}; saved=[]
lv=L.Live(lambda:dict(store),lambda st:saved.append(st)); before=json.dumps(lv.state(),sort_keys=True,default=str)
G.QUOTE[0]=None; G.LIVE[0]=None; G.SINK[0]=None; G._path[0]=None; R._path[0]=None
SUNK=[]; R.SINK[0]=lambda o:SUNK.append(o['id']); HS=[]; R.HSINK[0]=lambda d:HS.append(d)
NOW=1_800_000_000.0-1_800_000_000.0%900+300; CLOSE=NOW+600   # 5 min into a window
def rounds(coin,seq,vols=None,exps=None,end=None):
    end=end or (CLOSE-900); n=len(seq); out=[]
    for i,ch in enumerate(seq):
        c=end-(n-1-i)*900
        out.append(dict(ticker=f'{coin}-{int(c)}',result='yes' if ch=='Y' else 'no',close=c,volume_fp=str((vols or [1000]*n)[i]),expiration_value=str((exps or [100+i]*n)[i] if exps else 100.0)))
    return out
# ---- regime classification ----
R.add_rounds('BTC',rounds('BTC','YNNYNNNN',vols=[1000]*7+[500]))
R.add_rounds('ETH',rounds('ETH','NNYNNNYN'))
R.add_rounds('SOL',rounds('SOL','YYYYNYYY'))
R.add_rounds('XRP',rounds('XRP','NYNYNYNY'))
down=[];up=[]
for b in range(4):
    for i in range(900): down.append(100-b*1.0-(i%50)*0.01); up.append(100+b*1.0+(i%50)*0.01)
v,sd=R.vote_structure('BTC',NOW,vals=down); chk('CF structure: lower highs+lows over 60 min -> -1',v==-1 and sd['src']=='cf-rti-60m' and sd['lowerHL']==3)
v,sd=R.vote_structure('BTC',NOW,vals=up); chk('CF structure: higher highs+lows -> +1',v==1)
flat=[100+((i//450)%2)*0.5 for i in range(3600)]; v,_=R.vote_structure('BTC',NOW,vals=flat); chk('CF structure: chop -> 0',v==0)
a,sh,seq=R.vote_rounds('BTC',NOW); chk('rounds vote: last4 NNNN (0%) -> bearish -1',a==-1 and sh==0 and seq=='NNNN')
a,sh,_=R.vote_rounds('SOL',NOW); chk('rounds vote: 3/4 YES -> bullish +1',a==1 and sh==0.75)
a,sh,_=R.vote_rounds('XRP',NOW); chk('rounds vote: 2/4 -> 0',a==0)
chk('rounds vote stale after 2h -> None',R.vote_rounds('BTC',NOW+3*3600)[0] is None)
_cfv=R.cf_values; R.cf_values=lambda c,now:down if c in ('BTC','ETH') else (up if c=='SOL' else None)
R.refresh(NOW,force=True)
chk('coin regime BTC bearish (rounds -1 + structure -1 = -2)',R.REG['BTC']['regime']=='bearish' and R.REG['BTC']['score']==-2)
chk('coin regime SOL bullish',R.REG['SOL']['regime']=='bullish')
chk('pair regime BTC-ETH bearish',R.pair_regime('BTC-ETH')['regime']=='bearish')
chk('XRP falls back to settle values when CF missing',R.REG['XRP']['structure']['src'] in ('settle-values-75m','none'))
vc=R.REG['BTC']['volume']; chk('volume proxy: prev round 500 vs median 1000 -> low bucket',vc['bucket']=='low' and vc['ratio']==0.5)
chk('refresh throttled (30s)',R.refresh(NOW+5) is R.REG and R.STAT['refreshes']==1)
# ---- shadow books on a Rule A signal ----
def mk(tk,yb,ya): return dict(ticker=tk,close=CLOSE,yesBid=yb,yesAsk=ya,noBid=round(1-ya,2),noAsk=round(1-yb,2))
def mkts(btc=(0.79,0.81),eth=(0.59,0.61),sol=(0.70,0.72),xrp=(0.70,0.72),doge=(0.60,0.62)):
    return dict(BTC=mk('KXBTC15M-W',*btc),ETH=mk('KXETH15M-W',*eth),SOL=mk('KXSOL15M-W',*sol),XRP=mk('KXXRP15M-W',*xrp),DOGE=mk('KXDOGE15M-W',*doge))
m0=mkts(); npaper0=len(G.PAPER); G.on_tick(m0,NOW)
sig=[t for t in G.TRADES if t['rule']=='A' and t['pair']=='BTC-ETH']
chk('Rule A BTC-ETH ETH YES signal (leader BTC 0.80)',len(sig)==1 and sig[0]['side']=='YES' and sig[0]['laggard']=='ETH')
paper_before=json.dumps(G.PAPER,sort_keys=True,default=str)
R.on_tick(m0,NOW,signals=G.TRADES)
it={o['variant']:o for o in R.ITEMS if o['pair']=='BTC-ETH'}
t=it['v1']['tags']
chk('v1 tags counter-regime YES in bearish pair, leader 0.80 weak',t['counter'] and not t['strong'] and t['leaderConf']==0.8 and t['pairRegime']=='bearish')
chk('v1 enters at the signal ask 0.61',it['v1']['status']=='filled' and it['v1']['entry']==0.61 and it['v1']['side']=='YES')
chk('v2 skips weak counter entry',it['v2']['status']=='skipped' and 'counter-regime' in it['v2']['skipReason'])
chk('v3 flips to ETH NO at the NO ask 0.41',it['v3']['status']=='filled' and it['v3']['side']=='NO' and it['v3']['flipped'] and it['v3']['entry']==0.41)
chk('hour ET + volume recorded',isinstance(t['hourET'],int) and t['volume']['bucket'] in ('low','mid','high','unknown'))
chk('one set per signal (no duplicates on the next tick)',R.on_tick(m0,NOW+1,signals=G.TRADES)==[] and len([o for o in R.ITEMS if o['pair']=='BTC-ETH'])==3)
chk('persisted via SINK',all(f"{it['v1']['shadowKey']}|{v}" in SUNK for v in ('v1','v2','v3')))
# strong leader -> v2 takes it ; MoogleEdge bar
R.ITEMS.clear(); R._by.clear(); R._seen.clear()
st=dict(sig[0]); st['leaderYesMid']=0.88; st['closesAt']=CLOSE+900; st['openedAt']=NOW; st['ticker']='KXETH15M-W2'
R.on_tick({},NOW,signals=[st]); it2={o['variant']:o for o in R.ITEMS}
chk('leader 0.88 >= 0.85 bar -> v2 and v3 take the base YES',it2['v2']['status']=='filled' and it2['v2']['side']=='YES' and it2['v3']['side']=='YES' and not it2['v3']['flipped'])
st3=dict(st); st3['leaderYesMid']=0.70; st3['closesAt']=CLOSE+1800; st3['ticker']='KXETH15M-W3'
M.CUR['ETH']=dict(p=0.80,ticker='KXETH15M-W3',t=NOW-2)
R.on_tick({},NOW,signals=[st3]); it3={o['variant']:o for o in R.ITEMS if o['ticker']=='KXETH15M-W3'}
chk('MoogleEdge p 0.80 for YES >= 0.75 -> strong, v2 enters',it3['v1']['tags']['moogleP']==0.8 and it3['v2']['status']=='filled')
M.CUR['ETH']=dict(p=0.80,ticker='KXETH15M-W3',t=NOW-60); chk('stale MoogleEdge p ignored',R.moogle_side_p('ETH','KXETH15M-W3','YES',NOW) is None); M.CUR.pop('ETH',None)
# non-counter: NO in bearish -> all three take NO
st4=dict(st); st4.update(side='NO',leaderYesMid=0.30,closesAt=CLOSE+2700,ticker='KXETH15M-W4',entry=0.55)
R.on_tick({},NOW,signals=[st4]); it4={o['variant']:o for o in R.ITEMS if o['ticker']=='KXETH15M-W4'}
chk('with-regime NO: not counter, all three enter NO at 0.55',not it4['v1']['tags']['counter'] and all(it4[v]['status']=='filled' and it4[v]['side']=='NO' and it4[v]['entry']==0.55 for v in ('v1','v2','v3')))
# deep fill tag via fresh quote
R.QUOTE[0]=lambda tk:dict(yesBid=0.44,yesAsk=0.45) if tk=='KXETH15M-W5' else None
st5=dict(st); st5.update(closesAt=CLOSE+3600,ticker='KXETH15M-W5',entry=0.58,leaderYesMid=0.9)
R.on_tick({},NOW,signals=[st5]); it5={o['variant']:o for o in R.ITEMS if o['ticker']=='KXETH15M-W5'}
chk('deepFill tag when fresh ask 0.45 <= signal 0.58-0.10',it5['v1']['tags']['deepFill'] and it5['v1']['tags']['freshAsk']==0.45)
R.QUOTE[0]=None
chk('old signal (>15s) ignored',R.on_tick({},NOW+100,signals=[dict(st,closesAt=CLOSE+5000,ticker='X',openedAt=NOW)])==[] or not any(o['ticker']=='X' for o in R.ITEMS))
# trail + settle on the first signal set
R.ITEMS.clear(); R._by.clear(); R._seen.clear(); R.on_tick(m0,NOW,signals=G.TRADES)
it={o['variant']:o for o in R.ITEMS if o['pair']=='BTC-ETH'}
R.on_tick(mkts(eth=(0.80,0.82)),NOW+30,signals=[]); chk('trail arms on bid 0.80 >= 0.61+0.15',it['v1'].get('trailArmedAt') is not None and it['v1'].get('exit') is None)
R.on_tick(mkts(eth=(0.69,0.71)),NOW+60,signals=[])
chk('trail exits v1 on 10c giveback (bid 0.69 <= 0.80-0.10)',it['v1']['exit']==0.69 and it['v1']['pnl'] is not None)
chk('v3 NO side not exited by YES rally',it['v3'].get('exit') is None)
for t_ in G.TRADES:
    if t_['rule']=='A' and t_['pair']=='BTC-ETH': t_['settle']='no'
R.settle(lambda tk:None,CLOSE+10)
chk('settle: v3 NO wins, hold pnl > 0',it['v3']['settle']=='no' and it['v3']['hold_pnl']>0 and it['v3']['pnl']==it['v3']['hold_pnl'])
chk('settle: v1 keeps trail pnl, hold pnl -1',it['v1']['hold_pnl']==-1.0 and it['v1']['pnl']!=-1.0)
chk('settle: v2 skipped item records the result only',it['v2']['settle']=='no' and it['v2']['pnl'] is None)
s=R.summary(); json.dumps(R.api(),default=str)
chk('summary: books + tagged-vs-untagged + hour + volume buckets',set(s['books'])=={'v1','v2','v3'} and 'counter' in s['taggedVsUntagged'] and s['byHourET'] and s['byVolumeBucket'])
chk('summary: v2 skipped what-base-made = v1 trail pnl',s['books']['v2']['skippedWhatBaseMade']['pnl']==it['v1']['pnl'])
chk('summary: v3 flipped book counted',s['books']['v3']['flipped']['settled']==1)
chk('brief is a short string',isinstance(R.brief(),str) and 'flip' in R.brief())
# persistence roundtrip
snap=R.snapshot(); R.ITEMS.clear(); R._by.clear(); R._seen.clear(); R.merge_in(snap+[dict(snap[0],settle=None)])
chk('merge_in restores items (settled beats open, no dupes)',len(R.ITEMS)==len(snap) and R._by[snap[0]['id']]['settle']=='no')
R.HIST={c:[] for c in R.SERIES}; R.merge_hist(dict(hist={'BTC':[dict(close=CLOSE-900,ticker='x',result='no',vol=5.0,exp=1.0)]}))
chk('round history restores from persisted doc',len(R.HIST['BTC'])==1 and R.HIST['BTC'][0]['vol']==5.0)
# fetch gating (no network: FETCH stub)
FC=[]; R.FETCH[0]=lambda ser,lim:(FC.append((ser,lim)),rounds('BTC','NNNY',end=math.floor(T/900)*900) if ser=='KXBTC15M' else [])[1]
R.HIST={c:[] for c in R.SERIES}; R._st['lastTry'].clear()
T=math.floor(NOW/900)*900+5; R.poll_rounds(T); chk('no fetch within 20s of the 15m close',FC==[])
T=math.floor(NOW/900)*900+30; R.poll_rounds(T)
chk('first fetch per series uses limit 24 (boot backfill)',len(FC)==6 and all(l==24 for _,l in FC) and len(R.HIST['BTC'])==4 and HS)
FC.clear(); R.poll_rounds(T+10); chk('BTC up to date -> not refetched; others back off 60s',FC==[])
R.poll_rounds(T+400); chk('after 3 empty retries others back off 300s',len(FC)==5)
R.cf_values=_cfv
chk('gap paper book untouched by the regime shadow',json.dumps(G.PAPER,sort_keys=True,default=str)==paper_before and len(G.PAPER)>=npaper0)
chk('never called live.api / network',CALLS==[])
chk('live state unchanged (paused, used, test id)',json.dumps(lv.state(),sort_keys=True,default=str)==before and saved==[])
ss=open('server.py').read(); gl=[l.strip() for l in ss.splitlines() if 'GREG.' in l]
chk('server uses GREG only for boot/persist hooks, tick, brief and /api/gapregime',all(any(k in l for k in ('GREG.load','GREG.merge','GREG.SINK','GREG.HSINK','GREG.QUOTE','GREG.FETCH','GREG.start','GREG.MAX_KEEP','GREG.on_tick','GREG.brief','GREG.api')) for l in gl) and len(gl)>=8)
chk('FETCH hook is a public settled-markets GET (no auth)',"status=settled" in ss and "GREG.FETCH[0]=lambda ser,lim:get(f\"{KALSHI}/markets?series_ticker={ser}&status=settled" in ss)
chk('dashboard line present',"🧭 Gap regime shadow" in open('index.html').read())
try:
    df=subprocess.run(['git','diff','--name-only','HEAD','--','live.py','gaplive.py','kws.py','trader_lease.py','spec.py','moogle.py','gaplimit.py','gapshadow.py'],capture_output=True,text=True).stdout.strip()
    chk('no diff in live/Spec/MoogleEdge/limit/gap code',df=='')
except Exception as e: chk('git diff check ran: '+str(e),False)
print('ALL PASS' if ok else 'SOME FAILED'); sys.exit(0 if ok else 1)
