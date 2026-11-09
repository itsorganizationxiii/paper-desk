"""Gap LIVE test (owner t1013 + approval t1014, Nov 9 2026): Rule A gap signals may place REAL Kalshi orders.
Hard limits, all enforced here (server wires the hooks):
  - GAP_LIVE_ENABLED (default '1' for this commit only so it takes effect without a Render env change; set 0 to kill)
  - GAP_LIVE_MAX_TRADES (default 3): counted in the Mongo 'live' state doc (Live.gapLiveUsed); partial fills count.
    When used>=max, gap-live auto-disables (Live.gapLiveDisabled=True, persisted) and never re-enables by itself.
  - Rule A only; pairs BTC-ETH and SOL-XRP only; the traded (laggard) leg is never XRP (XRP hard block kept in live.py too).
  - Fixed $1 per trade (GAP_LIVE_USD, capped at live.HARD_MAX_USD), overriding the $3 strong paper tier.
  - Hold to settlement: no trail / takeprofit / stops / edgedecay on gapLive trades (server guards every exit path).
  - Needs Live.enabled(): LIVE_TRADING=1 + keys + NOT paused (owner presses Resume) + lease owner. Daily loss + stale kill apply.
LIVE_STRATEGIES (default 'gap') names which strategies may trade live; Spec/leadlag/ethlead are excluded by default."""
import os, re, threading, time, collections
E=os.environ.get
ENABLED=E('GAP_LIVE_ENABLED','1')!='0'
MAX_TRADES=int(E('GAP_LIVE_MAX_TRADES','3'))
USD=float(E('GAP_LIVE_USD','1'))
QUOTE_WAIT=float(E('GAP_QUOTE_WAIT','3'))   # seconds to wait for a first/next WS quote before REST fallback
RETRY_MAX=int(E('GAP_STALE_RETRY_MAX','5')); RETRY_WINDOW=90.0   # stale-quote retries per ticker, within 90s of the signal
WATCH_ASSETS=('BTC','ETH','SOL')   # non-XRP legs of BTC-ETH / SOL-XRP
RULES={'A'}
PAIRS={'BTC-ETH','SOL-XRP'}
LIVE_STRATEGIES={x.strip().lower() for x in E('LIVE_STRATEGIES','gap').split(',') if x.strip()}
def strategy_live(name,lv):
    """True only if the live engine is enabled (Resume pressed etc.) AND this strategy is allowed to trade live."""
    return bool(lv is not None and str(name).lower() in LIVE_STRATEGIES and lv.enabled())
_SAFE=('at','action','ticker','side','limit','want','filled','http','error')
def _scrub(v):
    v=str(v)
    if v.startswith('balance'): return 'balance error'
    return re.sub(r'cash \$-?[\d.]+,? ?','',v)[:200]
def _safe(o):
    return {k:(_scrub(o[k]) if k=='error' and o.get(k) is not None else o.get(k)) for k in _SAFE if k in o}
