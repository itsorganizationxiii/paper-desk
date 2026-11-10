"""Late-window SHADOW (owner t1117u, Nov 10 2026). PAPER/SHADOW ONLY: never places, sizes or blocks a trade;
never touches pt.trades, Spec, gap paper/live, night/regime/limit shadows, MoogleEdge's book, stakes,
or the paused/live toggle.

Model (frozen from /workspace/research-packet/backtest, T=60 edge>=0.10; 180/120/30 are the same rule
at other entry times). Kalshi 15-min crypto settles on the 60-tick CF RTI mean from close-60s through
close-1s. At T seconds left, fair P(mean > strike) uses the locked partial average when any settlement
ticks are already observed; before the final 60s (T=180/120) that lock is empty and the remaining path
is a zero-drift Brownian average of the current 1s CF price. Vol = std of 1s price diffs over the prior
300s. Paper-buy $1 of the side where fair − ask >= 0.10 (fee 0.07*p*(1-p), ceiled like the other
shadows). Hold to settlement.

CF is read from the existing MoogleEdge in-memory 1s series (WS + its own repair). No new CF REST
polling. One quote read per variant time per coin per round (WS cache first; one public /markets
GET only if the cache is empty). Coins: BTC, ETH, SOL, XRP. Metals are skipped.
"""
import json, math, os, threading, time
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
    ET=ZoneInfo('America/New_York')
except Exception:
    ET=None
E=os.environ.get
ENABLED=E('LATEWINDOW','1')!='0'
COINS=('BTC','ETH','SOL','XRP')
VARIANTS=(180,120,60,30)   # seconds left, primary frozen rule is 60
VNAME={180:'t180',120:'t120',60:'t60',30:'t30'}
EDGE=0.10; STAKE=1.0; VOL_LOOKBACK=300; SETTLE_N=60
LATE_SEC=8.0               # still count as this variant if we fire a few seconds late
FRESH_SEC=15.0
MAX_KEEP=int(E('LATEWINDOW_MAX_KEEP','4000'))
FILE_SAVE_SEC=60.0
TTL_DAYS=int(E('LATEWINDOW_TTL_DAYS','120'))
QUOTE=[None]    # fn(ticker)->{'yesBid','yesAsk'} read-only WS quote cache
MARKET=[None]   # fn(ticker)->market dict (public GET), used at most once per variant/coin/round if WS misses
SINK=[None]
ITEMS=[]; _by={}; _lk=threading.RLock(); _path=[None]
WIN={}          # coin -> dict(ticker,strike,close,seenAt)
STAT=dict(evals=0,entries=0,skipped=0,settled=0,errors=0,missed=0,quoteRest=0,noCf=0,noQuote=0,late=0)
_st=dict(lastFile=0.0,fileDirty=False)
_started=[False]
_try={}
# ---------------------------------------------------------------- pure math (unit-tested) -----------------------------
def fee(c,p):
    return math.ceil(7*c*p*(1-p))/100
def ncdf(z):
    return 0.5*(1.0+math.erf(z/math.sqrt(2.0)))
def fair_p_yes(P,strike,locked_sum,n_locked,n_remain,sigma,delay):
    """P(60s settlement mean > strike) with remaining ticks as a BM average from current P.
    Matches research-packet/backtest/bt.py (zero-drift, vol = 1s diff std)."""
    n_tot=n_locked+n_remain
    if n_tot<=0 or not _fin(P) or not _fin(strike): return None
    if n_remain<=0:
        s=locked_sum/n_locked if n_locked else None
        if s is None: return None
        if s>strike: return 1.0
        if s<strike: return 0.0
        return 0.5
    if not _fin(sigma) or sigma<=0: return None
    e_s=(locked_sum+n_remain*P)/n_tot
    std_remain_avg=sigma*math.sqrt(max(delay,0.0)+n_remain/3.0)
    std_s=(n_remain/n_tot)*std_remain_avg
    if std_s<=1e-15:
        if e_s>strike: return 1.0
        if e_s<strike: return 0.0
        return 0.5
    return float(ncdf((e_s-strike)/std_s))
