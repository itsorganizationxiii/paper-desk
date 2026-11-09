"""SHADOW-ONLY entry filters (Nov 9 2026, owner-approved t1002). Research tags only: nothing here can open, skip, size,
stop or exit a trade. Each Spec paper trade and each gap shadow (A/B/C) / gap PAPER trade gets t['filters'] recording what
each filter WOULD have done at entry, using only data known at entry time:
  skip2L    per strategy (spec | gapShadow = A/B/C combined | gapPaper): after 2 consecutive losing rounds (round P&L<0,
            rounds = 15m windows with >=1 settled trade of that strategy), skip the very next 15m window; reset after it.
  specStrict (Spec only): keep only if entry-side price >=0.80 or leftSec <=540.
  chop65    round is choppy if the asset's Kalshi yes mid crossed 50c >=2 times this window before entry (crossChop) OR the
            prior window's outcome differed from the window before (priorFlip; outcome = last observed yes mid of that
            window >0.5, observed within 90s of its close). Choppy -> keep only if entry-side price >=0.65.
Tags persist with the trade docs (Spec state doc / gap_trades). Mid-cross/outcome history is snapshotted to research_state."""
import collections, threading, time
VERSION=1
STRICT_PX=0.80; STRICT_LEFT=540.0; CHOP_CROSSES=2; CHOP_PX=0.65; ROUND=900.0; OUT_MAX_AGE=90.0
_lk=threading.Lock()
WIN={}                                                      # asset -> dict(close,sign,crosses,last,lastTs,n)
OUT=collections.defaultdict(lambda:collections.deque(maxlen=12))   # asset -> deque[(close,'yes'|'no')]
STAT=dict(observed=0,tagged=0,errors=0,lastError=None)
GAP_SHADOW_RULES=('A','B','C')
def _f(x):
    try: return float(x) if x is not None else None
    except Exception: return None
def _mid(m):
    b,a=_f(m.get('yesBid')),_f(m.get('yesAsk'))
    if b is not None and a is not None and (a or b): return (a+b)/2
    return None
def _sgn(y):
    if y is None or abs(y-0.5)<1e-9: return 0
    return 1 if y>0.5 else -1
def _finish(asset,w):
    if w and w.get('last') is not None and w.get('lastTs') is not None and w['close']-w['lastTs']<=OUT_MAX_AGE:
        s=_sgn(w['last'])
        if s: OUT[asset].append((w['close'],'yes' if s>0 else 'no'))
def observe(asset,m,now=None):
    """Feed one Kalshi quote (server mk() dict) for an asset. Never raises."""
    try:
        if not isinstance(m,dict) or not m.get('close'): return
        now=time.time() if now is None else now; c=float(m['close'])
        if now>=c: return
        y=_mid(m)
        if y is None: return
        with _lk:
            w=WIN.get(asset)
            if w is None or w['close']!=c:
                if w is not None and w['close']<c: _finish(asset,w)
                if w is not None and w['close']>c: return
                w=WIN[asset]=dict(close=c,sign=0,crosses=0,last=None,lastTs=None,n=0)
            s=_sgn(y)
            if s:
                if w['sign'] and s!=w['sign']: w['crosses']+=1
                w['sign']=s
            w.update(last=y,lastTs=now,n=w['n']+1); STAT['observed']+=1
    except Exception as e: _err(e)
def _err(e):
    STAT['errors']+=1; STAT['lastError']=f'{type(e).__name__}: {str(e)[:120]}'
def outcome(asset,close):
    for c,r in OUT.get(asset) or ():
        if abs(c-close)<1: return r
    return None
def chop_state(asset,close):
    with _lk:
        w=WIN.get(asset); cr=w['crosses'] if w and abs(w['close']-close)<1 else None
        o1,o2=outcome(asset,close-ROUND),outcome(asset,close-2*ROUND)
    pf=None if (o1 is None or o2 is None) else (o1!=o2)
    return dict(crosses=cr,crossChop=bool(cr is not None and cr>=CHOP_CROSSES),priorFlip=pf,prior=[o2,o1])
def _tpnl(t):
    for k in ('pnl','pnlRaw'):
        v=_f(t.get(k))
        if v is not None: return v
    return None
def _done(t,now):
    if t.get('status') in ('open',None): return False
    ca=_f(t.get('closedAt'))
    return ca is None or ca<=now+1e-6
def skip2l_state(trades,close,now):
    """Pure. trades: that strategy's trades. Rounds strictly before `close` whose trades are all settled by `now`."""
    rp=collections.defaultdict(float); ok=collections.defaultdict(lambda:True)
    for t in trades:
        c=_f(t.get('closesAt'))
        if c is None or c>=close-1: continue
        c=round(c)
        if not _done(t,now) or _tpnl(t) is None: ok[c]=False; continue
        rp[c]+=_tpnl(t); ok[c]
    streak=0; skip_round=None; last=[]; prev=None
    for c in sorted(k for k in rp if ok[k]):
        if prev is not None and abs(c-prev-ROUND)>1: streak=0          # losing rounds must be consecutive 15m windows
        prev=c
        if skip_round is not None:
            if abs(c-skip_round)<1: skip_round=None; last.append((c,round(rp[c],2),'skipped')); continue   # skipped round never counts
            if c>skip_round: skip_round=None
        if rp[c]<0:
            streak+=1
            if streak>=2: skip_round=c+ROUND; streak=0
        else: streak=0
        last.append((c,round(rp[c],2)))
    sk=skip_round is not None and abs(skip_round-close)<1
    return dict(skip=bool(sk),streak=streak,skipRound=skip_round,lastRounds=last[-3:])
