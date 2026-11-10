"""Gap REGIME SHADOW (owner t1086u, Nov 9 2026). PAPER/SHADOW ONLY: no order path, imports nothing from live/gaplive,
never touches pt.trades, the gap paper book, gap live (cap/used/paused), Spec, MoogleEdge's book or the limit shadow.
Why: tonight's gap losses were YES bets in a bearish tape (lower highs/lows, 9 of 32 rounds YES) while gap NO went 6-0.
REGIME (per coin, refreshed <= every 30s from data already in the app; no new market polling per tick):
  a) settled rounds : YES share of the coin's last 4 settled 15m rounds (Kalshi public settled markets, fetched once at boot
                      (limit 24) then limit 3 only after each 15m close, in a background thread). <=25% -> -1, >=75% -> +1.
  b) price structure: crypto = MoogleEdge CF RTI 1s feed (already in memory), last 60 min split into four 15-min blocks;
                      >=2 of 3 block steps with BOTH a lower high and a lower low (and no higher-high+higher-low step) -> -1,
                      mirror -> +1. Fallback (metals, or CF feed not warm): the last 5 settle values (Kalshi expiration_value):
                      >=3 of 4 steps lower and last<first -> -1, mirror -> +1.
  coin score = a + b;  bearish < 0 < bullish, else neutral (a conflict cancels).  pair score = sum of the two legs.
  Extra tags: hour of day (ET) of the entry; volume proxy = previous settled round's Kalshi volume_fp for the laggard vs the
  median of its last <=24 settled rounds (6h): low <0.75x, mid, high >1.33x (absolute volume also recorded).
VARIANTS (each its own $1 book on every Rule A shadow signal, all three pairs; $1 incl. taker entry fee; exit = gap paper
trail: arm at entry+15c, sell at our bid on a 10c giveback (minus taker fee), else hold; hold_pnl always recorded):
  v1 'tag'            : base Rule A at the signal ask + tags: counter = YES while the PAIR is bearish or NO while bullish;
                        deepFill = fresh our-side ask >= 10c below the signal ask (the GP55 pattern).
  v2 'strict-counter' : counter-regime entries need a strong leader: leader prob for the traded side >= LEAD_BAR (0.85)
                        OR MoogleEdge p for the laggard's traded side >= MOOGLE_BAR (0.75, crypto, fresh). Else skipped.
  v3 'flip'           : like v2, but a weak counter entry takes the OPPOSITE side on the laggard at its current ask
                        (ask 10-90c, yes spread <=5c), e.g. NO in a bearish tape."""
import json, math, os, statistics, threading, time
from datetime import datetime
try: from paper_tz import ET
except Exception: ET=None
E=os.environ.get
ENABLED=E('GAP_REGIME','1')!='0'
SERIES={'BTC':'KXBTC15M','ETH':'KXETH15M','SOL':'KXSOL15M','XRP':'KXXRP15M','GOLD':'KXGOLD15M','SILVER':'KXSILVER15M'}
CRYPTO=('BTC','ETH','SOL','XRP')
N_ROUNDS=4; BEAR_SHARE=0.25; BULL_SHARE=0.75; ROUND_STALE_SEC=7200
STRUCT_SEC=3600; BLOCK_SEC=900; CF_STALE_SEC=120
LEAD_BAR=float(E('GAP_REGIME_LEAD_BAR','0.85')); MOOGLE_BAR=float(E('GAP_REGIME_MOOGLE_BAR','0.75'))
DEEP=float(E('GAP_REGIME_DEEP_FILL','0.10'))
ARM=0.15; DROP=0.10; STAKE=1.0; FRESH_SEC=15.0; MAX_KEEP=int(E('GAP_REGIME_MAX_KEEP','3000'))
VOL_LO=0.75; VOL_HI=1.33; HIST_KEEP=24; REFRESH_SEC=30.0; FILE_SAVE_SEC=60.0
VARIANTS=('v1','v2','v3'); VNAME={'v1':'tag','v2':'strict-counter','v3':'flip'}
FETCH=[None]   # fn(series,limit)->[settled market dicts] (server: public Kalshi GET, read-only)
QUOTE=[None]   # fn(ticker)->{'yesBid','yesAsk'} read-only WS quote cache
SINK=[None]    # fn(item) non-blocking persistence (server: GP.upsert, lease-gated)
HSINK=[None]   # fn(doc)  round history persistence
HIST={c:[] for c in SERIES}   # coin -> [dict(close,ticker,result,vol,exp)] oldest..newest
REG={}; PAIRS={}; ITEMS=[]; _by={}; _seen=set(); _lk=threading.RLock(); _path=[None]
STAT=dict(signals=0,entries=0,skipped=0,flipped=0,trailExits=0,settled=0,errors=0,fetches=0,fetchErrors=0,refreshes=0)
_st=dict(lastRefresh=0.0,lastFile=0.0,fileDirty=False,lastTry={},started=False)
def fee(c,p): return math.ceil(7*c*p*(1-p))/100
def _f(x):
    try: return float(x) if x is not None and x!='' else None
    except Exception: return None