def settle_bounds(close):
    """Inclusive unix seconds for the 60-tick settlement window (close-60 .. close-1)."""
    c=int(round(float(close))); return c-60,c-1
def locked_parts(close,td,getpx):
    """Return (locked_sum, n_locked, n_remain, delay, missing_ok) at decision second td."""
    lo,hi=settle_bounds(close); n_tot=hi-lo+1
    if td>=lo:
        hi_l=min(td,hi); n=hi_l-lo+1
        if n<=0: return 0.0,0,n_tot,max(0,lo-td),True
        miss=0; sm=0.0; got=0
        for t in range(lo,hi_l+1):
            v=getpx(t)
            if v is None or not _fin(v): miss+=1
            else: sm+=float(v); got+=1
        if n and miss/n>0.05: return 0.0,0,n_tot,0,False
        n_remain=n_tot-got
        if n_remain<0: n_remain=0
        delay=0 if got else max(0,lo-td)
        return sm,got,n_remain,delay,True
    return 0.0,0,n_tot,max(0,lo-td),True
def sigma_from_px(getpx,td,look=VOL_LOOKBACK):
    """Std of 1s price diffs over [td-look, td], ddof=1. None if too sparse."""
    a,b=td-look,td; n=b-a+1
    if n<2: return None
    vals=[]; miss=0
    for t in range(a,b+1):
        v=getpx(t)
        if v is None or not _fin(v): miss+=1; vals.append(None)
        else: vals.append(float(v))
    if miss/n>0.1: return None
    d=[]
    for i in range(1,len(vals)):
        if vals[i] is not None and vals[i-1] is not None: d.append(vals[i]-vals[i-1])
    if len(d)<30: return None
    mean=sum(d)/len(d)
    var=sum((x-mean)**2 for x in d)/(len(d)-1)
    s=math.sqrt(var)
    return s if s>0 else None
def _fin(x):
    try: return x is not None and x==x and abs(x)!=float('inf')
    except Exception: return False
def _f(x):
    try: return float(x) if x is not None and x!='' else None
    except Exception: return None
# ---------------------------------------------------------------- CF / quote (read-only) ------------------------------
def _cf_getpx(coin):
    """Read-only accessor over MoogleEdge's in-memory raw 1s series. Never calls CF REST."""
    try:
        import moogle as M
        with M._lk:
            s=M.SER.get(coin)
            if s is None or s.cur is None: return None,None,None
            start,cur,raw=s.start,s.cur,s.raw
            def getpx(t,start=start,cur=cur,raw=raw):
                if t<start or t>cur: return None
                return raw[t-start]
            return getpx,cur,start
    except Exception:
        return None,None,None
def _quote(tk,m=None):
    """One quote read: WS cache first, else a single public market GET (MARKET hook), else tick mkts."""
    q=None; src=None
    if QUOTE[0] is not None:
        try: q=QUOTE[0](tk)
        except Exception: q=None
        if q and q.get('yesBid') is not None and q.get('yesAsk') is not None: src='ws'
    if src is None and MARKET[0] is not None:
        try:
            q=MARKET[0](tk); STAT['quoteRest']+=1
        except Exception: q=None
        if q and (q.get('yesBid') is not None or q.get('yes_bid_dollars') is not None):
            if q.get('yesBid') is None:
                q=dict(yesBid=_f(q.get('yes_bid_dollars')),yesAsk=_f(q.get('yes_ask_dollars')),
                       noBid=_f(q.get('no_bid_dollars')),noAsk=_f(q.get('no_ask_dollars')))
            src='rest'
    if src is None and m and m.get('ticker')==tk and m.get('yesAsk') is not None:
        q=m; src='tick'
    if not q: return None
    yb,ya=_f(q.get('yesBid')),_f(q.get('yesAsk'))
    if ya is None or yb is None: return None
    na=_f(q.get('noAsk'))
    if na is None: na=round(1-yb,4)
    nb=_f(q.get('noBid'))
    if nb is None: nb=round(1-ya,4)
    return dict(yesBid=yb,yesAsk=ya,noBid=nb,noAsk=na,spread=round(ya-yb,4),src=src)
