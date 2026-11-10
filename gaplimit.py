"""Gap Rule A LIMIT-ORDER SHADOW (owner t1062u/t1064u, Nov 9 2026). PAPER/SHADOW ONLY: this module has NO order path,
imports nothing from live/gaplive, never touches pt.trades, the gap paper book, the live cap/used counter or paused state.
Idea: the gap paper entry takes the market ask when the signal fires and the laggard often moves first. Here, every Rule A
signal on the gap paper pairs places SIMULATED resting limit buys on the laggard at signalAsk - N cents (GAP_LIMIT_OFFSETS,
default 2c/4c/6c) plus an offset-0 'market' reference (fills at the signal ask at once, $1, same exit) for apples-to-apples.
  fill   : conservative - only when OUR-SIDE ASK (YES: yesAsk, NO: 1-yesBid) <= limit (never on mid/bid); filled at the limit.
           Checked each tick (fresh WS quote first, else the tick's REST market); checks start on the tick AFTER the signal.
  expire : leftSec <= CUTOFF (default 180s), or the signal disappears (leader back across 50%, laggard no longer cheaper in
           the leader's direction, or |gap| < Rule A min gap). A touch on the same tick wins over expiry (adverse-fill bias).
  exit   : same as gap paper: trail arm entry+15c / 10c giveback at our bid (minus Kalshi fee), else hold; hold_pnl always.
  fee    : taker formula ceil(7*c*p*(1-p))/100 on entry and exit (conservative; real resting orders pay the lower maker fee).
Unfilled orders record what the offset-0 market entry made on the same signal. The real gap paper twin (by shadowKey) is
linked for comparison (its P&L per $ staked, since strong-tier twins are $3)."""
import json, math, os, statistics, threading, time
E=os.environ.get
ENABLED=E('GAP_LIMIT','1')!='0'
def _offs(v):
    out=[]
    for x in (v or '').split(','):
        try:
            c=round(float(x.strip()),4)
            if 0<=c<0.5 and c not in out: out.append(c)
        except Exception: pass
    return sorted(out)
OFFSETS=_offs(E('GAP_LIMIT_OFFSETS','0.02,0.04,0.06'))
VARIANTS=[0.0]+[o for o in OFFSETS if o>0]
CUTOFF=float(E('GAP_LIMIT_CUTOFF_LEFT','180')); STAKE=1.0
ARM=float(E('GAP_LIMIT_ARM','0.15')); DROP=float(E('GAP_LIMIT_DROP','0.10'))
FRESH_SEC=float(E('GAP_LIMIT_FRESH_SEC','15'))
MAX_KEEP=int(E('GAP_LIMIT_MAX_KEEP','4000'))
ORDERS=[]; _seen=set(); _lk=threading.Lock(); _path=[None]
QUOTE=[None]    # fn(ticker)->{'yesBid','yesAsk'} read-only quote cache
SINK=[None]     # fn() non-blocking persistence hook (server: GP snapshot)
STAT=dict(signals=0,placed=0,filled=0,expired=0,trailExits=0,settled=0,errors=0)
def fee(c,p): return math.ceil(7*c*p*(1-p))/100
def _f(x):
    try: return float(x) if x is not None else None
    except Exception: return None
def _mid(m):
    if not m: return None
    b,a=_f(m.get('yesBid')),_f(m.get('yesAsk'))
    if b and a: return (b+a)/2
    return a or b or None
def vname(off): return 'mkt' if off<=0 else f'lim{round(off*100)}c'
def oid(sk,off): return f'{sk}|{vname(off)}'
def _quote(o,m):
    """(ourAsk, ourBid, src) for the order's ticker: fresh WS quote first, else this tick's market."""
    q=None
    if QUOTE[0] is not None:
        try: q=QUOTE[0](o['ticker'])
        except Exception: q=None
    src='ws'
    if not (q and q.get('yesBid') is not None and q.get('yesAsk') is not None):
        q=m if (m and m.get('ticker')==o['ticker']) else None; src='tick'
    if not q: return None,None,None
    yb,ya=_f(q.get('yesBid')),_f(q.get('yesAsk'))
    if o['side']=='YES': return ya,yb,src
    return (None if yb is None else round(1-yb,4)),(None if ya is None else round(1-ya,4)),src
