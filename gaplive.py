"""Gap LIVE session #4 (owner t1164u, Nov 10 2026): uncapped $1 Rule A until the owner pauses.
Tests #1–#3 stay in Live.gapLivePrev / trade history (test #3 was 5 trades, MoogleEdge 65%+ gate, then auto-disable).
Session #4:
  - Crypto: same as test #3 — Rule A on BTC-ETH and SOL-XRP (XRP never the laggard), MoogleEdge side p>=0.65,
    skip if MoogleEdge p is missing or stale (>60s). No fallback.
  - Metals: GOLD-SILVER Rule A is now live. No MoogleEdge gate (crypto-only). Weekday 6:00 AM–5:00 PM ET only,
    only when |gap|>=0.06, and Kalshi metals closures are respected (no metals 5–6 PM ET weekdays, nor
    Fri 5 PM–Sun 6 PM ET). Same Kalshi order path / quote checks as crypto (WS pre-subscribe KXGOLD15M/KXSILVER15M).
  - Skip all live entries during the midnight hour (00:00–00:59 ET).
  - $1 per trade, same +15c arm / 10c giveback trail with reduce-only IOC sells, else hold to settlement.
  - NO trade cap and NO auto-disable: runs until the owner presses Pause. Leftover GAP_LIVE_MAX_TRADES env is ignored.
  - Changing GAP_LIVE_TEST / TEST_ID still resets the counter once and forces live PAUSED until owner Resume.
Hard limits, all enforced here (server wires the hooks):
  - GAP_LIVE_ENABLED (default '1' so it takes effect without a Render env change; set 0 to kill)
  - Rule A only; XRP leg is never live (XRP hard block kept in live.py too).
  - Fixed $1 per trade (GAP_LIVE_USD, capped at live.HARD_MAX_USD), overriding the $3 strong paper tier.
  - Live trailing exit as in tests #2/#3 (server still no-ops generic takeprofit/stop/edgedecay paths).
  - Needs Live.enabled(): LIVE_TRADING=1 + keys + NOT paused (owner presses Resume) + lease owner. Daily loss + stale kill apply.
LIVE_STRATEGIES (default 'gap') names which strategies may trade live; Spec/leadlag/ethlead are excluded by default."""
import os, re, threading, time, collections, contextlib
from datetime import datetime
from paper_tz import ET
E=os.environ.get
ENABLED=E('GAP_LIVE_ENABLED','1')!='0'
MAX_TRADES=0   # session 4: no cap / no auto-disable. Leftover GAP_LIVE_MAX_TRADES env is ignored.
TEST_ID=E('GAP_LIVE_TEST','4')   # changing this id resets the live-trade counter once (and pauses live)
METAL_PAIR='GOLD-SILVER'
METALS={'GOLD','SILVER'}
METAL_MIN_GAP=0.06
METAL_DAY_START=6   # 6:00 AM ET inclusive
METAL_DAY_END=17    # 5:00 PM ET exclusive
MOOGLE_BAR=float(E('GAP_LIVE_MOOGLE_BAR','0.65'))          # same bar as night shadow N1 (moogle-65)
MOOGLE_STALE_SEC=float(E('GAP_LIVE_MOOGLE_STALE_SEC','60')) # skip live if MoogleEdge p is older than this
USD=float(E('GAP_LIVE_USD','1'))
QUOTE_WAIT=float(E('GAP_QUOTE_WAIT','1.5'))   # seconds to wait for a first/next WS quote before REST fallback
RETRY_MAX=int(E('GAP_STALE_RETRY_MAX','5')); RETRY_WINDOW=90.0   # stale-quote retries per ticker, within 90s of the signal
def _exit_spec(v):
    p=str(v or '').strip().lower().split(':')
    if p[0]=='trail' and len(p)==3: return ('trail',float(p[1]),float(p[2]))
    return ('hold',None,None)