def _iso(s):
    try: return datetime.fromisoformat(str(s).replace('Z','+00:00')).timestamp()
    except Exception: return None
def hour_et(ts):
    try: return datetime.fromtimestamp(ts,ET).hour if ET else None
    except Exception: return None
# ------------------------------------------------------------ settled rounds ------------------------------------------
def add_rounds(coin,markets):
    """Merge settled market dicts (any order) into HIST[coin]. Returns number of new rounds."""
    if coin not in HIST: return 0
    with _lk:
        cur={r['close']:r for r in HIST[coin]}; n=0
        for m in markets or []:
            try:
                res=(m.get('result') or '').lower()
                if res not in ('yes','no'): continue
                c=_iso(m.get('close_time')) if m.get('close_time') else _f(m.get('close'))
                if not c: continue
                vol=next((m.get(k) for k in ('volume_fp','volume','vol') if m.get(k) is not None),None)
                r=dict(close=round(c),ticker=m.get('ticker'),result=res,vol=_f(vol),
                       exp=_f(m.get('expiration_value') if m.get('expiration_value') is not None else m.get('exp')),
                       strike=_f(m.get('floor_strike') if m.get('floor_strike') is not None else m.get('strike')))
                if r['close'] not in cur: n+=1
                cur[r['close']]=r
            except Exception: pass
        HIST[coin]=sorted(cur.values(),key=lambda r:r['close'])[-HIST_KEEP:]
    return n
def _need_fetch(coin,now):
    last=HIST[coin][-1]['close'] if HIST.get(coin) else 0
    boundary=math.floor(now/900)*900
    if last>=boundary: return None
    if now<boundary+20: return None
    tries=_st['lastTry'].get(coin,(0,0)); gap=60 if tries[1]<3 else 300
    if now-tries[0]<gap: return None
    return HIST_KEEP if (not HIST.get(coin) or boundary-last>3*900) else 3
def poll_rounds(now=None):
    """One pass: fetch settled rounds only for coins whose latest settled round is older than the last 15m close."""
    now=time.time() if now is None else now; got=0
    if FETCH[0] is None: return 0
    for coin,ser in SERIES.items():
        lim=_need_fetch(coin,now)
        if lim is None: continue
        t=_st['lastTry'].get(coin,(0,0))
        try:
            ms=FETCH[0](ser,lim); STAT['fetches']+=1; n=add_rounds(coin,ms); got+=n
            _st['lastTry'][coin]=(now,0 if n else t[1]+1)
        except Exception as e:
            STAT['fetchErrors']+=1; _st['lastTry'][coin]=(now,t[1]+1); print('gapregime fetch err',coin,type(e).__name__,flush=True)
    if got:
        refresh(now,force=True)
        if HSINK[0] is not None:
            try: HSINK[0](dict(hist=HIST,updatedAt=now))
            except Exception as e: print('gapregime hsink err',e,flush=True)
    return got
def _loop():
    while True:
        try: poll_rounds()
        except Exception as e: print('gapregime poll err',e,flush=True)
        time.sleep(20)
def start():
    if _st['started'] or not ENABLED: return
    _st['started']=True; threading.Thread(target=_loop,daemon=True,name='gapregime-rounds').start()
# ------------------------------------------------------------ regime ---------------------------------------------------
def vote_rounds(coin,now):
    h=[r for r in HIST.get(coin) or [] if r.get('result') in ('yes','no')][-N_ROUNDS:]
    if len(h)<N_ROUNDS or now-h[-1]['close']>ROUND_STALE_SEC: return None,None,''.join('Y' if r['result']=='yes' else 'N' for r in h)
    sh=sum(1 for r in h if r['result']=='yes')/len(h)
    return (-1 if sh<=BEAR_SHARE+1e-9 else (1 if sh>=BULL_SHARE-1e-9 else 0)),round(sh,3),''.join('Y' if r['result']=='yes' else 'N' for r in h)
