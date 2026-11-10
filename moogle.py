"""MoogleEdge SHADOW (owner t1077u, Nov 9 2026). PAPER/SHADOW ONLY: never places, sizes or blocks a trade; never touches
pt.trades, Spec, gap, gap live, the limit shadow, stakes, the live cap/used counter or the paused/live toggle.
Model (offline build /workspace/moogleedge-20261109, params frozen in moogle_params.json, no online training):
  P(YES) = P(avg of the CF RTI 1s values over the final 60s >= floor_strike).
  baseline  tau>=60: z = ln(S/K) / (s*sqrt(tau-40));  tau<60: z = -((60K - m*Aobs)/(tau*S) - 1) / (s*sqrt(tau/3))
            s^2 = (w*EWMA60 + (1-w)*EWMA900)*scale^2 of 1s squared log returns (forward-filled seconds)
  calib     logistic on [z, z*ln(v300/v3600), z*ln(tau/300), z|z|/8, mom60/300/900 z-scores]
Feed (READ ONLY, Kalshi key signed with live.sign; no order, portfolio or balance endpoints anywhere in this module):
  live ticks : Kalshi WS channel 'cfbenchmarks_value' (1 Hz per index, ~1.5 KB/s for 4 coins, no REST rate tokens)
  backfill   : GET /trade-api/v2/cfbenchmarks/values?id=<INDEX> (returns the last hour of 1s values, ~150 KB) once per
               coin at startup, then only to repair gaps / WS outages (>= MOOGLE_REPAIR_MIN_SEC apart per coin;
               every MOOGLE_REST_FALLBACK_SEC per coin while the WS is down). 429 -> Retry-After / exponential backoff.
Shadow book: per crypto window, at every Spec decision tick (including skips) we note moogle_p next to Spec's action and
its mom/CVD side; three hypothetical $1 hold-to-settle entries at the first eligible tick (>=60s left, p available):
  none   = Spec's mom/CVD side, ungated;  full = only if MoogleEdge agrees (p>=.5 <=> YES);
  strong = vetoed only if MoogleEdge disagrees with |p-0.5| >= 0.20.
$1 includes the taker entry fee (contracts=(1-fee)/ask), held to settlement. Brier of p vs outcome is reported."""
import collections, json, math, os, threading, time, urllib.request, urllib.error
E=os.environ.get
ENABLED=E('MOOGLE','1')!='0'
APP=os.path.dirname(os.path.abspath(__file__))
PARAMS=json.load(open(os.path.join(APP,'moogle_params.json')))
INDEX={'BTC':'BRTI','ETH':'ETHUSD_RTI','SOL':'SOLUSD_RTI','XRP':'XRPUSD_RTI'}
COIN_OF={v:k for k,v in INDEX.items()}
HLS=(60,300,900,3600)
_bv=PARAMS['baseline_vol']; FAST,SLOW,WFAST,SCALE=int(_bv['fast_hl']),int(_bv['slow_hl']),float(_bv['w_fast']),float(_bv['scale'])
B0=float(PARAMS['intercept']); COEF=[float(x) for x in PARAMS['coef']]
STRONG=float(E('MOOGLE_STRONG','0.20'))
BUF_SEC=7200; WARM_MIN=1000; MAX_MISS_900=30
EVAL_SEC=float(E('MOOGLE_EVAL_SEC','5'))
REPAIR_MIN_SEC=float(E('MOOGLE_REPAIR_MIN_SEC','300'))
FALLBACK_SEC=float(E('MOOGLE_REST_FALLBACK_SEC','30'))
STALE_SEC=15.0
CHECKPOINTS=(840,600,300,120,60,30)
MAX_KEEP=int(E('MOOGLE_MAX_KEEP','3000'))
MAX_DEC=40; STAKE=1.0
VARIANTS=('none','full','strong')
_lk=threading.RLock()
def fee(c,p): return math.ceil(7*c*p*(1-p))/100
def _ncdf(z): return 0.5*(1+math.erf(z/math.sqrt(2)))
# ---------------------------------------------------------------- model (pure) ----------------------------------------
def prob(S,K,tau,m_obs,aobs,v,mom):
    """v: {60,300,900,3600: EWMA var}; mom: (ln S/S60, ln S/S300, ln S/S900). Returns (p, z, p_base). Mirrors moogleedge.py."""
    tau=float(tau); var=(WFAST*v[FAST]+(1-WFAST)*v[SLOW])*SCALE**2
    s=math.sqrt(max(var,1e-14))
    if tau>=60: z=math.log(S/K)/(s*math.sqrt(max(tau-40,1)))
    else:
        need=(60*K-m_obs*(aobs if aobs is not None else 0.0))/(max(tau,1)*S)-1
        z=-need/(s*math.sqrt(max(tau,1)/3))
    z=max(-8.0,min(8.0,z))
    s3=math.sqrt(max(v[300],1e-14)); lvr=math.log(max(v[300],1e-14)/max(v[3600],1e-14)); lt=math.log(tau/300.0)
    cl=lambda x:max(-6.0,min(6.0,x))
    X=[z,z*lvr,z*lt,z*abs(z)/8,cl(mom[0]/(s3*math.sqrt(60))),cl(mom[1]/(s3*math.sqrt(300))),cl(mom[2]/(s3*math.sqrt(900)))]
    u=B0+sum(c*x for c,x in zip(COEF,X)); p=1/(1+math.exp(-u)) if u>=0 else math.exp(u)/(1+math.exp(u))
    return p,z,_ncdf(z)
