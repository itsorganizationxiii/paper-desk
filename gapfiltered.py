"""Gap FILTERED paper book (owner t1155u, Nov 10 2026). PAPER/SHADOW ONLY: no order path, imports nothing from
live/gaplive, never touches pt.trades, the $3 gap paper book, gap live (cap/used/paused), Spec, MoogleEdge,
night/regime/limit/late-window shadows or the live toggle.
Mirrors gap PAPER Rule A signals (same real Kalshi ask entry as /api/gappaper) at $1 flat, but SKIPS
  (1) any trade on the SOL-XRP pair (SOL or XRP asset)
  (2) any entry price < 0.55
Each fill is scored with the same exits as the gap paper book: trail15 (arm entry+15c / 10c giveback, sell at
bid on the same quote feed) and hold-to-settlement (hold_pnl). A trail that never fires falls through to hold.
Persisted per item to Mongo gap_filtered via gappersist (lease-gated) + file, 120-day TTL on openedAtDt."""
import json, math, os, threading, time
E=os.environ.get
ENABLED=E('GAP_FILTERED','1')!='0'
STAKE=1.0; ARM=0.15; DROP=0.10; MIN_ENTRY=float(E('GAP_FILTERED_MIN_ENTRY','0.55'))
FRESH_SEC=15.0; MAX_KEEP=int(E('GAP_FILTERED_MAX_KEEP','3000'))
FILE_SAVE_SEC=60.0; TTL_DAYS=int(E('GAP_FILTERED_TTL_DAYS','120'))
SOL_XRP_ASSETS=frozenset(('SOL','XRP'))
QUOTE=[None]   # fn(ticker)->{'yesBid','yesAsk'} read-only WS quote cache
SINK=[None]    # fn(item) non-blocking persistence (server: GP.upsert, lease-gated)
ITEMS=[]; _by={}; _seen=set(); _lk=threading.RLock(); _path=[None]
STAT=dict(signals=0,entries=0,skipped=0,skippedSolXrp=0,skippedCheap=0,trailExits=0,settled=0,errors=0)
_st=dict(lastFile=0.0,fileDirty=False)
def fee(c,p): return math.ceil(7*c*p*(1-p))/100
def _f(x):
    try: return float(x) if x is not None and x!='' else None
    except Exception: return None
def paper_key(st):
    try:
        import gapshadow as G; return G.doc_id(st)
    except Exception:
        return f"paper|{st.get('rule')}|{st.get('pair')}|{float(st.get('closesAt') or 0):.3f}"
def skip_reason(st):
    """None if the paper signal should enter the filtered book; else a skip reason."""
    pair=(st.get('pair') or '').upper()
    asset=(st.get('asset') or st.get('laggard') or '').upper()
    if pair=='SOL-XRP' or asset in SOL_XRP_ASSETS: return 'sol-xrp'
    price=_f(st.get('price') if st.get('price') is not None else st.get('entry'))
    if price is None: return 'no-entry'
    if price<MIN_ENTRY-1e-12: return 'cheap-entry'
    return None
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
def _entry(o,price):
    c0=STAKE/price; ef=fee(c0,price)
    o.update(status='filled',entry=round(price,4),price=round(price,4),stake=STAKE,
             contracts=round(max(STAKE-ef,0.001)/price,4),entryFee=ef,peakBid=None)
def _bump_skip(reason):
    STAT['skipped']+=1
    if reason=='sol-xrp': STAT['skippedSolXrp']+=1
    elif reason=='cheap-entry': STAT['skippedCheap']+=1
def make_item(st,now):
    pk=paper_key(st)
    if pk in _seen: return None
    _seen.add(pk); STAT['signals']+=1
    price=_f(st.get('price') if st.get('price') is not None else st.get('entry'))
    o=dict(id=f'{pk}|filt',paperKey=pk,mode='shadow',book='gap-filtered',rule=st.get('rule') or 'A',
           pair=st.get('pair'),laggard=st.get('asset') or st.get('laggard'),asset=st.get('asset') or st.get('laggard'),
           leader=st.get('leader'),ticker=st['ticker'],side=st['side'],signalAsk=_f(st.get('signalAsk')),
           closesAt=st['closesAt'],openedAt=now,leftSec=round(float(st['closesAt'])-now,1),
           status=None,skipReason=None,entry=None,price=None,stake=STAKE,contracts=None,entryFee=None,
           exit=None,exitFee=None,pnl=None,hold_pnl=None,settle=None,settledAt=None,
           gapDown=st.get('gapDown'),conf=st.get('conf'),paperStake=st.get('stake'))
    why=skip_reason(st)
    if why is None and price is not None:
        _entry(o,price); STAT['entries']+=1
    else:
        o.update(status='skipped',skipReason=why or 'no-entry',entry=None if price is None else round(price,4))
        _bump_skip(o['skipReason'])
    with _lk:
        ITEMS.append(o); _by[o['id']]=o
        if len(ITEMS)>MAX_KEEP:
            for old in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(old['id'],None)
            del ITEMS[:len(ITEMS)-MAX_KEEP]
    return o