def _blocks_vote(blocks):
    dn=sum(1 for i in range(1,len(blocks)) if blocks[i][0]<blocks[i-1][0] and blocks[i][1]<blocks[i-1][1])
    up=sum(1 for i in range(1,len(blocks)) if blocks[i][0]>blocks[i-1][0] and blocks[i][1]>blocks[i-1][1])
    v=-1 if (dn>=2 and up==0) else (1 if (up>=2 and dn==0) else 0)
    return v,dn,up
def cf_values(coin,now):
    """Last STRUCT_SEC forward-filled CF RTI 1s values from the MoogleEdge feed (read-only), or None."""
    try: import moogle as M
    except Exception: return None
    s=M.SER.get(coin) if hasattr(M,'SER') else None
    if s is None or s.cur is None or now-s.cur>CF_STALE_SEC: return None
    with M._lk: ff=list(s.ff)
    if len(ff)<STRUCT_SEC: return None
    return ff[-STRUCT_SEC:]
def vote_structure(coin,now,vals=None):
    vals=cf_values(coin,now) if (vals is None and coin in CRYPTO) else vals
    if vals and len(vals)>=STRUCT_SEC:
        nb=STRUCT_SEC//BLOCK_SEC; bl=[]
        for i in range(nb):
            seg=vals[len(vals)-STRUCT_SEC+i*BLOCK_SEC:len(vals)-STRUCT_SEC+(i+1)*BLOCK_SEC:5]
            bl.append((max(seg),min(seg)))
        v,dn,up=_blocks_vote(bl)
        return v,dict(src='cf-rti-60m',blocks=[[round(a,6),round(b,6)] for a,b in bl],lowerHL=dn,higherHL=up)
    ex=[r['exp'] for r in (HIST.get(coin) or []) if r.get('exp')][-5:]
    if len(ex)<5 or now-(HIST[coin][-1]['close'] if HIST.get(coin) else 0)>ROUND_STALE_SEC: return None,dict(src='none')
    lo=sum(1 for i in range(1,5) if ex[i]<ex[i-1]); hi=sum(1 for i in range(1,5) if ex[i]>ex[i-1])
    v=-1 if (lo>=3 and ex[-1]<ex[0]) else (1 if (hi>=3 and ex[-1]>ex[0]) else 0)
    return v,dict(src='settle-values-75m',values=ex,lowerSteps=lo,higherSteps=hi)
def _lab(sc): return 'bearish' if sc<0 else ('bullish' if sc>0 else 'neutral')
def volume_ctx(coin):
    h=[r['vol'] for r in HIST.get(coin) or [] if r.get('vol')]
    if not h: return dict(prevVol=None,medianVol=None,ratio=None,bucket='unknown')
    med=statistics.median(h); last=h[-1]; ratio=round(last/med,3) if med else None
    b='unknown' if ratio is None else ('low' if ratio<VOL_LO else ('high' if ratio>VOL_HI else 'mid'))
    return dict(prevVol=round(last),medianVol=round(med),ratio=ratio,bucket=b,n=len(h))
def refresh(now=None,force=False):
    now=time.time() if now is None else now
    if not force and now-_st['lastRefresh']<REFRESH_SEC: return REG
    _st['lastRefresh']=now; STAT['refreshes']+=1; out={}
    for c in SERIES:
        try:
            a,sh,seq=vote_rounds(c,now); b,sd=vote_structure(c,now)
            sc=(a or 0)+(b or 0)
            out[c]=dict(regime=_lab(sc),score=sc,roundsVote=a,yesShare4=sh,last4=seq,structureVote=b,structure=sd,volume=volume_ctx(c),at=round(now,1),
                        lastSettledClose=HIST[c][-1]['close'] if HIST.get(c) else None)
        except Exception as e: STAT['errors']+=1; out[c]=dict(regime='neutral',score=0,error=str(e)[:120])
    with _lk:
        REG.clear(); REG.update(out)
        for p in ('BTC-ETH','SOL-XRP','GOLD-SILVER'):
            a,b=p.split('-'); sc=(out.get(a) or {}).get('score',0)+(out.get(b) or {}).get('score',0)
            PAIRS[p]=dict(regime=_lab(sc),score=sc,legs={a:(out.get(a) or {}).get('regime'),b:(out.get(b) or {}).get('regime')})
    return REG
