"""Offline checks for the MoogleEdge SHADOW (owner t1077u). Fake feed only: no network, no real orders, live untouched.
Math parity vs the offline model (/workspace/moogleedge-20261109): feature-level samples + real CF 1s slices replayed
through the live Series/WS/REST code paths (fixture moogle_fixture.json.gz, offline reference p computed with numpy)."""
import os, sys, json, gzip, re, subprocess, math, time
os.environ['LIVE_TRADING']='0'; os.environ['MOOGLE']='1'
import urllib.request
def _no_net(*a,**k): raise AssertionError('network call attempted')
urllib.request.urlopen=_no_net
import live as L
CALLS=[]
def _no_api(*a,**k): CALLS.append(a); raise AssertionError('live.api called')
L.api=_no_api
L.keys_present=lambda:True
_sigK=[0]
L.sign=lambda key,ts,m,p:(_sigK.__setitem__(0,_sigK[0]+1) or 'SIG'); L.private_key=lambda pem=None:None; L._key_id=lambda:'KEYID'
import moogle as M
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
src=open('moogle.py').read()
chk('module has no order/portfolio/balance path',not re.search(r'/portfolio|/orders|balance\(|\.buy\(|\.sell\(|live\.api\(|\bstatus\(\)|POST|DELETE',src)
    and not re.search(r'^\s*(import|from)\s+(gaplive|kws|trader_lease|gaplimit|gapshadow|spec)\b',src,re.M))
chk('only read path is GET /trade-api/v2/cfbenchmarks/values (+ WS cfbenchmarks_value)',set(re.findall(r"'/trade-api/v2/[a-z/]+'",src))=={"'/trade-api/v2/cfbenchmarks/values'"} and "'cfbenchmarks_value'" in src)
chk('no key/header logging',not re.search(r"print\([^)]*(hd|header|_signed_headers|KALSHI-ACCESS)",src))
store={'paused':True,'gapLiveUsed':2,'gapLiveTest':'2','gapLiveDisabled':False}; saved=[]
lv=L.Live(lambda:dict(store),lambda st:saved.append(st)); before=json.dumps(lv.state(),sort_keys=True,default=str)
FX=json.load(gzip.open('moogle_fixture.json.gz','rt'))
# ---- 1. feature-level parity (offline sample rows: features -> p)
md=0
for r in FX['samples']:
    p,z,pb=M.prob(r['S'],r['K'],r['tau'],r['m_obs'],r['aobs'],{60:r['v60'],300:r['v300'],900:r['v900'],3600:r['v3600']},(r['mom60'],r['mom300'],r['mom900']))
    md=max(md,abs(p-r['p']),abs(pb-r['p_base']))
chk(f'prob() == offline p on {len(FX["samples"])} sample rows (incl. tau<60) max|diff|={md:.2e}',md<1e-6)
# ---- 2. Series replay (zero EWMA init = offline lfilter init) -> exact parity at tau 840..10, incl. missing seconds
mx=0; n=0
for w in FX['windows']:
    s=M.Series(w['coin'],init='zero'); want={w['close']-e['tau']:e for e in w['expected']}
    for i,v in enumerate(w['values']):
        sec=w['start']+i
        if v is not None:
            if s.cur is None: s._replay({sec:v})
            else: s.add(sec,v)
        elif s.cur is not None: s._step(None); s.cur+=1; s.start=s.cur-len(s.ff)+1
        e=want.get(sec+1)
        if e:
            s.backfilled=True; r=s.p_at(w['strike'],w['close'])
            d=abs(r['p']-e['p']) if r else 9; mx=max(mx,d); n+=1
chk(f'Series replay == offline p on {n} decision points in {len(FX["windows"])} real windows (1 with 40 missing seconds) max|diff|={mx:.2e}',n==24 and mx<1e-5)
# ---- 3. live path: fake REST backfill (last hour) + fake WS frames, mean-init warm-up; vs full-history offline p
calls=[]
def fake_http(url,hd):
    calls.append((url,dict(hd)))
    w=fake_http.w; a=fake_http.upto-3600
    pl=[dict(value=str(v),time=(w['start']+i)*1000) for i,v in enumerate(w['values']) if v is not None and a<=w['start']+i<fake_http.upto]
    return 200,json.dumps(dict(data=dict(serverTime='x',payload=pl))).encode(),{}
