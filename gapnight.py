"""Gap NIGHT-MODE SHADOW (owner t1093u/t1095u/t1102u, Nov 10 2026). PAPER/SHADOW ONLY: no order path, imports nothing from
live/gaplive, never touches pt.trades, the gap paper book, gap live (cap/used/paused), Spec, MoogleEdge's book,
the limit shadow or the regime shadow books. Night = 5 PM-6 AM ET; day rounds are logged as not-applicable only.
Applied to gap Rule A SHADOW signals on BTC-ETH, and SOL-XRP when XRP is not the laggard, $1 flat.
Each night book tracks three EXIT variants on the same fill (same $1, same bid feed as the gap paper trail):
  trail15 : arm entry+15c / 10c giveback, sell at bid (original night shadow exit; also stored in exit/pnl).
  trail25 : arm entry+25c / 10c giveback, sell at bid (shadow only; stored in exit25/pnl25).
  hold    : hold to settlement (hold_pnl). A trail that never fires also falls through to hold at settle.
VARIANTS (each its own $1 book on every eligible night signal):
  baseline 'unfiltered' : enter every eligible night Rule A signal (comparison book).
  n1       'moogle-65'  : enter only if the live MoogleEdge shadow p for the traded side is >= 0.65 at entry.
                          If MoogleEdge is not warm for that coin/ticker, skip and count reason 'moogle-not-warm'.
  n2       'moogle-65+vol': N1 AND skip when the laggard's previous settled Kalshi volume_fp is below its 6h
                          median (gapregime HIST / volume_ctx, last <=24 settled rounds). Unknown volume does not skip.
Volume is reused from gapregime (no new market polling). Quotes are read-only WS / tick cache."""
import json, math, os, threading, time
from datetime import datetime
try: from paper_tz import ET
except Exception: ET=None
E=os.environ.get
ENABLED=E('GAP_NIGHT','1')!='0'
MOOGLE_BAR=float(E('GAP_NIGHT_MOOGLE_BAR','0.65'))
ARM=0.15; ARM25=0.25; DROP=0.10; STAKE=1.0; FRESH_SEC=15.0; MAX_KEEP=int(E('GAP_NIGHT_MAX_KEEP','3000'))
FILE_SAVE_SEC=60.0; TTL_DAYS=int(E('GAP_NIGHT_TTL_DAYS','120'))
PAIRS=frozenset(('BTC-ETH','SOL-XRP'))
VARIANTS=('baseline','n1','n2'); VNAME={'baseline':'unfiltered','n1':'moogle-65','n2':'moogle-65+vol'}
# Field maps for the two trail exit variants. trail15 keeps the original field names so existing records stay valid.
T15=dict(peak='peakBid',armed='trailArmedAt',exit='exit',exitFee='exitFee',pnl='pnl',
         exitedAt='exitedAt',exitLeftSec='exitLeftSec',exitReason='exitReason',label='trail')
T25=dict(peak='peakBid25',armed='trailArmedAt25',exit='exit25',exitFee='exitFee25',pnl='pnl25',
         exitedAt='exitedAt25',exitLeftSec='exitLeftSec25',exitReason='exitReason25',label='trail25')
QUOTE=[None]   # fn(ticker)->{'yesBid','yesAsk'} read-only WS quote cache
SINK=[None]    # fn(item) non-blocking persistence (server: GP.upsert, lease-gated)
ITEMS=[]; _by={}; _seen=set(); _lk=threading.RLock(); _path=[None]
STAT=dict(signals=0,nightSignals=0,daySignals=0,entries=0,skipped=0,skippedDay=0,skippedMoogleCold=0,
          skippedMoogleBar=0,skippedLowVol=0,skippedXrp=0,trailExits=0,trail25Exits=0,settled=0,errors=0)
_st=dict(lastFile=0.0,fileDirty=False)
def fee(c,p): return math.ceil(7*c*p*(1-p))/100
def _f(x):
    try: return float(x) if x is not None and x!='' else None
    except Exception: return None
def hour_et(ts):
    try: return datetime.fromtimestamp(ts,ET).hour if ET else None
    except Exception: return None
def is_night(ts):
    """True during 5 PM (17:00) through 5:59 AM ET; False 6 AM-4:59 PM; None if the hour is unknown."""
    h=hour_et(ts)
    if h is None: return None
    return h>=17 or h<6