def pair_regime(pair):
    return dict(PAIRS.get(pair) or dict(regime='neutral',score=0,legs={}))
# ------------------------------------------------------------ shadow books ---------------------------------------------
def _quote(tk,side,m):
    """(ourAsk, ourBid, src) - fresh WS quote first, else this tick's market for the ticker."""
    q=None
    if QUOTE[0] is not None:
        try: q=QUOTE[0](tk)
        except Exception: q=None
    src='ws'
    if not (q and q.get('yesBid') is not None and q.get('yesAsk') is not None):
        q=m if (m and m.get('ticker')==tk) else None; src='tick'
    if not q: return None,None,None
    yb,ya=_f(q.get('yesBid')),_f(q.get('yesAsk'))
    if side=='YES': return ya,yb,src
    return (None if yb is None else round(1-yb,4)),(None if ya is None else round(1-ya,4)),src
def moogle_side_p(coin,ticker,side,now):
    try:
        import moogle as M; r=M.CUR.get(coin)
        if not r or r.get('ticker')!=ticker or now-float(r.get('t') or 0)>20 or r.get('p') is None: return None
        p=float(r['p']); return round(p if side=='YES' else 1-p,4)
    except Exception: return None
def _twin_fresh(sk):
    try:
        import gapshadow as G
        for t in reversed(G.PAPER[-12:]):
            if t.get('shadowKey')==sk: return _f(t.get('freshAsk'))
    except Exception: pass
    return None
def classify(st,m,now):
    """Tags for a Rule A shadow signal (pure apart from read-only caches)."""
    side=st['side']; pr=pair_regime(st.get('pair')); reg=pr['regime']
    counter=(side=='YES' and reg=='bearish') or (side=='NO' and reg=='bullish')
    sig=float(st['entry']); fa,_,fsrc=_quote(st['ticker'],side,m)
    try: import gapshadow as G; sk=G.doc_id(st)
    except Exception: sk=f"shadow|{st.get('rule')}|{st.get('pair')}|{float(st.get('closesAt') or 0):.3f}"
    if fa is None: fa=_twin_fresh(sk); fsrc='paper-twin' if fa is not None else None
    deep=fa is not None and fa<=sig-DEEP+1e-9
    ly=_f(st.get('leaderYesMid')); lconf=None if ly is None else round(ly if side=='YES' else 1-ly,4)
    mp=moogle_side_p(st.get('laggard'),st['ticker'],side,now)
    strong=(lconf is not None and lconf>=LEAD_BAR-1e-9) or (mp is not None and mp>=MOOGLE_BAR-1e-9)
    lag=st.get('laggard'); cr=REG.get(lag) or {}
    return sk,dict(pairRegime=reg,pairScore=pr['score'],legs=pr.get('legs'),laggardRegime=cr.get('regime'),counter=counter,
                   freshAsk=fa,freshSrc=fsrc,deepFill=deep,leaderConf=lconf,moogleP=mp,strong=strong,
                   hourET=hour_et(now),volume=dict(cr.get('volume') or volume_ctx(lag)),
                   yesShare4=cr.get('yesShare4'),last4=cr.get('last4'),structureVote=cr.get('structureVote'),structureSrc=(cr.get('structure') or {}).get('src'))
def _entry(o,price):
    c0=STAKE/price; ef=fee(c0,price)
    o.update(status='filled',entry=round(price,4),contracts=round(max(STAKE-ef,0.001)/price,4),entryFee=ef,peakBid=None)