class Series:
    """1s CF RTI series for one coin: raw values per second, forward-filled, incremental EWMA of squared 1s log returns."""
    def __init__(s,coin,init='mean'):
        s.coin=coin; s.init=init; s.reset()
    def reset(s):
        s.raw=collections.deque(maxlen=BUF_SEC)   # raw value or None per processed second (oldest..cur)
        s.ff=collections.deque(maxlen=BUF_SEC); s.start=None; s.cur=None; s.v={h:0.0 for h in HLS}; s.n=0
        s.pend={}; s.newest=None; s.lastAddAt=None; s.backfilled=False
    def _step(s,val):
        prev=s.ff[-1] if s.ff else None
        f=val if val is not None else prev
        if f is None: return False
        r2=(math.log(f/prev))**2 if prev else 0.0
        for h in HLS: a=1-0.5**(1/h); s.v[h]=a*r2+(1-a)*s.v[h]
        s.raw.append(val); s.ff.append(f); s.n+=1; return True
    def _replay(s,pts):
        """rebuild from {sec: val}; EWMA init = mean r2 of the first hour (init='mean', live) or 0 (init='zero', offline parity)."""
        if not pts: return
        ks=sorted(pts); a,b=max(ks[0],ks[-1]-BUF_SEC+1),ks[-1]
        seq=[pts.get(t) for t in range(a,b+1)]
        s.raw.clear(); s.ff.clear(); s.n=0; s.start=a; s.cur=a-1
        if s.init=='mean':
            r2=[]; prev=None
            for x in seq[:3601]:
                if x is None: continue
                if prev: r2.append(math.log(x/prev)**2)
                prev=x
            m=sum(r2)/len(r2) if r2 else 0.0; s.v={h:m for h in HLS}
        else: s.v={h:0.0 for h in HLS}
        for x in seq:
            if not s.ff and x is None: s.start+=1; s.cur+=1; continue
            s._step(x); s.cur+=1
        s.start=s.cur-len(s.ff)+1
    def add(s,sec,val,now=None):
        """live tick (in order). Out-of-order/old seconds are ignored (repairs go through merge())."""
        if val is None or val<=0: return
        s.lastAddAt=now or time.time()
        if s.cur is None: s._replay({sec:val}); s.newest=sec; return
        if sec<=s.cur: return
        s.newest=max(s.newest or sec,sec)
        if sec-s.cur>BUF_SEC: s._replay({sec:val}); return
        while s.cur<sec-1: s._step(None); s.cur+=1
        s._step(val); s.cur=sec; s.start=s.cur-len(s.ff)+1
    def merge(s,pts):
        """REST backfill/repair: union with what we have, full replay (rare)."""
        cur={}
        if s.cur is not None:
            for i,x in enumerate(s.raw):
                if x is not None: cur[s.start+i]=x
        cur.update({int(k):float(v) for k,v in pts.items() if v and float(v)>0})
        s._replay(cur); s.newest=s.cur; s.backfilled=True
    def at(s,sec):
        if s.cur is None or sec<s.start or sec>s.cur: return None
        return s.ff[sec-s.start]
    def missing(s,a,b):
        n=0
        for t in range(max(a,s.start),min(b,s.cur)+1):
            if s.raw[t-s.start] is None: n+=1
        return n+max(0,s.start-a)
    def warm(s):
        return s.cur is not None and len(s.ff)>=WARM_MIN and (s.backfilled or s.n>=3600) and s.missing(s.cur-899,s.cur)<=MAX_MISS_900
    def p_at(s,K,close,t=None):
        """MoogleEdge p for a window (strike K, close epoch s) at decision second t (data < t, like the offline build).
        t defaults to cur+1 (newest data). Returns dict or None."""
        if s.cur is None or not K: return None
        t=s.cur+1 if t is None else int(t)
        i=t-1
        if i>s.cur or not s.warm(): return None
        tau=int(close)-t
        if tau<=0: return None
        S=s.at(i); S60,S300,S900=s.at(i-60),s.at(i-300),s.at(i-900)
        if None in (S,S60,S300,S900): return None
        m=max(0,min(60,t-(int(close)-60))); aobs=None
        if m>0:
            vals=[s.at(x) for x in range(int(close)-60,int(close)-60+m)]
            if None in vals: return None
            aobs=sum(vals)/m
        p,z,pb=prob(S,float(K),tau,m,aobs,s.v,(math.log(S/S60),math.log(S/S300),math.log(S/S900)))
        return dict(p=round(p,5),z=round(z,4),pBase=round(pb,5),tau=tau,t=t,S=S,K=float(K),mObs=m)