# ---------------------------------------------------------------- books ----------------------------------------------
def doc_id(ticker,T): return f'lw|{ticker}|{int(T)}'
def set_window(coin,k,now=None):
    if coin not in COINS or not isinstance(k,dict): return
    tk,st,cl=k.get('ticker'),_f(k.get('strike')),_f(k.get('close'))
    if not tk or not _fin(st) or not _fin(cl): return
    cl=int(round(cl)); now=time.time() if now is None else now
    cur=WIN.get(coin)
    if cur and cur.get('ticker')==tk and cur.get('close')==cl:
        if cur.get('strike')!=st: cur['strike']=st
        return
    WIN[coin]=dict(ticker=tk,strike=float(st),close=cl,seenAt=now)
def _entry(price):
    c0=STAKE/price; ef=fee(c0,price)
    return dict(entry=round(price,4),contracts=round(max(STAKE-ef,0.001)/price,4),entryFee=ef)
def _mk_item(coin,w,T,now,status,reason=None,**extra):
    o=dict(id=doc_id(w['ticker'],T),shadowKey=doc_id(w['ticker'],T),mode='shadow',book='latewindow',
           variant=T,variantName=VNAME[T],coin=coin,ticker=w['ticker'],strike=w['strike'],
           closesAt=w['close'],openedAt=now,secondsLeft=round(w['close']-now,2),targetLeft=T,
           status=status,skipReason=reason,side=None,fair=None,ask=None,bid=None,spread=None,
           quoteSrc=None,cf=None,sigma=None,nLocked=None,nRemain=None,delay=None,edge=None,
           entry=None,contracts=None,entryFee=None,settle=None,settledAt=None,pnl=None,win=None)
    o.update(extra)
    return o
def _remember(o):
    with _lk:
        x=_by.get(o['id'])
        if x is None:
            ITEMS.append(o); _by[o['id']]=o
            if len(ITEMS)>MAX_KEEP:
                for old in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(old['id'],None)
                del ITEMS[:len(ITEMS)-MAX_KEEP]
        else:
            x.clear(); x.update(o)
        return _by[o['id']]
def _already(ticker,T):
    with _lk: return doc_id(ticker,T) in _by