def make_items(st,m,now):
    sk,tg=classify(st,m,now)
    if sk in _seen: return []
    _seen.add(sk); STAT['signals']+=1; out=[]
    weak_counter=tg['counter'] and not tg['strong']
    for v in VARIANTS:
        o=dict(id=f'{sk}|{v}',shadowKey=sk,variant=v,variantName=VNAME[v],mode='shadow',book='gap-regime',rule='A',pair=st.get('pair'),
               laggard=st.get('laggard'),leader=st.get('leader'),ticker=st['ticker'],signalSide=st['side'],side=st['side'],flipped=False,
               signalAsk=float(st['entry']),closesAt=st['closesAt'],openedAt=now,leftSec=round(st['closesAt']-now,1),status=None,skipReason=None,
               entry=None,contracts=None,entryFee=None,exit=None,exitFee=None,pnl=None,hold_pnl=None,settle=None,settledAt=None,
               gapDown=st.get('gapDown'),leaderYesMid=st.get('leaderYesMid'),laggardYesMid=st.get('laggardYesMid'),tags=tg)
        if v=='v1' or not weak_counter: _entry(o,float(st['entry']))
        elif v=='v2': o.update(status='skipped',skipReason=f"counter-regime ({tg['pairRegime']}) {st['side']} below bar: leader {tg['leaderConf']} < {LEAD_BAR}, moogle {tg['moogleP']} < {MOOGLE_BAR}")
        else:
            opp='NO' if st['side']=='YES' else 'YES'; yb,ya=_f((m or {}).get('yesBid')),_f((m or {}).get('yesAsk'))
            ask,_,_=_quote(st['ticker'],opp,m)
            if ask is None and m and m.get('ticker')==st['ticker']: ask=_f(m.get('noAsk') if opp=='NO' else m.get('yesAsk'))
            if ask is None or not (0.10<=ask<=0.90) or (yb is not None and ya is not None and ya-yb>0.05+1e-9):
                o.update(status='skipped',skipReason=f'flip: {opp} ask {ask} unusable')
            else: o.update(side=opp,flipped=True); _entry(o,ask); STAT['flipped']+=1
        if o['status']=='filled': STAT['entries']+=1
        else: STAT['skipped']+=1
        out.append(o)
    with _lk:
        for o in out: ITEMS.append(o); _by[o['id']]=o
        if len(ITEMS)>MAX_KEEP:
            for o in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(o['id'],None)
            del ITEMS[:len(ITEMS)-MAX_KEEP]
    return out
def on_tick(mkts,now=None,signals=None):
    """Pure paper bookkeeping on Rule A shadow signals (gapshadow.TRADES); trail exits for open items."""
    if not ENABLED: return []
    now=time.time() if now is None else now; ch=[]
    mkts={k:v for k,v in (mkts or {}).items() if isinstance(v,dict)}
    try: refresh(now)
    except Exception as e: STAT['errors']+=1; print('gapregime refresh err',e,flush=True)
    with _lk: cur=[o for o in ITEMS if o['status']=='filled' and o.get('exit') is None and now<o['closesAt']]
    for o in cur:
        try:
            _,bid,src=_quote(o['ticker'],o['side'],mkts.get(o.get('laggard')))
            if bid is None or bid<=0: continue
            o['peakBid']=round(max(o.get('peakBid') or 0,bid),4)
            if o['peakBid']>=o['entry']+ARM-1e-9:
                if not o.get('trailArmedAt'): o['trailArmedAt']=now; ch.append(o)
                if bid<=o['peakBid']-DROP+1e-9:
                    xf=fee(o['contracts'],bid)
                    o.update(exit=round(bid,4),exitFee=xf,pnl=round(o['contracts']*bid-xf-STAKE,2),exitedAt=now,exitLeftSec=round(o['closesAt']-now,1),
                             exitReason=f"trail: peak {o['peakBid']:.2f} (armed at {o['entry']:.2f}+{ARM:.2f}) -> bid {bid:.2f}")
                    STAT['trailExits']+=1; ch.append(o)
        except Exception as e: STAT['errors']+=1; print('gapregime exit err',e,flush=True)
    for st in signals or []:
        try:
            if st.get('rule')!='A' or st.get('mode')!='shadow' or now>=st['closesAt']: continue
            if now-float(st.get('openedAt') or 0)>FRESH_SEC: continue
            n=make_items(st,mkts.get(st.get('laggard')),now)
            if n:
                ch.extend(n); t=n[0]['tags']
                print('GAPREGIME',st.get('pair'),st.get('laggard'),st.get('side'),st.get('entry'),t['pairRegime'],'counter' if t['counter'] else '-','deep' if t['deepFill'] else '-',[(o['variant'],o['side'] if o['status']=='filled' else 'skip') for o in n],flush=True)
        except Exception as e: STAT['errors']+=1; print('gapregime signal err',e,flush=True)
    if ch: _changed(ch,now)
    return ch