def build(strategy,trades,asset,close,price,left,now,spec=False):
    close=float(close); price=_f(price); left=_f(left)
    s2=skip2l_state(trades,close,now)
    ch=chop_state(asset,close); choppy=bool(ch['crossChop'] or ch['priorFlip'])
    out=dict(v=VERSION,strategy=strategy,at=round(now,3),asset=asset,price=price,leftSec=None if left is None else round(left,1),
             skip2L=dict(s2,keep=not s2['skip']),
             chop65=dict(ch,choppy=choppy,keep=(not choppy) or (price is not None and price>=CHOP_PX-1e-9)))
    if spec: out['specStrict']=dict(keep=bool((price is not None and price>=STRICT_PX-1e-9) or (left is not None and left<=STRICT_LEFT+1e-9)))
    return out
def tag(t,strategy,trades,asset,price,now=None,spec=False):
    """Attach t['filters'] (never raises, never touches anything else on t)."""
    try:
        now=_f(t.get('openedAt')) or time.time() if now is None else now
        c=_f(t.get('closesAt'))
        if c is None: return None
        t['filters']=build(strategy,[x for x in trades if x is not t],asset,c,price,c-now,now,spec=spec); STAT['tagged']+=1
        return t['filters']
    except Exception as e: _err(e); return None
def is_spec(t):
    return not (t.get('mode')=='live' and not t.get('shadow')) and not t.get('gap')
# ---------- summary ----------
FILTERS={'spec':('skip2L','specStrict','chop65'),'gapShadow':('skip2L','chop65'),'gapPaper':('skip2L','chop65')}
def _agg(ts):
    p=[_tpnl(t) or 0.0 for t in ts]
    return dict(n=len(ts),wins=sum(1 for x in p if x>0),losses=sum(1 for x in p if x<=0),pnl=round(sum(p),2))
def summarize(groups,et=None,max_rounds=96):
    """groups: {strategy: [trades]} (closed trades with t['filters'] are scored)."""
    res={}; rounds={}
    for st,ts in groups.items():
        ts=[t for t in ts if isinstance(t.get('filters'),dict) and t.get('status') not in ('open',None) and _tpnl(t) is not None]
        base=_agg(ts); fo={}
        for fn in FILTERS.get(st,()):
            kept=[t for t in ts if (t['filters'].get(fn) or {}).get('keep',True)]; sk=[t for t in ts if t not in kept]
            k=_agg(kept); fo[fn]=dict(kept=k,skipped=_agg(sk),baseline=base,deltaVsBaseline=round(k['pnl']-base['pnl'],2))
        res[st]=dict(baseline=base,filters=fo,open=0)
        for t in ts:
            c=round(_f(t.get('closesAt')) or 0); r=rounds.setdefault(c,dict(round=c,roundET=et(c) if et else c)).setdefault(st,dict(n=0,pnl=0.0,kept={}))
            r['n']+=1; r['pnl']=round(r['pnl']+_tpnl(t),2)
            for fn in FILTERS.get(st,()):
                kk=r['kept'].setdefault(fn,dict(n=0,pnl=0.0,skipped=0))
                if (t['filters'].get(fn) or {}).get('keep',True): kk['n']+=1; kk['pnl']=round(kk['pnl']+_tpnl(t),2)
                else: kk['skipped']+=1
    return dict(summary=res,rounds=[rounds[k] for k in sorted(rounds)][-max_rounds:],stats=dict(STAT),
                params=dict(specStrict=dict(minPrice=STRICT_PX,maxLeftSec=STRICT_LEFT),chop65=dict(minCrosses=CHOP_CROSSES,minPrice=CHOP_PX),skip2L=dict(losingRounds=2,skipRounds=1)),
                note='SHADOW ONLY: what each filter WOULD have done at entry; no trade was skipped, sized or exited by these filters')
# ---------- persistence of observation state ----------
def state():
    with _lk: return dict(win={a:dict(w) for a,w in WIN.items()},out={a:list(map(list,d)) for a,d in OUT.items()})
def restore(d,now=None):
    try:
        if not isinstance(d,dict): return
        now=time.time() if now is None else now
        with _lk:
            for a,rows in (d.get('out') or {}).items():
                if not OUT[a]:
                    for c,r in rows: OUT[a].append((float(c),r))
            for a,w in (d.get('win') or {}).items():
                if a in WIN or not isinstance(w,dict): continue
                if float(w.get('close') or 0)>now: WIN[a]=dict(w)
                else:
                    _finish(a,w)
    except Exception as e: _err(e)