def _contracts(price): c0=STAKE/price; ef=fee(c0,price); return round(max(STAKE-ef,0.001)/price,4),ef
def _fill(o,price,now,src):
    c,ef=_contracts(price)
    o.update(status='filled',fillPrice=round(price,4),filledAt=now,timeToFill=round(now-o['placedAt'],1),fillLeftSec=round(o['closesAt']-now,1),
             fillQuote=src,contracts=c,entryFee=ef,peakBid=None)
    STAT['filled']+=1
def make_orders(st,now=None):
    """Create simulated orders (one per variant) for a Rule A shadow signal dict (gapshadow trade). Returns new orders."""
    now=st.get('openedAt') if now is None else now
    try:
        import gapshadow as G; sk=G.doc_id(st)
    except Exception: sk=f"shadow|{st.get('rule')}|{st.get('pair')}|{float(st.get('closesAt') or 0):.3f}"
    if sk in _seen: return []
    _seen.add(sk); STAT['signals']+=1; out=[]
    ask=float(st['entry'])
    for off in VARIANTS:
        lim=round(ask-off,4)
        o=dict(id=oid(sk,off),shadowKey=sk,variant=vname(off),offset=off,mode='shadow',book='gap-limit',rule=st.get('rule'),pair=st.get('pair'),
               laggard=st.get('laggard'),leader=st.get('leader'),ticker=st['ticker'],side=st['side'],signalAsk=ask,limit=lim,stake=STAKE,
               closesAt=st['closesAt'],placedAt=now,placedLeftSec=round(st['closesAt']-now,1),status='resting',gapDown=st.get('gapDown'),
               conf=None,fillPrice=None,filledAt=None,timeToFill=None,contracts=None,entryFee=None,exit=None,exitFee=None,pnl=None,hold_pnl=None,
               settle=None,expireReason=None,expiredAt=None)
        if off<=0: _fill(o,ask,now,'signal')
        elif not (0.01<=lim<=0.99): o.update(status='expired',expireReason='limit out of range',expiredAt=now); STAT['expired']+=1
        out.append(o)
    with _lk:
        ORDERS.extend(out)
        if len(ORDERS)>MAX_KEEP: del ORDERS[:len(ORDERS)-MAX_KEEP]
    STAT['placed']+=len(out); return out
def signal_alive(o,mkts):
    """Rule A gap still present in the order's direction (same window)."""
    ml,mg=mkts.get(o.get('leader')),mkts.get(o.get('laggard'))
    if not ml or not mg or ml.get('close')!=o['closesAt'] or mg.get('ticker')!=o['ticker']: return None   # unknown this tick
    yl,yg=_mid(ml),_mid(mg)
    if yl is None or yg is None: return None
    try: import gapshadow as G; mg_=G.A_MIN_GAP
    except Exception: mg_=0.03
    if o['side']=='YES': return yl>0.5 and yg<yl and (yl-yg)>=mg_-1e-9
    return yl<0.5 and yg>yl and (yg-yl)>=mg_-1e-9
def on_tick(mkts,now=None,signals=None):
    """signals: Rule A shadow trades (gapshadow.TRADES); new ones on PAIRS get orders. Pure paper bookkeeping."""
    if not ENABLED: return []
    now=time.time() if now is None else now; ch=[]
    mkts={k:v for k,v in (mkts or {}).items() if isinstance(v,dict)}
    try:
        import gapshadow as G; pairs=G.PAPER_PAIRS
    except Exception: pairs={'BTC-ETH','SOL-XRP'}
    with _lk: cur=[o for o in ORDERS if o['status'] in ('resting','filled') and o.get('exit') is None and now<o['closesAt']]
    for o in cur:
        try:
            m=mkts.get(o.get('laggard'))
            if o['status']=='resting':
                if now<=o['placedAt']+1e-6: continue
                ask,_,src=_quote(o,m)
                if ask is not None and ask>0 and ask<=o['limit']+1e-9: _fill(o,o['limit'],now,src); ch.append(o); continue
                left=o['closesAt']-now; why=None
                if left<=CUTOFF: why=f'cutoff ({round(CUTOFF)}s left)'
                elif signal_alive(o,mkts) is False: why='signal gone'
                if why: o.update(status='expired',expireReason=why,expiredAt=now,expireLeftSec=round(left,1),lastAsk=ask); STAT['expired']+=1; ch.append(o)
            else:
                _,bid,src=_quote(o,m)
                if bid is None or bid<=0: continue
                o['peakBid']=round(max(o.get('peakBid') or 0,bid),4)
                if o['peakBid']>=o['fillPrice']+ARM-1e-9:
                    if not o.get('trailArmedAt'): o['trailArmedAt']=now; ch.append(o)
                    if bid<=o['peakBid']-DROP+1e-9:
                        xf=fee(o['contracts'],bid)
                        o.update(exit=round(bid,4),exitFee=xf,pnl=round(o['contracts']*bid-xf-STAKE,2),exitedAt=now,exitLeftSec=round(o['closesAt']-now,1),exitQuote=src,
                                 exitReason=f"trail: peak {o['peakBid']:.2f} (armed at {o['fillPrice']:.2f}+{ARM:.2f}) -> bid {bid:.2f}")
                        STAT['trailExits']+=1; ch.append(o)
        except Exception as e: STAT['errors']+=1; print('gaplimit order err',e,flush=True)
    for st in signals or []:
        try:
            if st.get('rule')!='A' or st.get('pair') not in pairs or now>=st['closesAt'] or st.get('mode')!='shadow': continue
            if now-float(st.get('openedAt') or 0)>FRESH_SEC: continue   # only signals seen live this tick (not old ones reloaded at boot)
            n=make_orders(st)
            if n: ch.extend(n); print('GAPLIMIT placed',st.get('pair'),st.get('laggard'),st.get('side'),st.get('entry'),[o['limit'] for o in n],flush=True)
        except Exception as e: STAT['errors']+=1; print('gaplimit signal err',e,flush=True)
    if ch: _save()
    return ch