EXIT=_exit_spec(E('GAP_LIVE_EXIT','trail:0.15:0.10'))   # same trail as the gap PAPER book (GAP_PAPER_EXIT default)
SELL_TRIES=int(E('GAP_TRAIL_SELL_TRIES','5'))           # live.sell() calls per trade (each = up to 3 reduce-only IOCs, bid-2/4/6c)
SELL_GAP_SEC=2.0                                        # min spacing between sell retries
NO_SELL_LAST_SEC=float(E('GAP_TRAIL_NO_SELL_LAST_SEC','8'))   # inside this many seconds of close: hold to settlement
def exit_mode(): return f'trail +{round(EXIT[1]*100)}c arm / {round(EXIT[2]*100)}c giveback (real Kalshi sell), else hold to settlement' if EXIT[0]=='trail' else 'hold to settlement'
def trail_step(t,bid,now):
    """Update peak/arm on a gap-live trade from our-side bid; latch and return True once the giveback triggers."""
    if EXIT[0]!='trail' or bid is None or bid<=0: return bool(t.get('trailTriggeredAt'))
    _,arm,drop=EXIT; entry=float(t.get('price') or 0)
    t['peakBid']=round(max(float(t.get('peakBid') or 0),float(bid)),4)
    if t['peakBid']>=entry+arm-1e-9:
        if not t.get('trailArmedAt'): t['trailArmedAt']=now
        if not t.get('trailTriggeredAt') and bid<=t['peakBid']-drop+1e-9:
            t.update(trailTriggeredAt=now,trailTrigBid=round(float(bid),4),
                     trailReason=f"trail: peak {t['peakBid']:.2f} (armed at entry {entry:.2f}+{arm:.2f}) -> bid {float(bid):.2f}")
    return bool(t.get('trailTriggeredAt'))
WATCH_ASSETS=('BTC','ETH','SOL','GOLD','SILVER')   # non-XRP legs of BTC-ETH / SOL-XRP, plus metals
RULES={'A'}
PAIRS={'BTC-ETH','SOL-XRP','GOLD-SILVER'}
LIVE_STRATEGIES={x.strip().lower() for x in E('LIVE_STRATEGIES','gap').split(',') if x.strip()}
def strategy_live(name,lv):
    """True only if the live engine is enabled (Resume pressed etc.) AND this strategy is allowed to trade live."""
    return bool(lv is not None and str(name).lower() in LIVE_STRATEGIES and lv.enabled())
_SAFE=('at','action','ticker','side','limit','want','filled','fillPrice','fee','http','error','result','pnl','peak','entry')
def _scrub(v):
    v=str(v)
    if v.startswith('balance'): return 'balance error'
    return re.sub(r'cash \$-?[\d.]+,? ?','',v)[:200]
def _safe(o):
    return {k:(_scrub(o[k]) if k=='error' and o.get(k) is not None else o.get(k)) for k in _SAFE if k in o}
def _xrp(x): return 'XRP' in str(x or '').upper()
def _has_cap(): return int(MAX_TRADES or 0)>0
def et_dt(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(),ET)
def midnight_hour(ts=None):
    """True during 00:00–00:59 ET: no live entries (crypto or metals)."""
    return et_dt(ts).hour==0
def metals_session_closed(ts=None):
    # Kalshi GOLD/SILVER closed: daily 17:00-18:00 ET, and Fri 17:00 ET -> Sun 18:00 ET (DST-aware). Same as server.metals_closed.
    d=et_dt(ts); wd,h=d.weekday(),d.hour
    if h==17: return True
    if wd==4 and h>=17: return True
    if wd==5: return True
    if wd==6 and h<18: return True
    return False
def metals_daytime(ts=None):
    """Weekday 6:00 AM–5:00 PM ET and Kalshi metals session open."""
    d=et_dt(ts)
    if d.weekday()>=5: return False
    if not (METAL_DAY_START<=d.hour<METAL_DAY_END): return False
    return not metals_session_closed(ts)
def is_metal(p):
    return (p.get('pair')==METAL_PAIR) or (p.get('asset') or p.get('laggard')) in METALS
