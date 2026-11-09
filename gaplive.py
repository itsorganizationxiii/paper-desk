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
import os, threading, time
E=os.environ.get
ENABLED=E('GAP_LIVE_ENABLED','1')!='0'
MAX_TRADES=int(E('GAP_LIVE_MAX_TRADES','3'))
USD=float(E('GAP_LIVE_USD','1'))
RULES={'A'}
PAIRS={'BTC-ETH','SOL-XRP'}
LIVE_STRATEGIES={x.strip().lower() for x in E('LIVE_STRATEGIES','gap').split(',') if x.strip()}
def strategy_live(name,lv):
    """True only if the live engine is enabled (Resume pressed etc.) AND this strategy is allowed to trade live."""
    return bool(lv is not None and str(name).lower() in LIVE_STRATEGIES and lv.enabled())
def _xrp(x): return 'XRP' in str(x or '').upper()
class GapLive:
    def __init__(s,lv,opener,log=print):
        s.lv=lv; s.opener=opener; s.log=log; s.lk=threading.Lock()
        s.stat=dict(signals=0,blocked=0,attempts=0,filled=0,lastReason=None,lastAt=None)
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
                s.stat['attempts']+=1
                fill=s.lv.buy(p['asset'],p['ticker'],p['side'],1.0,float(p.get('price') or p.get('entry')),usd=USD)
                if not fill or not fill.get('contracts'):
                    s.stat.update(lastReason='no fill: '+str(getattr(s.lv,'lastBuy',None) or 'execution guard'),lastAt=time.time()); return None
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
            s.stat.update(lastReason=f'error: {type(e).__name__}: {e}',lastAt=time.time()); s.log('GAPLIVE err',e); return None
    def summary(s,trades):
        lt=[t for t in trades if t.get('gapLive')]
        cl=[t for t in lt if t.get('status')!='open']
        return dict(enabled=ENABLED,active=s.active(),liveStrategies=sorted(LIVE_STRATEGIES),used=s.used(),max=MAX_TRADES,
                    autoDisabled=s.disabled(),usd=USD,rules=sorted(RULES),pairs=sorted(PAIRS),exit='hold to settlement',
                    liveEngine=dict(envAllow=bool(s.lv and s.lv.allowed()),paused=bool(s.lv and s.lv.paused),killSwitch=bool(s.lv and s.lv.killed)),
                    stats=dict(s.stat),open=[t for t in lt if t.get('status')=='open'],closed=cl,
                    results=dict(trades=len(lt),closed=len(cl),wins=sum(1 for t in cl if (t.get('pnl') or 0)>0),
                                 losses=sum(1 for t in cl if (t.get('pnl') or 0)<=0),pnl=round(sum(t.get('pnl') or 0 for t in cl),2)))