M.HTTP[0]=fake_http; worst=0; cnt=0
for w in FX['windows']:
    coin=w['coin']; M.SER[coin]=M.Series(coin); fake_http.w=w; fake_http.upto=w['close']-900-60
    chk(f'{coin}: REST backfill ok (signed GET cfbenchmarks/values?id={M.INDEX[coin]})',M.backfill(coin,now=fake_http.upto) and calls[-1][0].endswith('/trade-api/v2/cfbenchmarks/values?id='+M.INDEX[coin]) and calls[-1][1].get('KALSHI-ACCESS-SIGNATURE')=='SIG')
    M.WIN[coin]=dict(ticker=w['ticker'],strike=w['strike'],close=w['close'])
    want={w['close']-e['tau']:e for e in w['expected']}
    for i,v in enumerate(w['values']):
        sec=w['start']+i
        if sec<fake_http.upto or v is None: continue
        M.on_ws_message(json.dumps(dict(type='cfbenchmarks_value',sid=1,seq=i,msg=dict(index_id=M.INDEX[coin],received_at=sec*1000+30,data=json.dumps(dict(type='value',time=sec*1000,id=M.INDEX[coin],value=str(v)))))),now=sec+0.4)
        e=want.get(sec+1)
        if e and e.get('pFullHistory') is not None:
            r=M.current_p(coin,now=sec+0.6); worst=max(worst,abs(r['p']-e['pFullHistory'])); cnt+=1
chk(f'live path (1h REST backfill + WS) vs full-history offline p on {cnt} points: max|diff|={worst:.3f} (<0.03 warm-up approx)',cnt>=12 and worst<0.03)
fs=M.feed_status(now=time.time())
chk('feed status exposes per-coin lag/warm/backfilled + rest counters',all(k in fs['coins']['BTC'] for k in ('lagSec','warm','backfilled','missingLast900')) and fs['rest']['ok']>=4)
# ---- 4. 429 backoff
def h429(url,hd): calls.append(('429',url)); return 429,b'{}',{'Retry-After':'45'}
M.HTTP[0]=h429; t=1_900_000_000.0; nb=len(calls)
r1=M.rest_values('BTC',now=t); r2=M.rest_values('BTC',now=t+10)
chk('429 -> None, Retry-After backoff respected (no second call inside backoff)',r1 is None and r2 is None and len(calls)==nb+1 and M.FEED['rest']['backoffUntil']>=t+45 and M.FEED['rest']['http429']==1)
M.HTTP[0]=_no_net
# ---- 5. shadow book: gates, decisions incl. skips, settlement P&L, Brier
M.ROWS.clear(); M._seen_order.clear(); M.WIN.clear(); sunk=[]; M.SINK[0]=lambda r:sunk.append(r)
NOW=1_800_000_000.0; CLOSE=int(NOW+600)
def kk(c,ya,na): return dict(ticker=f'KX{c}15M-T',strike=100.0,close=CLOSE,yesAsk=ya,noAsk=na,yesBid=ya-0.01,noBid=na-0.01)
P={'BTC':0.62,'ETH':0.35,'SOL':0.20,'XRP':None}
M.current_p=lambda coin,now=None:(None if P.get(coin) is None else dict(p=P[coin],pBase=P[coin],z=0,tau=CLOSE-int(now),t=int(now),lagSec=0.5,K=100.0,S=100.0))
sigs={'BTC':dict(kalshi=kk('BTC',0.55,0.47),direction='YES',action='skip: eloedge-unavailable',momentum=1,cvd=2,modelP=None),
      'ETH':dict(kalshi=kk('ETH',0.50,0.52),direction='YES',action='skip: eloedge-unavailable',momentum=1,cvd=1),
      'SOL':dict(kalshi=kk('SOL',0.40,0.62),direction='YES',action='skip: eloedge-unavailable',momentum=1,cvd=1),
      'XRP':dict(kalshi=kk('XRP',0.50,0.52),direction='disagree',action='skip: momentum/CVD disagree'),
      'GOLD':dict(kalshi=kk('GOLD',0.5,0.5),direction='YES',action='OPENED #1')}