def _quote(tk,side,m):
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
    """P(traded side) from the live MoogleEdge shadow cache (M.CUR). None if missing, stale (>20s) or wrong ticker."""
    try:
        import moogle as M; r=M.CUR.get(coin)
        if not r or r.get('ticker')!=ticker or now-float(r.get('t') or 0)>20 or r.get('p') is None: return None
        p=float(r['p']); return round(p if side=='YES' else 1-p,4)
    except Exception: return None
def volume_ctx(coin):
    """Reuse gapregime's settled-round volume proxy (prev volume_fp vs median of last <=24 rounds = 6h)."""
    try:
        import gapregime as R
        return dict(R.volume_ctx(coin) or {})
    except Exception:
        return dict(prevVol=None,medianVol=None,ratio=None,bucket='unknown')
def _low_vol(vol):
    if not vol: return False
    ratio=vol.get('ratio'); prev,med=vol.get('prevVol'),vol.get('medianVol')
    if ratio is not None: return ratio<1.0-1e-12
    return prev is not None and med is not None and prev<med-1e-9
def _entry(o,price):
    c0=STAKE/price; ef=fee(c0,price)
    o.update(status='filled',entry=round(price,4),contracts=round(max(STAKE-ef,0.001)/price,4),entryFee=ef,peakBid=None,peakBid25=None)
def _bump_skip(reason):
    STAT['skipped']+=1
    if reason=='day-not-applicable': STAT['skippedDay']+=1
    elif reason=='moogle-not-warm': STAT['skippedMoogleCold']+=1
    elif reason and reason.startswith('moogle-below-bar'): STAT['skippedMoogleBar']+=1
    elif reason and reason.startswith('low-volume'): STAT['skippedLowVol']+=1
    elif reason=='xrp-laggard': STAT['skippedXrp']+=1
def _reason(v,night,pair,lag,mp,vol):
    if night is None: return 'hour-unknown'
    if pair=='SOL-XRP' and lag=='XRP': return 'xrp-laggard'
    if not night: return 'day-not-applicable'
    if v=='baseline': return None
    if mp is None: return 'moogle-not-warm'
    if mp<MOOGLE_BAR-1e-9: return f'moogle-below-bar: {mp} < {MOOGLE_BAR}'
    if v=='n2' and _low_vol(vol):
        return f"low-volume: prev {vol.get('prevVol')} < median {vol.get('medianVol')}"
    return None
def make_items(st,m,now):
    try:
        import gapshadow as G; sk=G.doc_id(st)
    except Exception:
        sk=f"shadow|{st.get('rule')}|{st.get('pair')}|{float(st.get('closesAt') or 0):.3f}"
    if sk in _seen: return []
    _seen.add(sk); STAT['signals']+=1
    pair=st.get('pair'); lag=st.get('laggard'); side=st['side']
    night=is_night(now); hour=hour_et(now)
    session='night' if night else ('day' if night is False else 'unknown')
    if night: STAT['nightSignals']+=1
    elif night is False: STAT['daySignals']+=1
    mp=moogle_side_p(lag,st['ticker'],side,now)
    vol=volume_ctx(lag)
    tags=dict(hourET=hour,session=session,moogleP=mp,moogleWarm=mp is not None,moogleBar=MOOGLE_BAR,
              volume=vol,lowVol=_low_vol(vol))
    out=[]
    for v in VARIANTS:
        o=dict(id=f'{sk}|{v}',shadowKey=sk,variant=v,variantName=VNAME[v],mode='shadow',book='gap-night',rule='A',
               pair=pair,laggard=lag,leader=st.get('leader'),ticker=st['ticker'],side=side,signalSide=side,
               signalAsk=float(st['entry']),closesAt=st['closesAt'],openedAt=now,leftSec=round(st['closesAt']-now,1),
               session=session,status=None,skipReason=None,entry=None,contracts=None,entryFee=None,exit=None,exitFee=None,
               pnl=None,hold_pnl=None,exit25=None,exitFee25=None,pnl25=None,settle=None,settledAt=None,gapDown=st.get('gapDown'),
               leaderYesMid=st.get('leaderYesMid'),laggardYesMid=st.get('laggardYesMid'),tags=tags)
        why=_reason(v,night,pair,lag,mp,vol)
        if why is None: _entry(o,float(st['entry'])); STAT['entries']+=1
        else: o.update(status='skipped',skipReason=why); _bump_skip(why)
        out.append(o)
    with _lk:
        for o in out: ITEMS.append(o); _by[o['id']]=o
        if len(ITEMS)>MAX_KEEP:
            for o in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(o['id'],None)
            del ITEMS[:len(ITEMS)-MAX_KEEP]
    return out