_try={}
def settle(result_fn,now=None):
    now=time.time() if now is None else now; res={}; ch=[]
    with _lk: todo=[o for o in ITEMS if o.get('settle') is None and now>=o['closesAt']+5]
    if not todo: return False
    try:
        import gapshadow as G; sh={G.doc_id(t):t.get('settle') for t in G.TRADES[-400:] if t.get('settle') in ('yes','no')}
    except Exception: sh={}
    for o in todo:
        try:
            tk=o['ticker']; r=sh.get(o['shadowKey'])
            if r is None:
                if tk not in res:
                    if len(res)>=3 or now-_try.get(tk,0)<30 or result_fn is None: continue
                    _try[tk]=now
                    try: res[tk]=result_fn(tk)
                    except Exception: res[tk]=None
                r=res[tk]
            if r not in ('yes','no'): continue
            o['settle']=r; o['settledAt']=now
            if o['status']=='filled':
                win=(r=='yes')==(o['side']=='YES'); o['hold_pnl']=round((o['contracts'] if win else 0)-STAKE,2)
                if o.get('exit') is None: o['pnl']=o['hold_pnl']; o['heldToSettle']=True
            STAT['settled']+=1; ch.append(o)
        except Exception as e: STAT['errors']+=1; print('gapregime settle err',e,flush=True)
    if ch: _changed(ch,now)
    return bool(ch)
# ------------------------------------------------------------ persistence ----------------------------------------------
def _changed(items,now):
    if SINK[0] is not None:
        for o in items:
            try: SINK[0](json.loads(json.dumps(o,default=str)))
            except Exception as e: print('gapregime sink err',e,flush=True)
    _st['fileDirty']=True
    if now-_st['lastFile']>=FILE_SAVE_SEC or any(o.get('settle') for o in items): _save(now)
def _rank(o): return (o.get('settle') is not None,o.get('exit') is not None,o.get('peakBid') or 0)
def merge_in(items):
    with _lk:
        for o in items or []:
            try:
                if not isinstance(o,dict) or not o.get('id') or not o.get('shadowKey'): continue
                o={k:v for k,v in o.items() if k!='_id'}; x=_by.get(o['id'])
                if x is None: ITEMS.append(o); _by[o['id']]=o
                elif _rank(o)>_rank(x): x.clear(); x.update(o)
            except Exception: pass
        ITEMS.sort(key=lambda o:(o.get('openedAt') or 0,o.get('variant') or ''))
        if len(ITEMS)>MAX_KEEP:
            for o in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(o['id'],None)
            del ITEMS[:len(ITEMS)-MAX_KEEP]
        _seen.clear(); _seen.update(o['shadowKey'] for o in ITEMS)
def merge_hist(doc):
    for c,rows in ((doc or {}).get('hist') or {}).items():
        if c in HIST: add_rounds(c,[r for r in rows or [] if isinstance(r,dict)])
def load(data_dir):
    _path[0]=os.path.join(data_dir,'gap_regime.json')
    try:
        with open(_path[0]) as f: d=json.load(f)
        merge_in(d.get('items')); merge_hist(d)
    except Exception: pass
def snapshot():
    with _lk: return [dict(o) for o in ITEMS]
def _save(now=None):
    _st['lastFile']=time.time() if now is None else now; _st['fileDirty']=False
    if not _path[0]: return
    try:
        tmp=_path[0]+'.tmp'
        with open(tmp,'w') as f: json.dump(dict(items=snapshot(),hist=HIST),f,default=str)
        os.replace(tmp,_path[0])
    except Exception as e: print('gapregime save err',e,flush=True)
# ------------------------------------------------------------ read-only summary ----------------------------------------
def _agg(rows):
    fin=[o for o in rows if o.get('status')=='filled' and o.get('settle') is not None]
    return dict(n=len(rows),settled=len(fin),wins=sum(1 for o in fin if (o.get('pnl') or 0)>0),losses=sum(1 for o in fin if (o.get('pnl') or 0)<=0),
                pnl=round(sum(o.get('pnl') or 0 for o in fin),2),holdPnl=round(sum(o.get('hold_pnl') or 0 for o in fin),2),
                open=sum(1 for o in rows if o.get('status')=='filled' and o.get('settle') is None))