def evaluate(coin,T,now=None,mkt=None):
    """One decision for (coin, variant T) this round. Idempotent. Returns the item or None if not due."""
    if not ENABLED or coin not in COINS or T not in VARIANTS: return None
    now=time.time() if now is None else now
    w=WIN.get(coin)
    if not w: return None
    left=w['close']-now
    if left>T: return None
    if _already(w['ticker'],T): return None
    # too late for this variant: record only if we had the window in time to have fired
    if left<T-LATE_SEC:
        seen=w.get('seenAt')
        if seen is not None and seen<=(w['close']-T)+0.5:
            o=_mk_item(coin,w,T,now,'skipped','late')
            STAT['late']+=1; STAT['skipped']+=1; STAT['missed']+=1; STAT['evals']+=1
            _remember(o); return o
        return None
    STAT['evals']+=1
    getpx,cur,_=_cf_getpx(coin)
    td=int(math.floor(now))
    if getpx is None or cur is None:
        o=_mk_item(coin,w,T,now,'skipped','no-cf'); STAT['noCf']+=1; STAT['skipped']+=1; _remember(o); return o
    # current CF: exact second, else last finite within 2s
    P=getpx(td)
    if not _fin(P):
        for t in range(td,td-3,-1):
            P=getpx(t)
            if _fin(P): break
    if not _fin(P):
        o=_mk_item(coin,w,T,now,'skipped','no-cf'); STAT['noCf']+=1; STAT['skipped']+=1; _remember(o); return o
    sm,n_locked,n_remain,delay,ok=locked_parts(w['close'],td,getpx)
    if not ok:
        o=_mk_item(coin,w,T,now,'skipped','cf-incomplete'); STAT['noCf']+=1; STAT['skipped']+=1; _remember(o); return o
    sig=sigma_from_px(getpx,td)
    p=fair_p_yes(float(P),w['strike'],sm,n_locked,n_remain,sig,delay)
    if p is None:
        o=_mk_item(coin,w,T,now,'skipped','no-cf',cf=float(P),sigma=sig,nLocked=n_locked,nRemain=n_remain,delay=delay)
        STAT['noCf']+=1; STAT['skipped']+=1; _remember(o); return o
    q=_quote(w['ticker'],mkt)
    if not q:
        o=_mk_item(coin,w,T,now,'skipped','no-quote',fair=round(p,5),cf=float(P),sigma=round(sig,8) if sig else None,
                   nLocked=n_locked,nRemain=n_remain,delay=delay)
        STAT['noQuote']+=1; STAT['skipped']+=1; _remember(o); return o
    e_yes=p-q['yesAsk']; e_no=(1.0-p)-q['noAsk']
    take=None
    if e_yes>=EDGE and e_yes>=e_no: take=('YES',q['yesAsk'],q['yesBid'],e_yes)
    elif e_no>=EDGE and e_no>e_yes: take=('NO',q['noAsk'],q['noBid'],e_no)
    base=dict(fair=round(p,5),ask=round(q['yesAsk'],4),bid=round(q['yesBid'],4),spread=q['spread'],
              quoteSrc=q['src'],cf=float(P),sigma=round(sig,8) if sig else None,
              nLocked=n_locked,nRemain=n_remain,delay=delay,pYes=round(p,5),
              edgeYes=round(e_yes,5),edgeNo=round(e_no,5))
    if take is None or not (0.01<=take[1]<=0.99):
        o=_mk_item(coin,w,T,now,'skipped','edge-below',**base)
        STAT['skipped']+=1; _remember(o); return o
    side,ask,bid,ed=take
    ent=_entry(ask)
    o=_mk_item(coin,w,T,now,'filled',**base)
    o.update(side=side,ask=round(ask,4),bid=round(bid,4) if bid is not None else None,edge=round(ed,5),**ent)
    STAT['entries']+=1; _remember(o); return o
def evaluate_due(now=None,mkts=None):
    """Evaluate every variant that is currently in its fire window. At most one record per (ticker,T)."""
    if not ENABLED: return []
    now=time.time() if now is None else now
    mkts=mkts or {}
    ch=[]
    for coin in COINS:
        if coin in mkts: set_window(coin,mkts[coin],now)
        w=WIN.get(coin)
        if not w: continue
        left=w['close']-now
        m=mkts.get(coin)
        for T in VARIANTS:
            if left>T: continue
            try:
                o=evaluate(coin,T,now,mkt=m)
                if o: ch.append(o)
            except Exception as e:
                STAT['errors']+=1; print('latewindow eval err',coin,T,e,flush=True)
    return ch
def on_tick(mkts,now=None,signals=None):
    """Update windows from this tick's markets and fire any due variants (backup to the scheduler)."""
    if not ENABLED: return []
    now=time.time() if now is None else now
    mkts={k:v for k,v in (mkts or {}).items() if k in COINS and isinstance(v,dict)}
    for coin,m in mkts.items(): set_window(coin,m,now)
    ch=evaluate_due(now,mkts)
    if ch: _changed(ch,now)
    return ch