_try={}
def settle(result_fn,now=None):
    """Settle from the matching gap shadow trade's Kalshi result (no network); fallback result_fn(ticker) at most once
    per ticker per 30s, max 3 tickers per call."""
    now=time.time() if now is None else now; ch=False; res={}
    with _lk: todo=[o for o in ORDERS if o.get('settle') is None and now>=o['closesAt']+5]
    if not todo: return False
    try:
        import gapshadow as G; sh={G.doc_id(t):t.get('settle') for t in G.snapshot() if t.get('settle') in ('yes','no')}
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
            win=(r=='yes')==(o['side']=='YES'); o['settle']=r; o['settledAt']=now
            if o['status']=='filled':
                hp=round((o['contracts'] if win else 0)-STAKE,2); o['hold_pnl']=hp
                if o.get('exit') is None: o['pnl']=hp; o['heldToSettle']=True
            elif o['status']=='resting': o.update(status='expired',expireReason='never touched (window closed)',expiredAt=o['closesAt']); STAT['expired']+=1
            STAT['settled']+=1; ch=True
        except Exception as e: STAT['errors']+=1; print('gaplimit settle err',e,flush=True)
    if ch: _save()
    return ch
# ---------- persistence ----------
def _rank(o): return (o.get('settle') is not None, o.get('exit') is not None, o.get('status')!='resting')
def merge_in(items):
    with _lk:
        cur={o['id']:o for o in ORDERS}
        for o in items or []:
            try:
                if not isinstance(o,dict) or not o.get('id') or not o.get('shadowKey'): continue
                o={k:v for k,v in o.items() if k!='_id'}; x=cur.get(o['id'])
                if x is None: ORDERS.append(o); cur[o['id']]=o
                elif _rank(o)>_rank(x): x.clear(); x.update(o)
            except Exception: pass
        ORDERS.sort(key=lambda o:(o.get('placedAt') or 0,o.get('offset') or 0))
        if len(ORDERS)>MAX_KEEP: del ORDERS[:len(ORDERS)-MAX_KEEP]
        _seen.clear(); _seen.update(o['shadowKey'] for o in ORDERS)
def load(data_dir,extra=None):
    _path[0]=os.path.join(data_dir,'gap_limit.json')
    try:
        with open(_path[0]) as f: merge_in(json.load(f))
    except Exception: pass
    if extra: merge_in(extra)
def snapshot():
    with _lk: return [dict(o) for o in ORDERS]
def _save():
    if _path[0]:
        try:
            tmp=_path[0]+'.tmp'
            with open(tmp,'w') as f: json.dump(snapshot(),f,default=str)
            os.replace(tmp,_path[0])
        except Exception as e: print('gaplimit save err',e,flush=True)
    if SINK[0] is not None:
        try: SINK[0]()
        except Exception as e: print('gaplimit sink err',e,flush=True)