def _tick_trail(o,bid,now,arm,f):
    """Advance one trail exit (trail15 or trail25) on the current bid. Sell at bid, same fee as gap paper trail.
    trail25 seeds peakBid25 from an existing peakBid once so pre-deploy peaks are not lost. Returns True if the
    item changed (armed or exited)."""
    if o.get(f['exit']) is not None: return False
    if f['peak']=='peakBid25' and o.get('peakBid25') is None and o.get('peakBid') is not None:
        o['peakBid25']=o['peakBid']
    o[f['peak']]=round(max(o.get(f['peak']) or 0,bid),4)
    ch=False
    if o[f['peak']]>=o['entry']+arm-1e-9:
        if not o.get(f['armed']): o[f['armed']]=now; ch=True
        if bid<=o[f['peak']]-DROP+1e-9:
            xf=fee(o['contracts'],bid)
            o[f['exit']]=round(bid,4); o[f['exitFee']]=xf
            o[f['pnl']]=round(o['contracts']*bid-xf-STAKE,2)
            o[f['exitedAt']]=now; o[f['exitLeftSec']]=round(o['closesAt']-now,1)
            o[f['exitReason']]=f"{f['label']}: peak {o[f['peak']]:.2f} (armed at {o['entry']:.2f}+{arm:.2f}) -> bid {bid:.2f}"
            ch=True
    return ch
def on_tick(mkts,now=None,signals=None):
    """Pure paper bookkeeping on Rule A shadow signals (gapshadow.TRADES); trail15 + trail25 exits for open night items."""
    if not ENABLED: return []
    now=time.time() if now is None else now; ch=[]
    mkts={k:v for k,v in (mkts or {}).items() if isinstance(v,dict)}
    with _lk: cur=[o for o in ITEMS if o['status']=='filled' and o.get('session')=='night' and now<o['closesAt']
                   and (o.get('exit') is None or o.get('exit25') is None)]
    for o in cur:
        try:
            _,bid,_=_quote(o['ticker'],o['side'],mkts.get(o.get('laggard')))
            if bid is None or bid<=0: continue
            had15,had25=o.get('exit'),o.get('exit25')
            if _tick_trail(o,bid,now,ARM,T15):
                if o.get('exit') is not None and had15 is None: STAT['trailExits']+=1
                ch.append(o)
            if _tick_trail(o,bid,now,ARM25,T25):
                if o.get('exit25') is not None and had25 is None: STAT['trail25Exits']+=1
                ch.append(o)
        except Exception as e: STAT['errors']+=1; print('gapnight exit err',e,flush=True)
    for st in signals or []:
        try:
            if st.get('rule')!='A' or st.get('mode')!='shadow' or now>=st['closesAt']: continue
            if st.get('pair') not in PAIRS: continue
            if now-float(st.get('openedAt') or 0)>FRESH_SEC: continue
            n=make_items(st,mkts.get(st.get('laggard')),now)
            if n:
                ch.extend(n)
                print('GAPNIGHT',st.get('pair'),st.get('laggard'),st.get('side'),st.get('entry'),n[0]['session'],
                      [(o['variant'],'fill' if o['status']=='filled' else o.get('skipReason')) for o in n],flush=True)
        except Exception as e: STAT['errors']+=1; print('gapnight signal err',e,flush=True)
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
                if o.get('exit25') is None: o['pnl25']=o['hold_pnl']; o['heldToSettle25']=True
            STAT['settled']+=1; ch.append(o)
        except Exception as e: STAT['errors']+=1; print('gapnight settle err',e,flush=True)
    if ch: _changed(ch,now)
    return bool(ch)
# ------------------------------------------------------------ persistence ----------------------------------------------
def _changed(items,now):
    if SINK[0] is not None:
        for o in items:
            try: SINK[0](json.loads(json.dumps(o,default=str)))
            except Exception as e: print('gapnight sink err',e,flush=True)
    _st['fileDirty']=True
    if now-_st['lastFile']>=FILE_SAVE_SEC or any(o.get('settle') for o in items): _save(now)
def _rank(o): return (o.get('settle') is not None,o.get('exit') is not None,o.get('exit25') is not None,o.get('peakBid25') or o.get('peakBid') or 0)
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
def load(data_dir):
    _path[0]=os.path.join(data_dir,'gap_night.json')
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
    except Exception as e: print('gapnight save err',e,flush=True)