def on_tick(mkts,now=None,signals=None):
    """Paper bookkeeping on gap PAPER Rule A fills; trail15 exit for open filtered fills. Never mutates signals."""
    if not ENABLED: return []
    now=time.time() if now is None else now; ch=[]
    mkts={k:v for k,v in (mkts or {}).items() if isinstance(v,dict)}
    with _lk: cur=[o for o in ITEMS if o.get('status')=='filled' and o.get('exit') is None and now<o['closesAt']]
    for o in cur:
        try:
            _,bid,src=_quote(o['ticker'],o['side'],mkts.get(o.get('asset') or o.get('laggard')))
            if bid is None or bid<=0: continue
            o['peakBid']=round(max(o.get('peakBid') or 0,bid),4)
            if o['peakBid']>=o['entry']+ARM-1e-9:
                if not o.get('trailArmedAt'): o['trailArmedAt']=now; ch.append(o)
                if bid<=o['peakBid']-DROP+1e-9:
                    xf=fee(o['contracts'],bid); pnl=round(o['contracts']*bid-xf-STAKE,2)
                    o.update(status='exited',exit=round(bid,4),exitFee=xf,pnl=pnl,exitedAt=now,
                             exitLeftSec=round(o['closesAt']-now,1),exitQuote=src,
                             exitReason=f"trail: peak {o['peakBid']:.2f} (armed at entry {o['entry']:.2f}+{ARM:.2f}) -> bid {bid:.2f}")
                    STAT['trailExits']+=1; ch.append(o)
        except Exception as e: STAT['errors']+=1; print('gapfiltered exit err',e,flush=True)
    for st in signals or []:
        try:
            if not st.get('gap') or st.get('mode')!='paper' or st.get('rule')!='A': continue
            if now>=float(st.get('closesAt') or 0): continue
            if now-float(st.get('openedAt') or 0)>FRESH_SEC: continue
            o=make_item(st,now)
            if o:
                ch.append(o)
                print('GAPFILTERED',o.get('pair'),o.get('asset'),o.get('side'),o.get('entry') or st.get('price'),
                      'fill' if o['status']=='filled' else o.get('skipReason'),flush=True)
        except Exception as e: STAT['errors']+=1; print('gapfiltered signal err',e,flush=True)
    if ch: _changed(ch,now)
    return ch
_try={}
def settle(result_fn,now=None):
    now=time.time() if now is None else now; res={}; ch=[]
    with _lk: todo=[o for o in ITEMS if o.get('status') in ('filled','exited') and o.get('settle') is None and now>=o['closesAt']+5]
    if not todo: return False
    try:
        import gapshadow as G
        sh={G.doc_id(t):t.get('settle') for t in G.PAPER[-400:] if t.get('settle') in ('yes','no')}
    except Exception: sh={}
    for o in todo:
        try:
            tk=o['ticker']; r=sh.get(o.get('paperKey'))
            if r is None:
                if tk not in res:
                    if len(res)>=3 or now-_try.get(tk,0)<30 or result_fn is None: continue
                    _try[tk]=now
                    try: res[tk]=result_fn(tk)
                    except Exception: res[tk]=None
                r=res[tk]
            if r not in ('yes','no'): continue
            win=(r=='yes')==(o['side']=='YES'); hp=round((o['contracts'] if win else 0)-STAKE,2)
            o['settle']=r; o['hold_pnl']=hp; o['settledAt']=now
            if o.get('exit') is None: o['pnl']=hp; o['heldToSettle']=True
            STAT['settled']+=1; ch.append(o)
        except Exception as e: STAT['errors']+=1; print('gapfiltered settle err',e,flush=True)
    if ch: _changed(ch,now)
    return bool(ch)