def summary():
    its=snapshot(); by={v:[o for o in its if o['variant']==v] for v in VARIANTS}
    v1={o['shadowKey']:o for o in by['v1']}
    books={}
    for v in VARIANTS:
        rows=by[v]; ent=[o for o in rows if o['status']=='filled']; sk=[o for o in rows if o['status']=='skipped']
        b=dict(name=VNAME[v],entries=_agg(ent),skipped=len(sk))
        skf=[v1.get(o['shadowKey']) for o in sk if o.get('settle') is not None]
        b['skippedWhatBaseMade']=_agg([x for x in skf if x])
        if v=='v3':
            fl=[o for o in ent if o.get('flipped')]
            b['flipped']=_agg(fl); b['flippedWhatBaseMade']=_agg([v1.get(o['shadowKey']) for o in fl if v1.get(o['shadowKey'])])
        books[v]=b
    g=lambda f:dict(tagged=_agg([o for o in by['v1'] if f(o['tags'])]),untagged=_agg([o for o in by['v1'] if not f(o['tags'])]))
    tagged=dict(counter=g(lambda t:t.get('counter')),deepFill=g(lambda t:t.get('deepFill')),anyTag=g(lambda t:t.get('counter') or t.get('deepFill')))
    hours={}; vols={}; pairs={}; sides={}
    for o in by['v1']:
        t=o['tags']
        for d,k in ((hours,t.get('hourET')),(vols,(t.get('volume') or {}).get('bucket') or 'unknown'),(pairs,o.get('pair')),(sides,f"{o['signalSide']}|{t.get('pairRegime')}")):
            d.setdefault(str(k),[]).append(o)
    byh={k:dict(all=_agg(v),counter=_agg([o for o in v if o['tags'].get('counter')])) for k,v in sorted(hours.items(),key=lambda kv:int(kv[0]) if kv[0].isdigit() else 99)}
    byv={k:dict(all=_agg(v),counter=_agg([o for o in v if o['tags'].get('counter')])) for k,v in vols.items()}
    return dict(enabled=ENABLED,shadowOnly=True,stats=dict(STAT),
                thresholds=dict(roundsN=N_ROUNDS,bearishYesShareMax=BEAR_SHARE,bullishYesShareMin=BULL_SHARE,structure='60 min CF RTI in 4x15-min blocks; >=2 of 3 steps lower-high+lower-low (no opposite step) = down',
                                leaderBar=LEAD_BAR,moogleBar=MOOGLE_BAR,deepFillC=round(DEEP*100),volumeBuckets=f'prev settled round volume_fp / median of last {HIST_KEEP}: low<{VOL_LO}, high>{VOL_HI}'),
                exit=f'trail arm entry+{round(ARM*100)}c / {round(DROP*100)}c giveback, else hold; holdPnl = hold to settlement',stakeUsd=STAKE,
                books=books,taggedVsUntagged=tagged,byHourET=byh,byVolumeBucket=byv,byPair={k:_agg(v) for k,v in pairs.items()},bySideRegime={k:_agg(v) for k,v in sides.items()},
                volumeProxy='Kalshi volume_fp of the laggard\'s previous settled 15m round vs its median over the last <=24 settled rounds (public settled-markets data)',
                note='SHADOW ONLY: never places, sizes or blocks a trade. v1 = Rule A + tags; v2 = skip weak counter-regime; v3 = flip weak counter-regime to the opposite side on the laggard. pnl = trail exit; counts settled entries only.')
def current():
    with _lk: return dict(coins=json.loads(json.dumps(REG,default=str)),pairs=json.loads(json.dumps(PAIRS,default=str)))
def api(limit=300,items=True):
    its=snapshot()[-limit:] if items else []
    return dict(current=current(),summary=summary(),items=its[::-1])
_bc=[0,None]
def brief():
    if _bc[1] is not None and time.time()-_bc[0]<20: return _bc[1]
    s=summary(); b=s['books']; pr=PAIRS
    rg=' '.join(f"{p} {v['regime'][:4]}" for p,v in pr.items()) or 'warming up'
    tv=s['taggedVsUntagged']['counter']
    _bc[1]=(f"{rg} · "+' · '.join(f"{VNAME[k]} {v['entries']['wins']}-{v['entries']['losses']} {v['entries']['pnl']:+.2f}" for k,v in b.items())
            +f" · counter {tv['tagged']['pnl']:+.2f} vs other {tv['untagged']['pnl']:+.2f}"); _bc[0]=time.time(); return _bc[1]