# ---------- read-only summary ----------
def _twins():
    try:
        import gapshadow as G
        return {t.get('shadowKey'):t for t in G.paper_snapshot()}
    except Exception: return {}
def summary():
    os_=snapshot(); tw=_twins()
    bysig={}
    for o in os_: bysig.setdefault(o['shadowKey'],{})[o['variant']]=o
    out={}
    for off in VARIANTS:
        vn=vname(off); rows=[s for s in bysig.values() if vn in s]
        fin=[s for s in rows if s[vn].get('settle') is not None]
        fil=[s[vn] for s in rows if s[vn].get('filledAt') is not None]
        ffin=[s[vn] for s in fin if s[vn].get('filledAt') is not None]
        unf=[s for s in fin if s[vn].get('filledAt') is None]
        ttf=[o['timeToFill'] for o in fil if o.get('timeToFill') is not None]
        reasons={}
        for s in rows:
            o=s[vn]
            if o['status']=='expired': reasons[o.get('expireReason') or '?']=reasons.get(o.get('expireReason') or '?',0)+1
        mk=lambda s,k:(s.get('mkt') or {}).get(k)
        pnl=sum(o.get('pnl') or 0 for o in ffin); hold=sum(o.get('hold_pnl') or 0 for o in ffin)
        mkt_same=sum(mk(s,'pnl') or 0 for s in fin); mkt_hold_same=sum(mk(s,'hold_pnl') or 0 for s in fin)
        fink={s[vn]['shadowKey'] for s in fin}
        twin=[tw.get(k) for k in fink if tw.get(k) and tw[k].get('status') not in ('open',None) and tw[k].get('stake')]
        out[vn]=dict(offsetC=round(off*100),signals=len(rows),settledSignals=len(fin),filled=len(fil),
                     fillRate=round(len(fil)/len(rows),3) if rows else None,
                     timeToFillSec=dict(avg=round(sum(ttf)/len(ttf),1) if ttf else None,median=round(statistics.median(ttf),1) if ttf else None,max=max(ttf) if ttf else None),
                     resting=sum(1 for s in rows if s[vn]['status']=='resting'),expiredByReason=reasons,
                     filledSettled=len(ffin),wins=sum(1 for o in ffin if (o.get('pnl') or 0)>0),losses=sum(1 for o in ffin if (o.get('pnl') or 0)<=0),
                     trailExits=sum(1 for o in ffin if o.get('exit') is not None),pnl=round(pnl,2),holdPnl=round(hold,2),
                     unfilledSettled=len(unf),unfilledMktPnl=round(sum(mk(s,'pnl') or 0 for s in unf),2),unfilledMktHoldPnl=round(sum(mk(s,'hold_pnl') or 0 for s in unf),2),
                     mktPnlSameSignals=round(mkt_same,2),mktHoldPnlSameSignals=round(mkt_hold_same,2),vsMkt=round(pnl-mkt_same,2),
                     twinPaper=dict(n=len(twin),pnlPerDollar=round(sum((t.get('pnl') or 0)/t['stake'] for t in twin),2),holdPerDollar=round(sum((t.get('hold_pnl') or 0)/t['stake'] for t in twin if t.get('hold_pnl') is not None),2)))
    return dict(enabled=ENABLED,shadowOnly=True,variants=[vname(o) for o in VARIANTS],offsetsC=[round(o*100) for o in OFFSETS],cutoffLeftSec=CUTOFF,
                exit=f'trail arm fill+{round(ARM*100)}c / {round(DROP*100)}c giveback, else hold to settlement',stakeUsd=STAKE,
                fill='only when our-side ask <= limit (no mid/bid fills), filled at the limit, checked each tick from the tick after the signal',
                fee='taker formula on entry and exit (conservative)',stats=dict(STAT),byVariant=out,
                note='SHADOW ONLY: simulated resting limit buys on Rule A gap signals (BTC-ETH, SOL-XRP); never places orders, never live. mkt = $1 market entry at the signal ask with the same exit. pnl/vsMkt count settled signals only.')
def brief():
    s=summary()['byVariant']; parts=[]
    for k,v in s.items():
        if k=='mkt': parts.append(f"mkt {v['settledSignals']} sig {v['pnl']:+.2f}")
        else: parts.append(f"{k} fill {round((v['fillRate'] or 0)*100)}% {v['pnl']:+.2f} (vs mkt {v['vsMkt']:+.2f})")
    return ' · '.join(parts)