def _norm(p):
    """Copy a paper or GOLD-SILVER shadow signal into the live-hook shape (asset/price filled in)."""
    if not isinstance(p,dict): return p
    out=dict(p)
    if not out.get('asset'): out['asset']=out.get('laggard')
    if out.get('price') is None and out.get('entry') is not None: out['price']=out.get('entry')
    return out
def moogle_side_p(coin,ticker,side,now=None):
    """P(traded side) from the live MoogleEdge shadow cache (M.CUR). None if missing, stale (>60s) or wrong ticker."""
    now=time.time() if now is None else now
    try:
        import moogle as M; r=M.CUR.get(coin)
        if not r or r.get('ticker')!=ticker or now-float(r.get('t') or 0)>MOOGLE_STALE_SEC or r.get('p') is None: return None
        p=float(r['p']); return round(p if side=='YES' else 1-p,4)
    except Exception: return None
def moogle_gate_reason(p,now=None):
    """None if the live MoogleEdge side-p is >= MOOGLE_BAR and fresh; else a skip reason. All hours. No fallback."""
    coin=p.get('laggard') or p.get('asset'); mp=moogle_side_p(coin,p.get('ticker'),p.get('side'),now)
    if mp is None: return 'moogle p missing/stale (>60s)'
    if mp<MOOGLE_BAR-1e-9: return f'moogle-below-bar: {mp} < {MOOGLE_BAR:.2f}'
    return None