def settle(result_fn,now=None):
    now=time.time() if now is None else now; res={}; ch=[]
    with _lk: todo=[o for o in ITEMS if o.get('settle') is None and now>=o['closesAt']+5]
    if not todo: return False
    try:
        import gapshadow as G; sh={t.get('ticker'):t.get('settle') for t in G.TRADES[-400:] if t.get('settle') in ('yes','no')}
    except Exception: sh={}
    for o in todo:
        try:
            tk=o['ticker']; r=sh.get(tk)
            if r is None:
                if tk not in res:
                    if len(res)>=4 or now-_try.get(tk,0)<30 or result_fn is None: continue
                    _try[tk]=now
                    try: res[tk]=result_fn(tk)
                    except Exception: res[tk]=None
                r=res[tk]
            if r not in ('yes','no'): continue
            o['settle']=r; o['settledAt']=now
            if o['status']=='filled' and o.get('contracts') is not None:
                win=(r=='yes')==(o['side']=='YES')
                o['win']=bool(win); o['pnl']=round((o['contracts'] if win else 0)-STAKE,2)
            STAT['settled']+=1; ch.append(o)
        except Exception as e: STAT['errors']+=1; print('latewindow settle err',e,flush=True)
    if ch: _changed(ch,now)
    return bool(ch)
# ---------------------------------------------------------------- scheduler (one wake per variant time) ---------------
def _next_wake(now):
    soon=None
    for w in WIN.values():
        for T in VARIANTS:
            t=w['close']-T
            if t+LATE_SEC<now: continue
            if _already(w['ticker'],T): continue
            if soon is None or t<soon: soon=t
    return soon
def _loop():
    while True:
        try:
            if not ENABLED:
                time.sleep(5); continue
            now=time.time(); nxt=_next_wake(now)
            if nxt is None:
                time.sleep(2); continue
            wait=nxt-time.time()
            if wait>0.25:
                time.sleep(min(wait,5.0)); continue
            ch=evaluate_due(time.time())
            if ch: _changed(ch,time.time())
            time.sleep(0.4)  # avoid a tight re-loop on the same second
        except Exception as e:
            STAT['errors']+=1; print('latewindow loop err',e,flush=True); time.sleep(2)
def start():
    if _started[0]: return
    _started[0]=True
    threading.Thread(target=_loop,daemon=True,name='latewindow').start()
# ---------------------------------------------------------------- persistence -----------------------------------------
def _changed(items,now):
    if SINK[0] is not None:
        for o in items:
            try: SINK[0](json.loads(json.dumps(o,default=str)))
            except Exception as e: print('latewindow sink err',e,flush=True)
    _st['fileDirty']=True
    if now-_st['lastFile']>=FILE_SAVE_SEC or any(o.get('settle') for o in items): _save(now)
def _rank(o): return (o.get('settle') is not None,o.get('status')=='filled',o.get('pnl') is not None)
def merge_in(items):
    with _lk:
        for o in items or []:
            try:
                if not isinstance(o,dict) or not o.get('id'): continue
                o={k:v for k,v in o.items() if k!='_id'}; x=_by.get(o['id'])
                if x is None: ITEMS.append(o); _by[o['id']]=o
                elif _rank(o)>_rank(x): x.clear(); x.update(o)
            except Exception: pass
        ITEMS.sort(key=lambda o:(o.get('openedAt') or 0,o.get('variant') or 0))
        if len(ITEMS)>MAX_KEEP:
            for o in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(o['id'],None)
            del ITEMS[:len(ITEMS)-MAX_KEEP]
def load(data_dir):
    _path[0]=os.path.join(data_dir,'gap_latewindow.json')
    try:
        with open(_path[0]) as f: d=json.load(f)
        merge_in(d.get('items'))
    except Exception: pass
def snapshot():
    with _lk: return [dict(o) for o in ITEMS]
def _save(now=None):
    _st['lastFile']=time.time() if now is None else now; _st['fileDirty']=False
    if not _path[0]: return
    try:
        tmp=_path[0]+'.tmp'
        with open(tmp,'w') as f: json.dump(dict(items=snapshot()),f,default=str)
        os.replace(tmp,_path[0])
    except Exception as e: print('latewindow save err',e,flush=True)