# ------------------------------------------------------------ read-only summary ----------------------------------------
def _pnl_book(fin,key):
    return dict(wins=sum(1 for o in fin if (o.get(key) or 0)>0),
                losses=sum(1 for o in fin if (o.get(key) or 0)<=0),
                pnl=round(sum(o.get(key) or 0 for o in fin),2))
def _agg(rows):
    fin=[o for o in rows if o.get('status')=='filled' and o.get('settle') is not None]
    t15=_pnl_book(fin,'pnl'); hold=_pnl_book(fin,'hold_pnl')
    return dict(n=len(rows),settled=len(fin),wins=t15['wins'],losses=t15['losses'],
                pnl=t15['pnl'],holdPnl=hold['pnl'],
                open=sum(1 for o in rows if o.get('status')=='filled' and o.get('settle') is None))
def _exits(rows):
    fin=[o for o in rows if o.get('status')=='filled' and o.get('settle') is not None]
    return dict(trail15=_pnl_book(fin,'pnl'),trail25=_pnl_book(fin,'pnl25'),hold=_pnl_book(fin,'hold_pnl'))
def _reasons(rows):
    d={}
    for o in rows:
        if o.get('status')!='skipped': continue
        k=o.get('skipReason') or 'unknown'; d[k]=d.get(k,0)+1
    return d
def session_now(now=None):
    now=time.time() if now is None else now
    night=is_night(now); hour=hour_et(now)
    return dict(isNight=bool(night),hourET=hour,window='5 PM-6 AM ET',
                label='night' if night else ('day' if night is False else 'unknown'),now=round(now,1))
def summary():
    its=snapshot()
    night=[o for o in its if o.get('session')=='night']
    day=[o for o in its if o.get('session')=='day']
    books={}
    for v in VARIANTS:
        rows=[o for o in night if o['variant']==v]
        ent=[o for o in rows if o['status']=='filled']; sk=[o for o in rows if o['status']=='skipped']
        books[v]=dict(name=VNAME[v],entries=_agg(ent),exits=_exits(ent),skipped=len(sk),skipReasons=_reasons(rows))
    return dict(enabled=ENABLED,shadowOnly=True,stats=dict(STAT),session=session_now(),
                thresholds=dict(moogleBar=MOOGLE_BAR,nightWindow='5 PM (17:00) through 5:59 AM ET',
                                volumeSkip='n2 only: previous settled round Kalshi volume_fp < 6h median (gapregime volume_ctx, last <=24 rounds)',
                                pairs='BTC-ETH, and SOL-XRP when XRP is not the laggard',
                                trail15='arm entry+15c / 10c giveback, sell at bid',
                                trail25='arm entry+25c / 10c giveback, sell at bid',
                                hold='hold to settlement'),
                exit='trail15 arm entry+15c / 10c giveback; trail25 arm entry+25c / 10c giveback; hold = hold to settlement. W-L/PnL reported per exit. Sell at bid (same quote feed as gap paper trail).',
                stakeUsd=STAKE,books=books,night=_agg([o for o in night if o['status']=='filled']),
                nightExits=_exits([o for o in night if o['status']=='filled']),
                dayLogged=len(day),daySkipReasons=_reasons(day),
                persist=dict(collection='gap_night',ttlDays=TTL_DAYS,
                             note=f'3 docs per Rule A signal (baseline/n1/n2); trail15/trail25/hold stored on the same doc. Mongo TTL {TTL_DAYS}d on openedAtDt; in-memory cap {MAX_KEEP}. Night-only entries, day logged as not-applicable. Volume reused from gapregime HIST (no extra polling).'),
                note='SHADOW ONLY: never places, sizes or blocks a trade. baseline = unfiltered night Rule A; n1 = MoogleEdge side p>=0.65; n2 = n1 + skip previous-round volume below 6h median. Each book reports trail15, trail25 and hold W-L/PnL.')
def api(limit=300,items=True):
    its=snapshot()[-limit:] if items else []
    return dict(summary=summary(),items=its[::-1])
_bc=[0,None]
def brief():
    if _bc[1] is not None and time.time()-_bc[0]<20: return _bc[1]
    s=summary(); sess=s['session']['label']; b=s['books']
    def _ex(v,k):
        e=v['exits'][k]; return f"{e['wins']}-{e['losses']} {e['pnl']:+.2f}"
    _bc[1]=(f"{sess} · "+' · '.join(
        f"{VNAME[k]} t15 {_ex(v,'trail15')} t25 {_ex(v,'trail25')} hold {_ex(v,'hold')}" for k,v in b.items())
            +f" · day logged {s['dayLogged']}")
    _bc[0]=time.time(); return _bc[1]