class GapLive:
    def __init__(s,lv,opener,log=print):
        s.lv=lv; s.opener=opener; s.log=log; s.lk=threading.Lock()
        s.async_=False   # server sets True: each live buy runs on its own thread right when the paper signal opens
        s.emit=None      # server: re-persist the paper twin once liveTwinId is set
        s.inflight=0     # live buys in progress (reserved against the 3-trade cap)
        s.stat=dict(signals=0,blocked=0,attempts=0,filled=0,lastReason=None,lastAt=None)
        s.recent=collections.deque(maxlen=10)   # diagnostics only: last 10 live attempt records
        s.pending={}   # ticker -> dict(p,m,n,first) awaiting a stale-quote retry
        s.watched={}   # asset -> current-round ticker pre-subscribed on the WS
        s.tlock=None; s.save=None   # server: trade lock + pt.save for booking live trail exits
        s.xstat=dict(triggered=0,sellCalls=0,filled=0,partial=0,failed=0,fallbacks=0,recovered=0)
    def reset_for_test(s):
        """Once per TEST_ID: zero the live-trade counter + clear auto-disable (old values kept in gapLivePrev) and force
        live PAUSED, so the new test only starts when the owner presses Resume. Idempotent across restarts."""
        lv=s.lv
        if lv is None or getattr(lv,'gapLiveTest',None)==TEST_ID: return False
        prev=list(getattr(lv,'gapLivePrev',None) or [])
        prev.append(dict(test=getattr(lv,'gapLiveTest',None) or '1',used=int(getattr(lv,'gapLiveUsed',0) or 0),disabled=bool(getattr(lv,'gapLiveDisabled',False)),resetAt=time.time()))
        lv.gapLivePrev=prev[-10:]; lv.gapLiveUsed=0; lv.gapLiveDisabled=False; lv.gapLiveTest=TEST_ID; lv.paused=True
        try: lv.persist()
        except Exception as e: s.log('GAPLIVE reset persist err',e)
        s.log('GAPLIVE test',TEST_ID,'counter reset to 0 (no cap); live paused until Resume'); return True
    def presubscribe(s,markets):
        """Each tick: subscribe the WS feed to the current BTC/ETH/SOL/GOLD/SILVER 15-min markets (read-only), so a quote is warm before any signal."""
        try:
            if s.lv is None or not getattr(s.lv,'ws',None): return
            for x in WATCH_ASSETS:
                tk=((markets or {}).get(x) or {}).get('ticker') if isinstance((markets or {}).get(x),dict) else None
                if tk and not _xrp(tk) and s.lv.prewatch(tk): s.watched[x]=tk
        except Exception as e: s.log('GAPLIVE presub err',e)
    def tick(s,now=None):
        """Each tick: retry gap-live signals that were skipped only for a stale Kalshi quote (capped)."""
        now=time.time() if now is None else now
        if not s.pending: return
        for tk,r in list(s.pending.items()):
            try:
                if now-r['first']>RETRY_WINDOW or r['n']>=RETRY_MAX: s.pending.pop(tk,None); continue
                rb=s.block_reason(r['p'],now=now)
                if rb: s.pending.pop(tk,None); s._rec(r['p'],'retry dropped: '+rb,0); continue
                if r.get('busy'): continue
                with s.lk:
                    rb=s.block_reason(r['p'],now=now)
                    if rb: s.pending.pop(tk,None); continue
                    s.inflight+=1
                r['n']+=1; r['busy']=True; s.stat['retries']=s.stat.get('retries',0)+1
                s._dispatch(r['p'],r['m'],r['n']-1)
            except Exception as e: s.pending.pop(tk,None); s.log('GAPLIVE retry err',e)
    def _rec(s,p,reason,filled,**extra):
        try: s.recent.append(dict(at=time.time(),pair=p.get('pair'),ticker=p.get('ticker'),side=p.get('side'),
                                  signalPrice=p.get('price') or p.get('entry'),reason=reason,filled=filled,**extra))
        except Exception: pass
    def _why(s):
        lv=s.lv; return str(getattr(lv,'lastBuyDetail',None) or getattr(lv,'lastBuy',None) or 'execution guard (no reason recorded)')
    # persisted counters live on the Live object (saved in its Mongo state doc)
    def used(s): return int(getattr(s.lv,'gapLiveUsed',0) or 0) if s.lv is not None else 0
    def disabled(s):
        # Session 4 has no cap, so a leftover auto-disable flag never blocks. With a cap, the persisted flag still applies.
        if not _has_cap(): return False
        return bool(getattr(s.lv,'gapLiveDisabled',False)) if s.lv is not None else True
    def active(s): return ENABLED and 'gap' in LIVE_STRATEGIES and not s.disabled() and (not _has_cap() or s.used()<MAX_TRADES)
    def block_reason(s,p,now=None):
        now=time.time() if now is None else now
        if not ENABLED: return 'GAP_LIVE_ENABLED=0'
        if 'gap' not in LIVE_STRATEGIES: return 'gap not in LIVE_STRATEGIES'
        if s.lv is None: return 'live module unavailable'
        if _has_cap() and (s.disabled() or s.used()>=MAX_TRADES): return f'cap reached ({s.used()}/{MAX_TRADES}); gap-live auto-disabled'
        if _has_cap() and s.used()+s.inflight>=MAX_TRADES: return f'cap reserved ({s.used()} used + {s.inflight} in flight of {MAX_TRADES})'
        if p.get('rule') not in RULES: return f"rule {p.get('rule')} not live"
        if p.get('pair') not in PAIRS: return f"pair {p.get('pair')} not live"
        if _xrp(p.get('ticker')) or _xrp(p.get('asset')) or _xrp(p.get('laggard')): return 'XRP leg is never live'
        # Crypto live still requires a gap PAPER twin. GOLD-SILVER is shadow-only in the paper book, so the
        # live hook may receive a liveFromShadow payload (or an already-normalized paper-shaped copy).
        paperish=(not p.get('shadow') and p.get('mode')=='paper' and p.get('gap'))
        if not paperish: return 'not a gap paper signal'
        if not s.lv.enabled(): return 'live paused/off (owner must press Resume)'
        if midnight_hour(now): return 'midnight hour (00:00–00:59 ET): no live entries'
        if is_metal(p):
            if not metals_daytime(now): return 'metals outside daytime hours (6 AM–5 PM ET weekdays) or Kalshi closed'
            try: gap=abs(float(p.get('gapDown') or 0))
            except Exception: gap=0.0
            if gap<METAL_MIN_GAP-1e-9: return f'metals gap {gap} < {METAL_MIN_GAP:.2f}'
            return None   # no MoogleEdge gate on metals
        r=moogle_gate_reason(p,now)
        if r: return r
        return None
    def on_paper(s,p,m=None,now=None):
        """Called right after a gap PAPER trade opens (or a GOLD-SILVER shadow signal). Returns the live trade or None. Never raises."""
        try:
            p=_norm(p); s.stat['signals']+=1
            r=s.block_reason(p,now=now)
            if r: s.stat.update(blocked=s.stat['blocked']+1,lastReason=r,lastAt=time.time()); return None
            with s.lk:
                r=s.block_reason(p,now=now)   # re-check under the lock (two pairs can fire in one tick)
                if r: s.stat.update(blocked=s.stat['blocked']+1,lastReason=r,lastAt=time.time()); return None
                s.inflight+=1   # in-flight buys (no cap reservation in session 4); released in _run
            return s._dispatch(p,m,0)
        except Exception as e:
            s.stat.update(lastReason=f'error: {type(e).__name__}: {e}',lastAt=time.time()); s._rec(p if isinstance(p,dict) else {},f'error: {type(e).__name__}',0); s.log('GAPLIVE err',e); return None
    def _dispatch(s,p,m,retry):
        """Run one reserved attempt: on its own thread in production (tick loop never waits), inline in tests."""
        if s.async_:
            threading.Thread(target=s._run,args=(p,m,retry),daemon=True,name='gaplive-buy').start(); return None
        return s._run(p,m,retry)
    def _run(s,p,m,retry):
        try: return s._attempt(p,m,retry)
        finally:
            with s.lk: s.inflight=max(0,s.inflight-1)
            r=s.pending.get(p.get('ticker'))
            if r: r['busy']=False
    def _attempt(s,p,m,retry=0):
        """One live buy for a gap paper signal; a cap slot is already reserved (inflight). Never raises."""
        try:
            s.stat['attempts']+=1; t0=time.time()
            fill=s.lv.buy(p['asset'],p['ticker'],p['side'],1.0,float(p.get('price') or p.get('entry')),usd=USD,quote_wait=QUOTE_WAIT)
            lat=dict(retry=retry,sinceSignal=round(t0-float(p.get('openedAt') or t0),1),buySec=round(time.time()-t0,1),
                     stages=dict(getattr(s.lv,'lastBuyTiming',None) or {}))
            if not fill or not fill.get('contracts'):
                why=s._why(); s._rec(p,why,0,**lat)
                if getattr(s.lv,'lastBuyRetry',False): s.pending.setdefault(p['ticker'],dict(p=p,m=m,n=1,first=t0))   # n = tries made
                else: s.pending.pop(p.get('ticker'),None)
                s.stat.update(lastReason='no fill: '+why,lastAt=time.time()); return None
            s.pending.pop(p.get('ticker'),None)
            s._rec(p,'filled',fill.get('contracts'),**lat)
            with s.lk:
                s.lv.gapLiveUsed=s.used()+1
                if _has_cap() and s.lv.gapLiveUsed>=MAX_TRADES: s.lv.gapLiveDisabled=True
            s.lv.persist()
            s.stat.update(filled=s.stat['filled']+1,lastReason='filled',lastAt=time.time())
            strike=(m or {}).get('strike') if isinstance(m,dict) and (m or {}).get('ticker')==p.get('ticker') else None
            p['_gapLiveTest']=TEST_ID; p['_gapLiveExit']=EXIT[0]
            t=s.opener(p,fill,strike)
            try: p['liveTwinId']=t and t.get('id'); p['liveTwinOrderId']=fill.get('orderId')
            except Exception: pass
            # Do not emit liveFromShadow payloads: they are not paper-book trades and must not land in gap_trades as paper.
            if s.async_ and s.emit and p.get('liveTwinId') is not None and not p.get('liveFromShadow'):
                try: s.emit(p)
                except Exception as e: s.log('GAPLIVE emit err',e)
            s.log('GAPLIVE open',p.get('pair'),p.get('ticker'),p.get('side'),fill.get('price'),fill.get('contracts'),f"{s.used()}{' / '+str(MAX_TRADES) if _has_cap() else ''}")
            return t
        except Exception as e:
            s.stat.update(lastReason=f'error: {type(e).__name__}: {e}',lastAt=time.time()); s._rec(p if isinstance(p,dict) else {},f'error: {type(e).__name__}',0); s.log('GAPLIVE err',e); return None
    # ---------------- live trailing exit (same as test #2) ----------------
    def manage_exits(s,trades,bid_of,now=None):
        """Each tick: trail open gap-live trades of this test; start a real sell when triggered (retries capped)."""
        now=time.time() if now is None else now; started=[]
        for t in list(trades):
            try:
                if not t.get('gapLive') or t.get('status')!='open' or t.get('exitMode')!='trail': continue
                if t.get('exiting') or t.get('trailFallback') or _xrp(t.get('ticker')): continue
                if now>=float(t.get('closesAt') or 0)-NO_SELL_LAST_SEC:
                    if t.get('trailTriggeredAt'): s._fallback(t,f'within {NO_SELL_LAST_SEC:.0f}s of close')
                    continue
                bid=bid_of(t)
                was=bool(t.get('trailTriggeredAt'))
                if not trail_step(t,bid,now): continue
                if not was:
                    s.xstat['triggered']+=1
                    s.lv.log(action='gap-trail-trigger',ticker=t['ticker'],side=t['side'],want=t.get('contracts'),limit=t.get('trailTrigBid'),error=None,peak=t.get('peakBid'),entry=t.get('price'))
                if now-float(t.get('trailSellAt') or 0)<SELL_GAP_SEC: continue
                if s._start_sell(t,bid if bid else t.get('trailTrigBid'),now): started.append(t)
            except Exception as e: s.log('GAPLIVE exit err',e)
        return started
    def _fallback(s,t,why):
        t.update(trailFallback='hold to settlement',trailFallbackWhy=str(why)[:160],holdToSettle=True); s.xstat['fallbacks']+=1
        try: s.lv.log(action='gap-trail-fallback',ticker=t.get('ticker'),side=t.get('side'),want=t.get('contracts'),error=f'holding to settlement: {why}'[:200])
        except Exception: pass
        s._save()
    def _save(s):
        try:
            if s.save: s.save()
        except Exception as e: s.log('GAPLIVE save err',e)
    def _start_sell(s,t,bid,now):
        lv=s.lv
        if int(t.get('trailSellTries') or 0)>=SELL_TRIES: s._fallback(t,f'{SELL_TRIES} sell attempts without a full fill'); return False
        if not (lv.allowed() and lv.owner()):
            t['trailSellError']='live env/keys/lease unavailable; will retry'; t['trailSellAt']=now
            t['trailSellTries']=int(t.get('trailSellTries') or 0)+1; return False
        t['exiting']='trail'; t['trailSellAt']=now; t['trailSellTries']=int(t.get('trailSellTries') or 0)+1
        if s.async_: threading.Thread(target=s._sell,args=(t,bid),daemon=True,name='gaplive-sell').start()
        else: s._sell(t,bid)
        return True
    def _sell(s,t,bid):
        """One live.sell() of the held contracts (reduce-only IOCs at bid-2/4/6c); books the result. Never raises."""
        lv=s.lv; s.xstat['sellCalls']+=1; want=float(t['contracts']); realized=None
        try: fc,fpx,fee,oid,err=lv.sell(t['ticker'],t['side'],want,float(bid))
        except Exception as e: fc,fpx,fee,oid,err=0,None,0.0,None,f'{type(e).__name__}: {e}'
        lk=s.tlock if (s.tlock is not None and s.async_) else contextlib.nullcontext()   # inline (tests): caller holds no lock
        with lk:
            t.pop('exiting',None)
            t.setdefault('trailSells',[]).append(dict(at=time.time(),want=want,filled=fc,fillPrice=fpx,fee=fee,orderId=oid,error=(str(err)[:160] if err else None)))
            if fc<=0:
                s.xstat['failed']+=1; t['trailSellError']=str(err or 'no fill')[:160]
                rec=None
                if str(err)=='no position':   # Kalshi holds nothing: an earlier sell may have filled, or closed outside the bot
                    try: rec=lv.closing_fills(t['ticker'],t['side'],t.get('liveOrderId'),t.get('openedAt') or 0)
                    except Exception as e: s.log('GAPLIVE fill recover err',e)
                    if rec and rec[0]>=want-0.01 and rec[1] is not None:
                        q,p2,fe2,oids=rec; realized=round(q*p2-fe2-t['stake'],4); s.xstat['recovered']+=1
                        t.update(status='exited',exitPrice=p2,exitFee=fe2,exitReason=t.get('trailReason'),settledBy='live-trail-sell (recovered from Kalshi fills)',
                                 liveSellOrderId=oids[-1] if oids else None,pnl=round(realized+(t.get('partialPnl') or 0),2),closedAt=time.time(),fillRecovered=True)
                        t.pop('trailSellError',None); lv.log(action='sell-fill-recovered',ticker=t['ticker'],side=t['side'],filled=q,fillPrice=p2,fee=fe2,orderIds=oids)
                    else:
                        s._fallback(t,'Kalshi shows no position and no closing fills')
                elif 'XRP' in str(err) or 'stale instance' in str(err): s._fallback(t,err)
                elif int(t.get('trailSellTries') or 0)>=SELL_TRIES: s._fallback(t,f'{SELL_TRIES} sell attempts, last: {err or "no fill"}')
                s._save()
            else:
                share=min(1.0,fc/want); realized=round(fc*fpx-fee-t['stake']*share,4)
                if share>=0.999:
                    s.xstat['filled']+=1
                    t.update(status='exited',exitPrice=fpx,exitFee=round(fee,4),exitReason=t.get('trailReason'),liveSellOrderId=oid,settledBy='live-trail-sell',
                             pnl=round(realized+(t.get('partialPnl') or 0),2),closedAt=time.time(),exitLeftSec=round(float(t.get('closesAt') or 0)-time.time(),1))
                    for k in ('markPrice','unrealizedPnl','trailSellError'): t.pop(k,None)
                else:
                    s.xstat['partial']+=1
                    t.update(contracts=round(want-fc,2),stake=round(t['stake']*(1-share),4),partialPnl=round((t.get('partialPnl') or 0)+realized,4),trailSellError=f'partial {fc} of {want} filled; selling the rest')
                    if int(t.get('trailSellTries') or 0)>=SELL_TRIES: s._fallback(t,f'partial fill after {SELL_TRIES} attempts; rest held')
                s._save()
        if realized is not None:
            try: lv.book(round(realized,2))
            except Exception as e: s.log('GAPLIVE book err',e)
        if t.get('status')=='exited':   # verify the position actually closed on Kalshi
            try:
                p=lv.position(t['ticker']); held=None if p is None else round((p if t['side']=='YES' else -p),2)
                t['positionAfterExit']=held
                lv.log(action='gap-trail-verify',ticker=t['ticker'],side=t['side'],want=0,filled=held,error=None if held is not None and held<=0.01 else ('positions unavailable' if held is None else f'{held} {t["side"]} still held on Kalshi'))
            except Exception as e: s.log('GAPLIVE verify err',e)
        s.log('GAPLIVE trail sell',t.get('ticker'),fc,fpx,err,t.get('status'))
        return fc
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
                metal=tk.startswith('KXGOLD') or tk.startswith('KXSILVER')
                if not (tk in tks or (o.get('action') in ('buy','buy-skip','frac-test','sell','order-submitted','position-check','position-mismatch','settle','sell-fill-recovered') or str(o.get('action','')).startswith('gap-trail')) and (tk[:5] in ('KXBTC','KXETH','KXSOL') or metal)): continue
                out.append(_safe(o))
                if len(out)>=n: break
            return out
        except Exception as e: return [dict(error=f'order log unavailable: {type(e).__name__}')]
    def brief(s):
        """Dashboard/status line only: test id, trade count, gates. Never calls live.status() or balance()."""
        return dict(test=TEST_ID,used=s.used(),max=(MAX_TRADES if _has_cap() else None),autoDisabled=s.disabled(),
                    gate=f"crypto MoogleEdge ≥{MOOGLE_BAR:.0%} (skip if missing/stale >{int(MOOGLE_STALE_SEC)}s); metals 6AM–5PM ET weekdays, |gap|≥{int(METAL_MIN_GAP*100)}¢, no Moogle; skip 00:00–00:59 ET",
                    moogleBar=MOOGLE_BAR,moogleStaleSec=MOOGLE_STALE_SEC,
                    metals=dict(hours='6:00–17:00 ET weekdays',minGap=METAL_MIN_GAP,moogle=False,closures='Kalshi 5–6 PM ET daily, Fri 5 PM–Sun 6 PM ET'),
                    midnightSkip=True)
    def summary(s,trades):
        lt=[t for t in trades if t.get('gapLive')]
        cl=[t for t in lt if t.get('status')!='open']
        cur=[t for t in lt if t.get('gapLiveTest')==TEST_ID]; cc=[t for t in cur if t.get('status')!='open']
        lvp=bool(s.lv and s.lv.paused)
        return dict(enabled=ENABLED,active=s.active(),liveEnabled=bool(s.active() and not lvp and s.lv is not None and s.lv.allowed()),test=TEST_ID,
                    liveStrategies=sorted(LIVE_STRATEGIES),used=s.used(),max=(MAX_TRADES if _has_cap() else None),
                    autoDisabled=s.disabled(),usd=USD,rules=sorted(RULES),pairs=sorted(PAIRS),
                    moogleGate=dict(bar=MOOGLE_BAR,staleSec=MOOGLE_STALE_SEC,rule='crypto only: enter only if MoogleEdge shadow p for the traded side is >= 0.65; skip if missing/stale (>60s). Metals have no MoogleEdge gate.'),
                    metals=dict(hours='6:00–17:00 ET weekdays',minGap=METAL_MIN_GAP,moogle=False,closures='Kalshi 5–6 PM ET daily, Fri 5 PM–Sun 6 PM ET'),
                    midnightSkip=True,
                    exit=exit_mode(),
                    exitConfig=dict(mode=EXIT[0],armC=round(EXIT[1]*100) if EXIT[1] is not None else None,givebackC=round(EXIT[2]*100) if EXIT[2] is not None else None,
                                    sellTries=SELL_TRIES,sellPricing='reduce-only IOC at live bid -2c, -4c, -6c (3 per try)',noSellLastSec=NO_SELL_LAST_SEC,fallback='hold to settlement'),
                    exitStats=dict(s.xstat),previousTests=list(getattr(s.lv,'gapLivePrev',None) or []) if s.lv is not None else [],
                    thisTest=dict(trades=len(cur),closed=len(cc),trailExits=sum(1 for t in cc if t.get('status')=='exited'),wins=sum(1 for t in cc if (t.get('pnl') or 0)>0),
                                  losses=sum(1 for t in cc if (t.get('pnl') or 0)<=0),pnl=round(sum(t.get('pnl') or 0 for t in cc),2)),
                    liveEngine=dict(envAllow=bool(s.lv and s.lv.allowed()),paused=bool(s.lv and s.lv.paused),killSwitch=bool(s.lv and s.lv.killed)),
                    stats=dict(s.stat),recentAttempts=list(s.recent)[::-1],recentOrders=s.order_log(),pendingRetries={k:dict(n=v['n'],age=round(time.time()-v['first'],1)) for k,v in list(s.pending.items())},wsQuoteFresh=s.ws_fresh(),open=[t for t in lt if t.get('status')=='open'],closed=cl,
                    results=dict(trades=len(lt),closed=len(cl),wins=sum(1 for t in cl if (t.get('pnl') or 0)>0),
                                 losses=sum(1 for t in cl if (t.get('pnl') or 0)<=0),pnl=round(sum(t.get('pnl') or 0 for t in cl),2)))