def _changed(items,now):
    if SINK[0] is not None:
        for o in items:
            try: SINK[0](json.loads(json.dumps(o,default=str)))
            except Exception as e: print('gapfiltered sink err',e,flush=True)
    _st['fileDirty']=True
    if now-_st['lastFile']>=FILE_SAVE_SEC or any(o.get('settle') for o in items): _save(now)
def _rank(o): return (o.get('settle') is not None,o.get('exit') is not None,o.get('hold_pnl') is not None,o.get('peakBid') or 0)
def merge_in(items):
    with _lk:
        for o in items or []:
            try:
                if not isinstance(o,dict) or not o.get('id') or not o.get('paperKey'): continue
                o={k:v for k,v in o.items() if k!='_id'}; x=_by.get(o['id'])
                if x is None: ITEMS.append(o); _by[o['id']]=o
                elif _rank(o)>_rank(x): x.clear(); x.update(o)
            except Exception: pass
        ITEMS.sort(key=lambda o:o.get('openedAt') or 0)
        if len(ITEMS)>MAX_KEEP:
            for old in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(old['id'],None)
            del ITEMS[:len(ITEMS)-MAX_KEEP]
        _seen.clear(); _seen.update(o['paperKey'] for o in ITEMS)
def load(data_dir):
    _path[0]=os.path.join(data_dir,'gap_filtered.json')
    try:
        with open(_path[0]) as f: d=json.load(f)
        merge_in(d if isinstance(d,list) else d.get('items'))
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
    except Exception as e: print('gapfiltered save err',e,flush=True)
def reset():
    """Test helper: clear in-memory book state. Never called from the server."""
    global ITEMS,_by,_seen,_try
    with _lk:
        ITEMS=[]; _by={}; _seen=set()
        for k in list(STAT): STAT[k]=0
        _st.update(lastFile=0.0,fileDirty=False); _path[0]=None
    _try={}; QUOTE[0]=None; SINK[0]=None
    try:
        _bc[0]=0; _bc[1]=None
    except Exception:
        pass
def _pnl_book(fin,key):
    rows=[o for o in fin if o.get(key) is not None]
    return dict(n=len(rows),
                wins=sum(1 for o in rows if (o.get(key) or 0)>0),
                losses=sum(1 for o in rows if (o.get(key) or 0)<=0),
                pnl=round(sum((o.get(key) or 0) for o in rows),2))
def _reasons(rows):
    d={}
    for o in rows:
        if o.get('status')!='skipped': continue
        k=o.get('skipReason') or 'unknown'; d[k]=d.get(k,0)+1
    return d
def summary():
    its=snapshot()
    ent=[o for o in its if o.get('status') in ('filled','exited')]
    fin=[o for o in ent if o.get('settle') is not None]
    trail=_pnl_book(fin,'pnl'); hold=_pnl_book(fin,'hold_pnl')
    sk=[o for o in its if o.get('status')=='skipped']
    return dict(enabled=ENABLED,shadowOnly=True,book='gap-filtered',stakeUsd=STAKE,minEntry=MIN_ENTRY,
                filters=['skip SOL-XRP (SOL or XRP asset)','skip entry < 0.55'],
                exit='trail15 arm entry+15c / 10c giveback, sell at bid (same quote feed as gap paper trail); hold = hold to settlement',
                n=trail['n'],wins=trail['wins'],losses=trail['losses'],pnl=trail['pnl'],holdPnl=hold['pnl'],
                trail=trail,hold=hold,
                open=sum(1 for o in ent if o.get('settle') is None),
                skipped=len(sk),skipReasons=_reasons(its),stats=dict(STAT),
                persist=dict(collection='gap_filtered',ttlDays=TTL_DAYS,
                             note=f'One doc per gap paper Rule A signal. Mongo TTL {TTL_DAYS}d on openedAtDt; in-memory cap {MAX_KEEP}.'),
                note='PAPER ONLY: mirrors gap paper Rule A signals at $1, skipping SOL-XRP and entries under 55c. Never places, sizes or blocks a live or gap-paper trade.')
def api(limit=300,items=True):
    its=snapshot()[-limit:] if items else []
    return dict(summary=summary(),items=its[::-1])
_bc=[0,None]
def brief():
    if _bc[1] is not None and time.time()-_bc[0]<20: return _bc[1]
    s=summary(); t,h=s['trail'],s['hold']
    _bc[1]=f"n={t['n']} {t['wins']}-{t['losses']} trail {t['pnl']:+.2f} / hold {h['pnl']:+.2f}"
    if s['skipped']: _bc[1]+=f" · skipped {s['skipped']}"
    _bc[0]=time.time(); return _bc[1]