def reset():
    """Test helper: clear in-memory state. Does not start or stop the scheduler."""
    with _lk:
        ITEMS.clear(); _by.clear(); WIN.clear(); _try.clear()
        for k in list(STAT): STAT[k]=0
        _st['lastFile']=0.0; _st['fileDirty']=False
# ---------------------------------------------------------------- read-only summary -----------------------------------
def _book(rows):
    fin=[o for o in rows if o.get('status')=='filled' and o.get('settle') is not None and o.get('pnl') is not None]
    n=len(fin); pnl=round(sum(o['pnl'] for o in fin),2)
    wins=sum(1 for o in fin if (o.get('pnl') or 0)>0)
    return dict(n=n,
                open=sum(1 for o in rows if o.get('status')=='filled' and o.get('settle') is None),
                skipped=sum(1 for o in rows if o.get('status')=='skipped'),
                wins=wins,losses=sum(1 for o in fin if (o.get('pnl') or 0)<=0),
                winPct=round(100.0*wins/n,1) if n else None,
                pnl=pnl,
                cPer1=round(100.0*pnl/n,2) if n else None)
def _reasons(rows):
    d={}
    for o in rows:
        if o.get('status')!='skipped': continue
        k=o.get('skipReason') or 'unknown'; d[k]=d.get(k,0)+1
    return d
def summary():
    its=snapshot()
    books={}
    for T in VARIANTS:
        rows=[o for o in its if o.get('variant')==T]
        rec=_book(rows)
        rec.update(name=VNAME[T],targetLeft=T,skipReasons=_reasons(rows),signals=len(rows))
        books[VNAME[T]]=rec
    return dict(enabled=ENABLED,shadowOnly=True,stats=dict(STAT),
                thresholds=dict(edge=EDGE,variants=list(VARIANTS),settleWindow='close-60s .. close-1s (60 ticks)',
                                volLookbackSec=VOL_LOOKBACK,fee='0.07*p*(1-p) ceiled to a cent',
                                coins=list(COINS),hold='hold to settlement',
                                cf='MoogleEdge in-memory 1s CF RTI (no extra CF REST)',
                                quotes='one read per variant per coin per round; WS cache then one public /markets GET'),
                stakeUsd=STAKE,books=books,
                persist=dict(collection='gap_latewindow',ttlDays=TTL_DAYS,
                             note=f'One doc per (ticker, T) variant. Mongo TTL {TTL_DAYS}d on openedAtDt; in-memory cap {MAX_KEEP}.'),
                note='SHADOW ONLY: never places, sizes or blocks a trade. Frozen rule is t60, fair-ask >= 0.10, $1 hold-to-settle. t180/t120/t30 use the same rule at those entry times (before the final 60s the lock is empty). Fills use the real ask; 1-min candle slip from the offline backtest is not applied.')
def api(limit=300,items=True):
    its=snapshot()[-limit:] if items else []
    return dict(summary=summary(),items=its[::-1])
def rows():
    """One dashboard line per variant, in T order."""
    s=summary(); out=[]
    for T in VARIANTS:
        b=s['books'][VNAME[T]]
        wr='—' if b['winPct'] is None else f"{b['winPct']:.1f}%"
        cp='—' if b['cPer1'] is None else f"{b['cPer1']:+.1f}¢/$1"
        out.append(f"{VNAME[T]} n={b['n']} {wr} {b['wins']}-{b['losses']} {b['pnl']:+.2f} {cp} open {b['open']}")
    return out
_bc=[0,None]
def brief():
    if _bc[1] is not None and time.time()-_bc[0]<20: return _bc[1]
    _bc[1]=' · '.join(rows()); _bc[0]=time.time(); return _bc[1]
def brief_rows():
    return rows()