M.on_spec(sigs,now=NOW)
R={c:M.ROWS.get(f'KX{c}15M-T') for c in ('BTC','ETH','SOL','XRP','GOLD')}
chk('BTC p=.62 agrees with YES: none/full/strong all enter',sorted(R['BTC']['entries'])==['full','none','strong'])
chk('ETH p=.35 vs YES: full vetoes, strong allows (|p-.5|<.20)',sorted(R['ETH']['entries'])==['none','strong'])
chk('SOL p=.20 vs YES: full + strong veto, only ungated enters',sorted(R['SOL']['entries'])==['none'])
chk('skip actions recorded next to p (decision log incl. skips)',R['BTC']['decisions'][0]['action']=='skip: eloedge-unavailable' and R['BTC']['decisions'][0]['p']==0.62 and R['BTC']['first']['mom']==1)
chk('no p and no signal -> nothing recorded (XRP); metals ignored (GOLD)',R['XRP'] is None and R['GOLD'] is None)
P['ETH']=0.55; M.on_spec(sigs,now=NOW+10)
chk('full gate enters later when MoogleEdge comes to agree; entries never duplicated',sorted(R['ETH']['entries'])==['full','none','strong'] and R['ETH']['entries']['full']['at']==NOW+10 and R['ETH']['entries']['none']['at']==NOW)
P['BTC']=0.10; M.on_spec(sigs,now=NOW+20)
chk('later disagreement never removes an entry',len(R['BTC']['entries'])==3)
sigs2=dict(sigs); sigs2['BTC']=dict(sigs['BTC'],action='OPENED #77')
M.on_spec(sigs2,now=NOW+30)
chk('Spec real open noted (specOpened) for comparison',R['BTC']['specOpened'] and R['BTC']['specOpened']['action']=='OPENED #77')
res={'KXBTC15M-T':'yes','KXETH15M-T':'no','KXSOL15M-T':'no'}
for _ in range(3): M.settle(lambda tk:res.get(tk),now=CLOSE+60+_*31)
e=R['BTC']['entries']['none']; c0=1/0.55; f=math.ceil(7*c0*0.55*0.45)/100
chk('settlement P&L: $1 incl. taker fee, win = contracts-1, loss = -1',abs(R['BTC']['pnl']['none']-round((1-f)/0.55-1,4))<1e-9 and R['ETH']['pnl']['full']==-1 and R['SOL']['pnl']['none']==-1)
S=M.summary()
chk('summary n/W-L/P&L per gate',S['variants']['none']['n']==3 and S['variants']['none']['wins']==1 and S['variants']['full']['n']==2 and S['variants']['strong']['n']==2
    and S['variants']['full']['vetoed']['n']==1 and S['variants']['strong']['vetoed']['n']==1)
chk('Brier vs outcomes at first signal',S['brier']['firstSignal']['n']==3 and abs(S['brier']['firstSignal']['brier']-round(((0.62-1)**2+(0.35-0)**2+(0.20-0)**2)/3,4))<1e-9)
chk('rows persisted through SINK (gappersist upsert hook)',any(r['id']=='KXBTC15M-T' and r.get('settle')=='yes' for r in sunk))
a=M.api(); json.dumps(a,default=str)
chk('/api/moogleedge payload: status, feed lag, current p, trades, summary',all(k in a for k in ('status','current','trades','summary')) and a['shadowOnly'] is True)
# ---- 6. no trading side effects
chk('never called live.api / urlopen',CALLS==[])
chk('live state unchanged (paused, used, test id)',json.dumps(lv.state(),sort_keys=True,default=str)==before and saved==[])
ss=open('server.py').read(); mo=[l.strip() for l in ss.splitlines() if 'MOOG.' in l]
chk('server uses MOOG only for boot/persist hook, tick shadow call, status brief and /api/moogleedge',
    all(any(x in l for x in ('MOOG.load','MOOG.merge_in','MOOG.SINK','MOOG.start','MOOG.on_spec(sigs); MOOG.settle(_pl_result)','MOOG.brief()','MOOG.api(','MOOG.MAX_KEEP')) for l in mo) and len(mo)==7)
try:
    df=subprocess.run(['git','diff','--name-only','HEAD','--','live.py','gaplive.py','kws.py','trader_lease.py','spec.py','gapshadow.py','gaplimit.py'],capture_output=True,text=True).stdout.strip()
    chk('no diff in live/spec/gap code',df=='')
except Exception as e: chk('git diff check ran: '+str(e),False)
print('ALL PASS' if ok else 'SOME FAILED'); sys.exit(0 if ok else 1)