def _xrp(x): return 'XRP' in str(x or '').upper()
class GapLive:
    def __init__(s,lv,opener,log=print):
        s.lv=lv; s.opener=opener; s.log=log; s.lk=threading.Lock()
        s.stat=dict(signals=0,blocked=0,attempts=0,filled=0,lastReason=None,lastAt=None)
        s.recent=collections.deque(maxlen=10)   # diagnostics only: last 10 live attempt records
        s.pending={}   # ticker -> dict(p,m,n,first) awaiting a stale-quote retry
        s.watched={}   # asset -> current-round ticker pre-subscribed on the WS
    def presubscribe(s,markets):
        """Each tick: subscribe the WS feed to the current BTC/ETH/SOL 15-min markets (read-only), so a quote is warm before any signal."""
        try:
            if s.lv is None or not getattr(s.lv,'ws',None): return
            for x in WATCH_ASSETS:
                tk=((markets or {}).get(x) or {}).get('ticker') if isinstance((markets or {}).get(x),dict) else None
                if tk and not _xrp(tk) and s.lv.prewatch(tk): s.watched[x]=tk
        except Exception as e: s.log('GAPLIVE presub err',e)
    def tick(s):
        """Each tick: retry gap-live signals that were skipped only for a stale Kalshi quote (capped)."""
        if not s.pending: return
        for tk,r in list(s.pending.items()):
            try:
                if time.time()-r['first']>RETRY_WINDOW or r['n']>=RETRY_MAX: s.pending.pop(tk,None); continue
                rb=s.block_reason(r['p'])
                if rb: s.pending.pop(tk,None); s._rec(r['p'],'retry dropped: '+rb,0); continue
                r['n']+=1; s.stat['retries']=s.stat.get('retries',0)+1
                with s.lk: s._attempt(r['p'],r['m'],retry=r['n']-1)
            except Exception as e: s.pending.pop(tk,None); s.log('GAPLIVE retry err',e)
    def _rec(s,p,reason,filled,**extra):
        try: s.recent.append(dict(at=time.time(),pair=p.get('pair'),ticker=p.get('ticker'),side=p.get('side'),
                                  signalPrice=p.get('price') or p.get('entry'),reason=reason,filled=filled,**extra))
        except Exception: pass
    def _why(s):
        lv=s.lv; return str(getattr(lv,'lastBuyDetail',None) or getattr(lv,'lastBuy',None) or 'execution guard (no reason recorded)')
    # persisted counters live on the Live object (saved in its Mongo state doc)
    def used(s): return int(getattr(s.lv,'gapLiveUsed',0) or 0) if s.lv is not None else 0
    def disabled(s): return bool(getattr(s.lv,'gapLiveDisabled',False)) if s.lv is not None else True
    def active(s): return ENABLED and 'gap' in LIVE_STRATEGIES and not s.disabled() and s.used()<MAX_TRADES
    def block_reason(s,p):
        if not ENABLED: return 'GAP_LIVE_ENABLED=0'
        if 'gap' not in LIVE_STRATEGIES: return 'gap not in LIVE_STRATEGIES'
        if s.lv is None: return 'live module unavailable'
        if s.disabled() or s.used()>=MAX_TRADES: return f'cap reached ({s.used()}/{MAX_TRADES}); gap-live auto-disabled'
        if p.get('rule') not in RULES: return f"rule {p.get('rule')} not live"
        if p.get('pair') not in PAIRS: return f"pair {p.get('pair')} not live"
        if _xrp(p.get('ticker')) or _xrp(p.get('asset')) or _xrp(p.get('laggard')): return 'XRP leg is never live'
        if p.get('shadow') or p.get('mode')!='paper' or not p.get('gap'): return 'not a gap paper signal'
        if not s.lv.enabled(): return 'live paused/off (owner must press Resume)'
        return None
    def on_paper(s,p,m=None):
        """Called right after a gap PAPER trade opens. Returns the live trade or None. Never raises."""
        try:
            s.stat['signals']+=1
            r=s.block_reason(p)
            if r: s.stat.update(blocked=s.stat['blocked']+1,lastReason=r,lastAt=time.time()); return None
            with s.lk:
                r=s.block_reason(p)   # re-check under the lock (two pairs can fire in one tick)
                if r: s.stat.update(blocked=s.stat['blocked']+1,lastReason=r,lastAt=time.time()); return None
                return s._attempt(p,m)
        except Exception as e:
            s.stat.update(lastReason=f'error: {type(e).__name__}: {e}',lastAt=time.time()); s._rec(p if isinstance(p,dict) else {},f'error: {type(e).__name__}',0); s.log('GAPLIVE err',e); return None
    def _attempt(s,p,m,retry=0):
        """One live buy for a gap paper signal; caller holds s.lk and has checked block_reason. Never raises."""
        try:
            s.stat['attempts']+=1; t0=time.time()
            fill=s.lv.buy(p['asset'],p['ticker'],p['side'],1.0,float(p.get('price') or p.get('entry')),usd=USD,quote_wait=QUOTE_WAIT)
            lat=dict(retry=retry,sinceSignal=round(t0-float(p.get('openedAt') or t0),1),buySec=round(time.time()-t0,1))
            if not fill or not fill.get('contracts'):
                why=s._why(); s._rec(p,why,0,**lat)
                if getattr(s.lv,'lastBuyRetry',False): s.pending.setdefault(p['ticker'],dict(p=p,m=m,n=1,first=t0))   # n = tries made
                else: s.pending.pop(p.get('ticker'),None)
                s.stat.update(lastReason='no fill: '+why,lastAt=time.time()); return None
            s.pending.pop(p.get('ticker'),None)
            s._rec(p,'filled',fill.get('contracts'),**lat)
            s.lv.gapLiveUsed=s.used()+1
            if s.lv.gapLiveUsed>=MAX_TRADES: s.lv.gapLiveDisabled=True
            s.lv.persist()
            s.stat.update(filled=s.stat['filled']+1,lastReason='filled',lastAt=time.time())
            strike=(m or {}).get('strike') if isinstance(m,dict) and (m or {}).get('ticker')==p.get('ticker') else None
            t=s.opener(p,fill,strike)
            try: p['liveTwinId']=t and t.get('id'); p['liveTwinOrderId']=fill.get('orderId')
            except Exception: pass
            s.log('GAPLIVE open',p.get('pair'),p.get('ticker'),p.get('side'),fill.get('price'),fill.get('contracts'),f'{s.used()}/{MAX_TRADES}')
            return t
        except Exception as e:
            s.stat.update(lastReason=f'error: {type(e).__name__}: {e}',lastAt=time.time()); s._rec(p if isinstance(p,dict) else {},f'error: {type(e).__name__}',0); s.log('GAPLIVE err',e); return None
    def ws_fresh(s):
        """Read-only: is the live WS feed holding a fresh quote for each pre-subscribed gap ticker? (no prices)"""
        try:
            if s.lv is None or not getattr(s.lv,'ws',None): return dict(ws=None)
            out={x:dict(ticker=tk,**s.lv.ws_info(tk)) for x,tk in list(s.watched.items())}
            out['feed']=s.lv.ws_status(); return out
        except Exception as e: return dict(error=type(e).__name__)
    def order_log(s,n=10):
        """Last n live order-log entries for gap-live tickers (Rule A pairs' laggard legs), safe fields only."""
        try:
            tks={r.get('ticker') for r in s.recent if r.get('ticker')}
            out=[]
            for o in list(getattr(s.lv,'orders',None) or [])[::-1]:
                tk=str(o.get('ticker') or '')
                if not (tk in tks or (o.get('action') in ('buy','buy-skip','frac-test') and tk[:5] in ('KXBTC','KXETH','KXSOL'))): continue
                out.append(_safe(o))
                if len(out)>=n: break
            return out
        except Exception as e: return [dict(error=f'order log unavailable: {type(e).__name__}')]
    def summary(s,trades):
        lt=[t for t in trades if t.get('gapLive')]
        cl=[t for t in lt if t.get('status')!='open']
        return dict(enabled=ENABLED,active=s.active(),liveStrategies=sorted(LIVE_STRATEGIES),used=s.used(),max=MAX_TRADES,
                    autoDisabled=s.disabled(),usd=USD,rules=sorted(RULES),pairs=sorted(PAIRS),exit='hold to settlement',
                    liveEngine=dict(envAllow=bool(s.lv and s.lv.allowed()),paused=bool(s.lv and s.lv.paused),killSwitch=bool(s.lv and s.lv.killed)),
                    stats=dict(s.stat),recentAttempts=list(s.recent)[::-1],recentOrders=s.order_log(),pendingRetries={k:dict(n=v['n'],age=round(time.time()-v['first'],1)) for k,v in list(s.pending.items())},wsQuoteFresh=s.ws_fresh(),open=[t for t in lt if t.get('status')=='open'],closed=cl,
                    results=dict(trades=len(lt),closed=len(cl),wins=sum(1 for t in cl if (t.get('pnl') or 0)>0),
                                 losses=sum(1 for t in cl if (t.get('pnl') or 0)<=0),pnl=round(sum(t.get('pnl') or 0 for t in cl),2)))