# ---------------------------------------------------------------- feed ------------------------------------------------
SER={c:Series(c) for c in INDEX}
FEED=dict(ws=dict(connected=False,msgs=0,bytes=0,reconnects=0,error=None,lastMsgAt=None,startedAt=None,rxLagMs=None),
          rest=dict(calls=0,ok=0,http429=0,errors=0,bytes=0,lastError=None,backoffUntil=0,lastCallAt=None,byCoin={}),
          started=False)
_lastRepair={c:0.0 for c in INDEX}
def _signed_headers(path):
    import live
    ts=str(int(time.time()*1000))
    return {'KALSHI-ACCESS-KEY':live._key_id(),'KALSHI-ACCESS-TIMESTAMP':ts,'KALSHI-ACCESS-SIGNATURE':live.sign(live.private_key(),ts,'GET',path)}
HTTP=[None]   # test hook: fn(url,headers)->(status,body_bytes,headers)
def _get(url,headers):
    if HTTP[0] is not None: return HTTP[0](url,headers)
    req=urllib.request.Request(url,headers=dict(headers,Accept='application/json',**{'User-Agent':'paper-desk-moogle/1.0'}))
    try:
        with urllib.request.urlopen(req,timeout=15) as r: return r.status,r.read(),dict(r.headers)
    except urllib.error.HTTPError as e:
        try: b=e.read()
        except Exception: b=b''
        return e.code,b,dict(e.headers or {})
def rest_values(coin,now=None):
    """READ ONLY: GET /trade-api/v2/cfbenchmarks/values?id=<INDEX> -> {sec: value} (last hour, 1s). Backs off on 429."""
    R=FEED['rest']; now=time.time() if now is None else now
    if now<R['backoffUntil']: return None
    import live
    path='/trade-api/v2/cfbenchmarks/values'
    base=live.BASE.rsplit('/trade-api',1)[0]
    R['calls']+=1; R['lastCallAt']=now; bc=R['byCoin'].setdefault(coin,dict(calls=0,ok=0,lastOkAt=None))
    bc['calls']+=1
    try: st,body,hd=_get(base+path+'?id='+INDEX[coin],_signed_headers(path))
    except Exception as e: R['errors']+=1; R['lastError']=f'{coin}: {type(e).__name__}'; return None
    R['bytes']+=len(body or b'')
    if st==429:
        R['http429']+=1
        try: ra=float({k.lower():v for k,v in (hd or {}).items()}.get('retry-after') or 0)
        except Exception: ra=0
        R['_bo']=min(600.0,max(30.0,ra,2*R.get('_bo',15.0))); R['backoffUntil']=now+R['_bo']; R['lastError']=f'429 backoff {R["_bo"]:.0f}s'; return None
    if st!=200: R['errors']+=1; R['lastError']=f'{coin}: HTTP {st}'; return None
    R['_bo']=15.0
    try:
        pl=(json.loads(body).get('data') or {}).get('payload') or []
        out={}
        for x in pl: out[int(x['time'])//1000]=float(x['value'])
    except Exception as e: R['errors']+=1; R['lastError']=f'{coin}: parse {type(e).__name__}'; return None
    R['ok']+=1; bc['ok']+=1; bc['lastOkAt']=now; return out
def backfill(coin,now=None):
    pts=rest_values(coin,now)
    if pts:
        with _lk: SER[coin].merge(pts)
        _lastRepair[coin]=time.time() if now is None else now; return True
    _lastRepair[coin]=time.time() if now is None else now
    return False
def on_ws_message(raw,now=None):
    """parse one WS frame; returns coin updated or None."""
    now=time.time() if now is None else now; W=FEED['ws']
    W['msgs']+=1; W['bytes']+=len(raw or ''); W['lastMsgAt']=now
    try: d=json.loads(raw)
    except Exception: return None
    if d.get('type')=='error': W['error']=str(d.get('msg'))[:160]; return None
    if d.get('type')!='cfbenchmarks_value': return None
    m=d.get('msg') or {}; coin=COIN_OF.get(m.get('index_id'))
    if not coin: return None
    try:
        x=json.loads(m['data']) if isinstance(m.get('data'),str) else (m.get('data') or {})
        ms=int(x['time']); val=float(x['value'])
    except Exception: return None
    W['rxLagMs']=int(now*1000)-ms
    with _lk: SER[coin].add(ms//1000,val,now)
    return coin
def _ws_loop():
    W=FEED['ws']; back=2
    try: import websocket, live
    except Exception as e: W['error']=f'websocket-client missing: {e}'; return
    url=live.BASE.replace('https://','wss://').replace('http://','ws://').rsplit('/trade-api',1)[0]+'/trade-api/ws/v2'
    while True:
        ws=None
        try:
            ws=websocket.create_connection(url,header=_signed_headers('/trade-api/ws/v2'),timeout=20)
            ws.send(json.dumps(dict(id=1,cmd='subscribe',params=dict(channels=['cfbenchmarks_value'],index_ids=list(INDEX.values())))))
            W['connected']=True; W['error']=None; back=2
            while True:
                try: raw=ws.recv()
                except websocket.WebSocketTimeoutException: ws.ping(); continue
                if raw is None or raw=='': raise ConnectionError('closed')
                on_ws_message(raw)
        except Exception as e: W['error']=f'{type(e).__name__}: {str(e)[:120]}'
        W['connected']=False; W['reconnects']+=1
        try: ws and ws.close()
        except Exception: pass
        time.sleep(back); back=min(back*2,60)
# ---------------------------------------------------------------- windows / current p ---------------------------------
WIN={}            # coin -> dict(ticker,strike,close)
CUR={}            # coin -> latest p dict (eval thread)
ROWS={}           # ticker -> shadow row
_dirty=set(); _seen_order=collections.deque()
SINK=[None]       # fn(row) non-blocking persistence (server: GP.upsert)
FILE=[None]
STAT=dict(evals=0,decisions=0,entries=0,settled=0,errors=0,repairs=0)
def _row(coin,tk,strike,close,now):
    r=ROWS.get(tk)
    if r is None:
        r=dict(id=tk,coin=coin,ticker=tk,strike=strike,closesAt=close,createdAt=now,mode='shadow',book='moogleedge',
               first=None,entries={},decisions=[],checkpoints={},specActionLast=None,specOpened=None,settle=None,y=None,pnl={},settledAt=None)
        ROWS[tk]=r; _seen_order.append(tk)
        while len(_seen_order)>MAX_KEEP: ROWS.pop(_seen_order.popleft(),None)
    return r
def set_window(coin,k):
    if coin in INDEX and isinstance(k,dict) and k.get('ticker') and k.get('strike') and k.get('close'):
        WIN[coin]=dict(ticker=k['ticker'],strike=float(k['strike']),close=int(round(float(k['close']))))
def current_p(coin,now=None):
    w=WIN.get(coin)
    if not w: return None
    now=time.time() if now is None else now
    with _lk:
        s=SER[coin]
        r=s.p_at(w['strike'],w['close'])   # t = newest second + 1 (data < t, like the offline build)
    if r: r.update(ticker=w['ticker'],lagSec=round(max(0.0,now-r['t']),1),at=now)
    return r
def evaluate(now=None):
    """every EVAL_SEC: current p per coin + checkpoint p (tau crossing 840/600/300/120/60/30) per window."""
    now=time.time() if now is None else now
    with _lk:
      for coin in INDEX:
        try:
            r=current_p(coin,now); STAT['evals']+=1
            if r is None: CUR.pop(coin,None); continue
            CUR[coin]=r
            if r['lagSec']>STALE_SEC: continue
            w=WIN[coin]
            for cp in CHECKPOINTS:
                if r['tau']<=cp and r['tau']>cp-max(EVAL_SEC*3,20):
                    row=_row(coin,w['ticker'],w['strike'],w['close'],now)
                    if str(cp) not in row['checkpoints']:
                        row['checkpoints'][str(cp)]=dict(p=r['p'],tau=r['tau'],at=round(now,1)); _dirty.add(row['id'])
        except Exception as e: STAT['errors']+=1; print('moogle eval err',type(e).__name__,e,flush=True)
def _feed_upkeep(now=None):
    now=time.time() if now is None else now
    wsok=FEED['ws']['connected']
    for coin in INDEX:
        s=SER[coin]
        stale=s.lastAddAt is None or now-s.lastAddAt>STALE_SEC
        if not s.backfilled and now-_lastRepair[coin]>=60: need=True
        elif not wsok: need=now-_lastRepair[coin]>=FALLBACK_SEC
        elif stale or (s.cur is not None and s.missing(s.cur-119,s.cur)>10): need=now-_lastRepair[coin]>=REPAIR_MIN_SEC
        else: need=False
        if need:
            STAT['repairs']+=1; backfill(coin,now); time.sleep(1.5)
def _loop():
    for coin in INDEX:
        try: backfill(coin)
        except Exception as e: STAT['errors']+=1; print('moogle backfill err',type(e).__name__,flush=True)
        time.sleep(1.5)
    while True:
        try: _feed_upkeep(); evaluate()
        except Exception as e: STAT['errors']+=1; print('moogle loop err',type(e).__name__,e,flush=True)
        time.sleep(EVAL_SEC)
def start():
    if not ENABLED or FEED['started']: return
    try:
        import live
        if not live.keys_present(): FEED['ws']['error']='no Kalshi keys'; return
    except Exception as e: FEED['ws']['error']=f'keys: {type(e).__name__}'; return
    FEED['started']=True; FEED['ws']['startedAt']=time.time()
    threading.Thread(target=_ws_loop,daemon=True,name='moogle-ws').start()
    threading.Thread(target=_loop,daemon=True,name='moogle-eval').start()
# ---------------------------------------------------------------- Spec decision hook ----------------------------------
def _allow(var,side,p):
    if var=='none': return True
    agree=(p>=0.5)==(side=='YES')
    if var=='full': return agree
    return agree or abs(p-0.5)<STRONG
def on_spec(sigs,now=None,elo=None):
    """Called once per tick (after Spec decided) with the tick's per-asset signal dicts. Pure shadow bookkeeping."""
    if not ENABLED: return
    now=time.time() if now is None else now
    with _lk: _on_spec(sigs,now)
    _flush()
def _on_spec(sigs,now):
    for coin in INDEX:
        try:
            sg=(sigs or {}).get(coin) or {}; k=sg.get('kalshi')
            if not isinstance(k,dict) or not k.get('ticker'): continue
            set_window(coin,k)
            w=WIN.get(coin)
            if not w or w['ticker']!=k['ticker']: continue
            side=sg.get('direction') if sg.get('direction') in ('YES','NO') else None
            action=str(sg.get('action') or '')[:120]
            r=current_p(coin,now); p=r['p'] if (r and r['lagSec']<=STALE_SEC) else None
            left=round(w['close']-now)
            if side is None and p is None and not action.startswith('OPENED'): continue
            row=_row(coin,w['ticker'],w['strike'],w['close'],now); STAT['decisions']+=1
            ask=k.get('yesAsk') if side=='YES' else (k.get('noAsk') if side=='NO' else None)
            last=row['decisions'][-1] if row['decisions'] else None
            if last is None or last['side']!=side or last['action']!=action or now-last['at']>=60:
                row['decisions'].append(dict(at=round(now,1),left=left,side=side,action=action,p=p,ask=ask,eloP=sg.get('modelP')))
                if len(row['decisions'])>MAX_DEC: row['decisions']=row['decisions'][:5]+row['decisions'][-(MAX_DEC-5):]
                _dirty.add(row['id'])
            if row['specActionLast']!=action: row['specActionLast']=action; _dirty.add(row['id'])
            if action.startswith('OPENED') and not row['specOpened']:
                row['specOpened']=dict(at=round(now,1),left=left,side=side,ask=ask,p=p,action=action); _dirty.add(row['id'])
            if side is None or p is None or left<60 or not ask or not (0.01<=float(ask)<=0.99): continue
            if row['first'] is None:
                row['first']=dict(at=round(now,1),left=left,side=side,ask=ask,p=p,pBase=r.get('pBase'),eloP=sg.get('modelP'),
                                  mom=sg.get('momentum'),cvd=sg.get('cvd'),specAction=action)
                _dirty.add(row['id'])
            for var in VARIANTS:
                if var in row['entries'] or not _allow(var,side,p): continue
                ask=float(ask); c0=STAKE/ask; ef=fee(c0,ask)
                row['entries'][var]=dict(at=round(now,1),left=left,side=side,ask=ask,p=p,contracts=round(max(STAKE-ef,0.001)/ask,4),fee=ef,specAction=action)
                STAT['entries']+=1; _dirty.add(row['id'])
        except Exception as e: STAT['errors']+=1; print('moogle on_spec err',type(e).__name__,e,flush=True)
_try={}
def settle(result_fn,now=None):
    now=time.time() if now is None else now; n=0
    with _lk: todo=[r for r in list(ROWS.values()) if r.get('settle') is None and now>=r['closesAt']+5]
    for r in todo:
        if n>=3: break
        tk=r['ticker']
        if now-_try.get(tk,0)<30 or result_fn is None: continue
        _try[tk]=now; n+=1
        try: res=result_fn(tk)
        except Exception: res=None
        if res not in ('yes','no'):
            if now-r['closesAt']>6*3600: r['settle']='unknown'; _dirty.add(r['id'])
            continue
        with _lk:
            y=1 if res=='yes' else 0; r.update(settle=res,y=y,settledAt=round(now,1))
            for var,e in r['entries'].items():
                win=(y==1)==(e['side']=='YES'); r['pnl'][var]=round((e['contracts'] if win else 0)-STAKE,4)
            STAT['settled']+=1; _dirty.add(r['id'])
    _flush(save_file=n>0)
def _flush(save_file=False):
    with _lk:
        ids=list(_dirty); _dirty.clear()
        docs=[json.loads(json.dumps(ROWS[i],default=str)) for i in ids if i in ROWS]
    if SINK[0] is not None:
        for d in docs:
            try: SINK[0](d)
            except Exception as e: STAT['errors']+=1; print('moogle sink err',type(e).__name__,flush=True)
    if save_file and FILE[0]:
        try:
            tmp=FILE[0]+'.tmp'
            with open(tmp,'w') as f: json.dump([ROWS[t] for t in _seen_order if t in ROWS],f,default=str)
            os.replace(tmp,FILE[0])
        except Exception as e: print('moogle save err',type(e).__name__,flush=True)
def _rank(r): return (r.get('settle') is not None,len(r.get('entries') or {}),len(r.get('decisions') or []))
def merge_in(items):
    for r in sorted([x for x in items or [] if isinstance(x,dict) and x.get('ticker')],key=lambda x:x.get('closesAt') or 0):
        r={k:v for k,v in r.items() if k!='_id'}; x=ROWS.get(r['ticker'])
        if x is None: ROWS[r['ticker']]=r; _seen_order.append(r['ticker'])
        elif _rank(r)>_rank(x): x.clear(); x.update(r)
    while len(_seen_order)>MAX_KEEP: ROWS.pop(_seen_order.popleft(),None)
def load(data_dir):
    FILE[0]=os.path.join(data_dir,'moogle_shadow.json')
    try:
        with open(FILE[0]) as f: merge_in(json.load(f))
    except Exception: pass
# ---------------------------------------------------------------- read-only summary -----------------------------------
def summary():
    with _lk: return _summary(list(ROWS.values()))
def _summary(rows):
    fin=[r for r in rows if r.get('settle') in ('yes','no')]
    out={}
    for var in VARIANTS:
        es=[(r,r['entries'][var]) for r in fin if var in (r.get('entries') or {})]
        w=sum(1 for r,e in es if r['pnl'].get(var,0)>0)
        out[var]=dict(n=len(es),wins=w,losses=len(es)-w,pnl=round(sum(r['pnl'].get(var,0) for r,e in es),2),
                      open=sum(1 for r in rows if r.get('settle') is None and var in (r.get('entries') or {})))
    sig=[r for r in fin if 'none' in (r.get('entries') or {})]
    for var in ('full','strong'):
        vet=[r for r in sig if var not in r['entries']]
        out[var]['vetoed']=dict(n=len(vet),wouldHaveWon=sum(1 for r in vet if r['pnl'].get('none',0)>0),wouldHavePnl=round(sum(r['pnl'].get('none',0) for r in vet),2))
        lat=[r for r in sig if var in r['entries'] and r['entries'][var]['at']>r['entries']['none']['at']+1]
        out[var]['enteredLater']=len(lat)
    def brier(pairs): return dict(n=len(pairs),brier=round(sum((p-y)**2 for p,y in pairs)/len(pairs),4) if pairs else None,acc=round(sum(1 for p,y in pairs if (p>=.5)==(y==1))/len(pairs),3) if pairs else None)
    br={'firstSignal':brier([(r['first']['p'],r['y']) for r in fin if r.get('first') and r['first'].get('p') is not None])}
    for cp in CHECKPOINTS: br[f'tau{cp}']=brier([(r['checkpoints'][str(cp)]['p'],r['y']) for r in fin if str(cp) in (r.get('checkpoints') or {})])
    allc=[(c['p'],r['y']) for r in fin for k,c in (r.get('checkpoints') or {}).items() if 60<=int(k)<=600]
    br['checkpoints1to10min']=brier(allc)
    elo=[(r['first']['p'],r['first']['eloP'],r['y']) for r in fin if r.get('first') and r['first'].get('eloP') is not None]
    br['vsEloEdgeSameTick']=dict(n=len(elo),brierMoogle=round(sum((a-y)**2 for a,b,y in elo)/len(elo),4) if elo else None,brierElo=round(sum((b-y)**2 for a,b,y in elo)/len(elo),4) if elo else None)
    spec=dict(windowsWithSignal=len([r for r in rows if r.get('first')]),specOpened=len([r for r in rows if r.get('specOpened')]))
    return dict(variants=out,brier=br,spec=spec,windows=len(rows),settledWindows=len(fin),stats=dict(STAT))
def feed_status(now=None):
    now=time.time() if now is None else now; per={}
    for c,s in SER.items():
        per[c]=dict(index=INDEX[c],newestSec=s.cur,lagSec=round(max(0.0,now-s.cur),1) if s.cur is not None else None,
                    lastRxAgo=round(now-s.lastAddAt,1) if s.lastAddAt else None,warm=s.warm() if s.cur is not None else False,
                    seconds=len(s.ff),backfilled=s.backfilled,missingLast900=s.missing(s.cur-899,s.cur) if s.cur is not None else None)
    W=dict(FEED['ws']); R={k:v for k,v in FEED['rest'].items() if not k.startswith('_')}
    R['backoffSec']=max(0,round(R['backoffUntil']-now)); R.pop('backoffUntil',None)
    return dict(enabled=ENABLED,started=FEED['started'],source='Kalshi WS cfbenchmarks_value (1Hz) + REST /trade-api/v2/cfbenchmarks/values backfill/repair (read-only)',
                ws=W,rest=R,coins=per)
def current(now=None):
    now=time.time() if now is None else now; out={}
    for c in INDEX:
        r=CUR.get(c); w=WIN.get(c)
        out[c]=None if not r else dict(p=r['p'],pBase=r['pBase'],z=r['z'],tau=r['tau'],lagSec=round(now-r['t'],1),ticker=r.get('ticker'),strike=r['K'],S=r['S'])
        if out[c] is None and w: out[c]=dict(p=None,ticker=w['ticker'],why='feed not warm/fresh')
    return out
def api(limit=120,full=False):
    with _lk: rows=[json.loads(json.dumps(ROWS[t],default=str)) for t in list(_seen_order) if t in ROWS][-limit:]
    if not full:
        for r in rows: r['decisions']=r.get('decisions',[])[-3:]
    return dict(shadowOnly=True,model=dict(version=PARAMS.get('version'),fittedThrough=PARAMS.get('fitted_through'),strongVeto=STRONG,definition=PARAMS.get('definition')),
                status=feed_status(),current=current(),summary=summary(),trades=rows[::-1],
                note='SHADOW ONLY: never places, sizes or blocks any trade. none = Spec mom/CVD side ungated; full = MoogleEdge must agree; strong = vetoed only when MoogleEdge disagrees with |p-0.5|>=0.20. $1 incl. taker fee, held to settlement, first eligible Spec tick with >=60s left.')
_bc=[0,None]
def brief():
    if _bc[1] is not None and time.time()-_bc[0]<20: return _bc[1]
    _bc[1]=_brief(); _bc[0]=time.time(); return _bc[1]
def _brief():
    s=summary()['variants']; fs=feed_status(); lags=[v['lagSec'] for v in fs['coins'].values() if v['lagSec'] is not None]
    cur=' '.join(f"{c} {v['p']:.2f}" for c,v in current().items() if v and v.get('p') is not None)
    return (f"{cur or 'warming up'} · lag {max(lags):.0f}s · " if lags else 'feed starting · ')+' · '.join(f"{k} {v['wins']}-{v['losses']} {v['pnl']:+.2f}" for k,v in s.items())
