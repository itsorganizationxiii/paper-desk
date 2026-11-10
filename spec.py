"""Spec-aligned helpers for Paper Desk v3 (staged, flag-gated).

Flags (env):
  SPEC_EDGE=1           decision edge after fee + half-spread + SPEC_MARGIN
  SPEC_GATES=1          hard 10-90c ask band and spread <=5c
  SPEC_MARGIN           safety margin dollars (default 0.02)
  PAPER_STOP_MODE       current|off|time|edgedecay (paper/shadow only; never live)
  PAPER_TIME_EXIT_SEC   unused for exits when mode=time (time = no price stop); kept for docs
  EXPERIMENTAL_SHADOW_ONLY  default 1: leadlag/ethlead/reversal/alt-consensus never live

Research logging is additive and never raises into the trading loop.
"""
import json, math, os, threading, time
from datetime import datetime
try:
    from zoneinfo import ZoneInfo; ET=ZoneInfo('America/New_York')
except Exception:
    from datetime import timezone, timedelta; ET=timezone(timedelta(hours=-5))

SPEC_EDGE=os.environ.get('SPEC_EDGE','1')=='1'
SPEC_GATES=os.environ.get('SPEC_GATES','1')=='1'
SPEC_MARGIN=float(os.environ.get('SPEC_MARGIN','0.02'))
PAPER_STOP_MODE=os.environ.get('PAPER_STOP_MODE','edgedecay').lower().strip() or 'current'
PAPER_TIME_EXIT_SEC=int(os.environ.get('PAPER_TIME_EXIT_SEC','120'))
EXPERIMENTAL_SHADOW_ONLY=os.environ.get('EXPERIMENTAL_SHADOW_ONLY','1')!='0'
SPEC_LOG=os.environ.get('SPEC_RESEARCH_LOG','1')!='0'
ASK_LO=0.10; ASK_HI=0.90; MAX_SPREAD=0.05
CORR_TIGHT=0.70
METAL_INDEX_NOTES={'GOLD':'Metal.Index.1OZGOLD/USD (Kalshi); closest public Pyth feed Metal.XAU/USD',
                   'SILVER':'Metal.Index.SILVER/USD (Kalshi); closest public Pyth feed Metal.XAG/USD'}
CRYPTO_PAIR=('BTC','ETH','SOL','XRP','DOGE')

def kalshi_fee_1(p):
    """Kalshi taker fee for 1 contract at price p (dollars)."""
    try: p=float(p)
    except Exception: return 0.0
    if not (0<p<1): return 0.0
    return math.ceil(7*1*p*(1-p))/100

def edge_breakdown(q, ask, bid=None, margin=None):
    """Return dict with raw / after-fee / after-halfspread / after-margin edges."""
    margin=SPEC_MARGIN if margin is None else float(margin)
    try:
        if q is None or ask is None: raise ValueError('missing')
        q=float(q); ask=float(ask)
    except Exception: return dict(edge_raw=None,edge_after_fee=None,edge_after_halfspread=None,edge_after_margin=None,fee=None,half_spread=None,spread=None)
    raw=q-ask
    fee=kalshi_fee_1(ask)
    spread=None; hs=0.0
    if bid is not None:
        try:
            bid=float(bid); spread=round(ask-bid,6); hs=max(0.0,spread/2.0)
        except Exception: spread=None; hs=0.0
    after_fee=raw-fee
    after_hs=after_fee-hs
    after_m=after_hs-margin
    return dict(edge_raw=round(raw,6),edge_after_fee=round(after_fee,6),
                edge_after_halfspread=round(after_hs,6),edge_after_margin=round(after_m,6),
                fee=round(fee,6),half_spread=round(hs,6),spread=spread)

def decision_edge(q, ask, bid=None, margin=None, use_spec=None):
    """Edge used for sizing/filters. Flags-off => raw q-ask."""
    bd=edge_breakdown(q,ask,bid,margin)
    if (SPEC_EDGE if use_spec is None else use_spec):
        return bd['edge_after_margin'], bd
    return bd['edge_raw'], bd

def gate_results(ask, bid=None):
    """Always compute gate results (for logging)."""
    try: ask=float(ask)
    except Exception: ask=None
    price_ok=ask is not None and ASK_LO<=ask<=ASK_HI
    spread=None; spread_ok=True
    if bid is not None and ask is not None:
        try: spread=round(float(ask)-float(bid),6); spread_ok=spread<=MAX_SPREAD+1e-12
        except Exception: spread=None; spread_ok=True
    return dict(ask=ask,bid=None if bid is None else float(bid),spread=spread,
                price_band_ok=price_ok,spread_ok=spread_ok,
                gates_ok=bool(price_ok and spread_ok),
                ask_lo=ASK_LO,ask_hi=ASK_HI,max_spread=MAX_SPREAD)

def gates_block(ask, bid=None, use_gates=None):
    """True => skip entry. Flags-off => never block."""
    if not (SPEC_GATES if use_gates is None else use_gates): return False
    g=gate_results(ask,bid)
    return not g['gates_ok']

def experimental_force_shadow(meta=None):
    """Force experimental path metadata to paper/shadow. Returns updated meta dict."""
    m=dict(meta or {})
    if not EXPERIMENTAL_SHADOW_ONLY: return m
    m['shadow']=True; m['mode']='shadow'; m['experimentalShadowForced']=True
    return m

def is_paperish(t):
    """Paper or shadow trade (not a real live fill)."""
    return not (t.get('mode')=='live' and not t.get('shadow'))

def hard_stop_hit(bid, entry, stop_frac):
    try: return bid>0 and bid<=float(stop_frac)*float(entry)
    except Exception: return False

def paper_stop_action(t, bid, stop_frac, model_q=None, ask=None, left_sec=None, now=None):
    """Decide paper/shadow stop behavior under PAPER_STOP_MODE.

    Returns (should_exit: bool, reason: str|None, counterfactual: dict).
    Live trades must never call this for decisions — caller must gate on is_paperish.
    """
    mode=PAPER_STOP_MODE
    hit=hard_stop_hit(bid,t.get('price'),stop_frac)
    cf=dict(currentHardStopWouldFire=bool(hit),paperStopMode=mode,
            stopFrac=stop_frac,bid=None if bid is None else round(float(bid),4),
            entry=t.get('price'),leftSec=None if left_sec is None else round(float(left_sec),1))
    if mode=='current':
        return (hit, f'stop: mark {float(bid):.3f} <= {float(stop_frac):.0%} of entry {t.get("price")}' if hit else None), cf
    if mode=='off':
        return (False, None), cf
    if mode=='time':
        # no price stop; other exits (TP/lock/flip/whale) still apply elsewhere
        return (False, None), cf
    if mode=='edgedecay':
        if model_q is None or ask is None:
            return (False, None), cf
        _, bd=decision_edge(model_q, ask, bid=bid, use_spec=True)
        decay=bd['edge_after_margin'] is not None and bd['edge_after_margin'] < -SPEC_MARGIN
        cf['edgeAfterMargin']=bd['edge_after_margin']
        if decay:
            return (True, f'edgedecay: after-cost edge {bd["edge_after_margin"]:+.4f} < -{SPEC_MARGIN}'), cf
        return (False, None), cf
    # unknown mode -> behave like current
    return (hit, f'stop: mark {float(bid):.3f} <= {float(stop_frac):.0%} of entry {t.get("price")}' if hit else None), cf

# ---- research logging (non-blocking JSONL) ----
_lock=threading.Lock()
_ret_cache=dict(at=0,rets={},corr=None,tag=None)
_path=None

def _log_path(data_dir=None):
    global _path
    if _path: return _path
    base=data_dir or os.environ.get('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
    os.makedirs(base,exist_ok=True)
    _path=os.path.join(base,'spec_research.jsonl')
    return _path

LOG_SINK=[None]  # fn(row): non-blocking persistence hook (server -> async Mongo); research only
def _sink(row):
    if LOG_SINK[0] is None: return
    try: LOG_SINK[0](row)
    except Exception: pass
def append_research(row, data_dir=None):
    if not SPEC_LOG: return
    if isinstance(row,dict) and row.get('kind')=='settlement': _sink(dict(row,ts_ms=row.get('ts_ms') or int(time.time()*1000)))
    try:
        row=dict(row); row.setdefault('ts_ms',int(time.time()*1000)); row.setdefault('loggedAt',time.time())
        p=_log_path(data_dir)
        line=json.dumps(row,separators=(',',':'),default=str)+'\n'
        with _lock:
            with open(p,'a') as f: f.write(line); f.flush()
    except Exception as e:
        try: print('spec research log err',type(e).__name__,flush=True)
        except Exception: pass

def time_regime_tags(ts=None):
    d=datetime.fromtimestamp(ts or time.time(),ET)
    tod='night' if (d.hour>=17 or d.hour<9) else 'day'
    wd=d.weekday()
    wk='weekend' if wd>=5 or (wd==4 and d.hour>=17) or (wd==0 and d.hour<9) else 'weekday'
    return dict(timeOfDay=tod,weekday=d.strftime('%a'),weekRegime=wk,hourEt=d.hour)

def vol_regime_tag(sd15):
    if sd15 is None: return 'unknown'
    try: s=float(sd15)
    except Exception: return 'unknown'
    if s<0.001: return 'low'
    if s<0.003: return 'mid'
    return 'high'

def corr_regime(pair_rets, threshold=CORR_TIGHT):
    """pair_rets: dict asset->15m return. Label tight if mean pairwise abs-corr of available >= threshold."""
    keys=[k for k in CRYPTO_PAIR if k in pair_rets and pair_rets[k] is not None]
    if len(keys)<2: return dict(corrRegime='unknown',meanAbsCorr=None,assets=keys,rets={k:pair_rets.get(k) for k in CRYPTO_PAIR})
    # single-period pairwise agreement proxy: fraction of pairs with same sign, plus magnitude
    same=0; tot=0; mags=[]
    for i in range(len(keys)):
        for j in range(i+1,len(keys)):
            a,b=pair_rets[keys[i]],pair_rets[keys[j]]
            tot+=1
            if a==0 or b==0: continue
            if (a>0)==(b>0): same+=1
            mags.append(abs(a*b)**0.5)
    agree=same/tot if tot else 0
    # rolling corr unavailable from one snapshot; use sign-agreement as regime proxy
    tag='tight' if agree>=threshold else 'loose'
    return dict(corrRegime=tag,signAgree=round(agree,3),meanAbsRet=round(sum(abs(pair_rets[k]) for k in keys)/len(keys),6),
                assets=keys,rets={k:pair_rets.get(k) for k in CRYPTO_PAIR})

def update_pair_returns(candle_map):
    """candle_map: asset -> list of [t,low,high,open,close,vol] newest-first (Coinbase shape) or close series.
    Stores last completed 15m return per asset."""
    rets={}
    for a,c in (candle_map or {}).items():
        try:
            if not c or len(c)<2: continue
            # Coinbase: c[0] current forming, c[1] prior completed open->... use prior close vs its open
            prior=c[1]
            if isinstance(prior,(list,tuple)) and len(prior)>=5:
                o,cl=float(prior[3]),float(prior[4]); rets[a]=round((cl-o)/o,6) if o else None
            else:
                rets[a]=None
        except Exception: rets[a]=None
    tag=corr_regime(rets)
    _ret_cache.update(at=time.time(),rets=rets,corr=tag.get('signAgree'),tag=tag.get('corrRegime'),detail=tag)
    return tag

def cached_pair_snapshot():
    return dict(pairRets=dict(_ret_cache.get('rets') or {}),corrRegime=_ret_cache.get('tag'),
                signAgree=_ret_cache.get('corr'),pairDetail=_ret_cache.get('detail'))

def flow_flags(cb):
    """Secondary CVD / whale / large-taker flags from existing coinbase()/metal() dict."""
    if not cb: return dict(cvdRatio=None,cvd=None,imbalance=None,largeTaker=False,whaleNet=None,whaleStrongDir=None)
    wh=cb.get('whale') or {}
    alerts=wh.get('alerts') or []
    return dict(cvdRatio=cb.get('cvdRatio'),cvd=cb.get('cvd'),
                imbalance=None,  # book imbalance not in current feed; reserved
                largeTaker=bool(alerts),
                whaleNet=wh.get('netFlowUsd'),whaleStrongDir=wh.get('strongDir'),whaleCount=wh.get('count'))

def decision_row(*, asset, ticker, side, model_p, source, yes_bid, yes_ask, no_bid, no_ask,
                 q, ask, bid, action, skip_reason=None, cb=None, extra=None):
    bd=edge_breakdown(q,ask,bid)
    g=gate_results(ask,bid)
    tags=time_regime_tags()
    vol=vol_regime_tag((cb or {}).get('sd15'))
    row=dict(kind='decision',asset=asset,ticker=ticker,side=side,model_p=model_p,source=source,
             yesBid=yes_bid,yesAsk=yes_ask,noBid=no_bid,noAsk=no_ask,
             bid=bid,ask=ask,spread=g['spread'],
             edge_raw=bd['edge_raw'],edge_after_fee=bd['edge_after_fee'],
             edge_after_halfspread=bd['edge_after_halfspread'],edge_after_margin=bd['edge_after_margin'],
             fee=bd['fee'],half_spread=bd['half_spread'],
             gate_price_ok=g['price_band_ok'],gate_spread_ok=g['spread_ok'],gates_ok=g['gates_ok'],
             action=action,skipReason=skip_reason,
             volRegime=vol,**tags,**flow_flags(cb),**cached_pair_snapshot())
    if extra: row.update(extra)
    return row

def settlement_row(*, asset, ticker, side, coinbase_spot, kalshi_result, kalshi_expiration_value,
                   pyth_price=None, settled_by=None, trade_id=None, fill_ts=None, opened_at=None, closed_at=None):
    gap=None; agree=None
    try:
        if kalshi_expiration_value is not None and coinbase_spot is not None:
            kv=float(kalshi_expiration_value); cs=float(coinbase_spot)
            gap=round(cs-kv,6)
            # directional agreement vs strike not always available; flag numeric proximity
            agree=abs(gap)<=max(1e-6,abs(kv)*0.001) if kv else None
    except Exception: pass
    row=dict(kind='settlement',asset=asset,ticker=ticker,side=side,tradeId=trade_id,
             coinbaseSpot=coinbase_spot,kalshiResult=kalshi_result,kalshiExpirationValue=kalshi_expiration_value,
             coinbaseVsCfGap=gap,coinbaseVsCfAgree=agree,settledBy=settled_by,
             fillTs=fill_ts,openedAt=opened_at,closedAt=closed_at or time.time())
    if asset in ('GOLD','SILVER'):
        row.update(pythMetalPrice=pyth_price,metalIndexNote=METAL_INDEX_NOTES.get(asset),
                   pythFeed='Metal.XAU/USD' if asset=='GOLD' else 'Metal.XAG/USD')
        try:
            if pyth_price is not None and kalshi_expiration_value is not None:
                row['pythVsKalshiGap']=round(float(pyth_price)-float(kalshi_expiration_value),6)
        except Exception: pass
    return row

# --- research only: gold vs silver intra-window gap (never read by trading code) ---
import collections as _col
METAL_GAP=_col.deque(maxlen=int(os.environ.get('METAL_GAP_RING','3000')))
def _mid(b,a):
    try:
        b=float(b) if b is not None else None; a=float(a) if a is not None else None
        if b and a: return round((b+a)/2,4)
        return a if a else b
    except Exception: return None
def metal_gap_row(g, s, now=None):
    """g/s: kalshi market dicts (mk()) for GOLD/SILVER. Returns row or None if not same window."""
    if not g or not s or g.get('close')!=s.get('close'): return None
    now=time.time() if now is None else now
    gy=_mid(g.get('yesBid'),g.get('yesAsk')); sy=_mid(s.get('yesBid'),s.get('yesAsk'))
    if gy is None or sy is None: return None
    gap=round((1-gy)-(1-sy),4)  # gold Down% minus silver Down%
    same=(gy>=0.5)==(sy>=0.5)
    return dict(kind='metal_gap',ts_ms=int(now*1000),goldTicker=g.get('ticker'),silverTicker=s.get('ticker'),
                closeAt=g.get('close'),leftSec=round(g['close']-now,1),goldYesMid=gy,silverYesMid=sy,
                goldDown=round(1-gy,4),silverDown=round(1-sy,4),gapDown=gap,sameSide=same,state='same' if same else 'diverging',
                goldYesBid=g.get('yesBid'),goldYesAsk=g.get('yesAsk'),silverYesBid=s.get('yesBid'),silverYesAsk=s.get('yesAsk'))
def log_metal_gap(g, s, now=None):
    try:
        r=metal_gap_row(g,s,now)
        if r: METAL_GAP.append(r); _sink(r); append_research(r)
        return r
    except Exception: return None

# --- research only: crypto pairwise intra-window gaps (never read by trading code) ---
CRYPTO_GAP=_col.deque(maxlen=int(os.environ.get('CRYPTO_GAP_RING','3000')))
CRYPTO_PAIRS=(('BTC','ETH'),('BTC','SOL'),('BTC','XRP'),('ETH','SOL'),('ETH','XRP'),('SOL','XRP'))
def crypto_gap_row(mkts, now=None):
    """mkts: {asset: kalshi market dict}. Logs pairs on the same window close."""
    now=time.time() if now is None else now
    mids={a:_mid(m.get('yesBid'),m.get('yesAsk')) for a,m in (mkts or {}).items() if m}
    if len(mids)<2: return None
    pairs={}
    for a,b in CRYPTO_PAIRS:
        ma,mb=mkts.get(a),mkts.get(b)
        if not ma or not mb or ma.get('close')!=mb.get('close') or mids.get(a) is None or mids.get(b) is None: continue
        same=(mids[a]>=0.5)==(mids[b]>=0.5)
        pairs[f'{a}-{b}']=dict(gapDown=round((1-mids[a])-(1-mids[b]),4),sameSide=same,state='same' if same else 'diverging')
    if not pairs: return None
    c=next(m.get('close') for m in mkts.values() if m)
    return dict(kind='crypto_gap',ts_ms=int(now*1000),closeAt=c,leftSec=round(c-now,1),
                yesMid={a:v for a,v in mids.items()},down={a:round(1-v,4) for a,v in mids.items() if v is not None},
                tickers={a:m.get('ticker') for a,m in mkts.items() if m},pairs=pairs,
                yesBid={a:m.get('yesBid') for a,m in mkts.items() if m},yesAsk={a:m.get('yesAsk') for a,m in mkts.items() if m},
                closes={a:m.get('close') for a,m in mkts.items() if m})
def log_crypto_gap(mkts, now=None):
    try:
        r=crypto_gap_row(mkts,now)
        if r: CRYPTO_GAP.append(r); _sink(r); append_research(r)
        return r
    except Exception: return None


def elo_entry_skip(est, model_p, asset):
    """Skip a new Spec entry when EloEdge p is unusable. Returns the skip action or None.
    Covers HTTP 402 / any EloEdge error (est != 'live') and stale / not-open / missing model_p.
    XRP has no EloEdge feed and may still use the coinbase-proxy path.
    Does not apply to already-open positions (exits and settlement stay unchanged)."""
    if model_p is not None or asset=='XRP':
        return None
    if est=='live':
        return 'skip: no fresh EloEdge model_p'
    return 'skip: eloedge-unavailable'

# Spec model_p: prefer fresh EloEdge, else MoogleEdge (same CUR cache /api/moogleedge current() uses).
# Stale rule matches gap live: missing or now-t > 60s => unusable. Paper-only caller decides entries.
SPEC_P_STALE_SEC=60.0

def moogle_yes_p(row, now=None, ticker=None, stale_sec=None):
    """YES-p from a MoogleEdge CUR / current() row. None if missing, stale (>60s), or wrong ticker."""
    stale_sec=SPEC_P_STALE_SEC if stale_sec is None else float(stale_sec)
    now=time.time() if now is None else now
    if not isinstance(row, dict) or row.get('p') is None: return None
    if ticker is not None and row.get('ticker') is not None and row.get('ticker')!=ticker: return None
    lag=None
    if row.get('t') is not None:
        try: lag=now-float(row['t'])
        except Exception: lag=None
    if lag is None and row.get('lagSec') is not None:
        try: lag=float(row['lagSec'])
        except Exception: lag=None
    if lag is None or lag>stale_sec: return None
    try: return float(row['p'])
    except Exception: return None

def moogle_yes_p_for(asset, ticker=None, now=None, stale_sec=None):
    """Read MoogleEdge YES-p from moogle.CUR (the cache /api/moogleedge exposes). None if missing/stale."""
    try:
        import moogle as M
        return moogle_yes_p(M.CUR.get(asset), now=now, ticker=ticker, stale_sec=stale_sec)
    except Exception:
        return None

def resolve_spec_p(elo_p, moogle_p):
    """Prefer fresh EloEdge p, else MoogleEdge p. Returns (p, pSource) with pSource 'eloedge'|'moogleedge'|None."""
    if elo_p is not None:
        try: return float(elo_p), 'eloedge'
        except Exception: pass
    if moogle_p is not None:
        try: return float(moogle_p), 'moogleedge'
        except Exception: pass
    return None, None

def spec_entry_skip(p, p_source=None):
    """Skip a new Spec entry when neither EloEdge nor MoogleEdge p is fresh. No Coinbase fallback."""
    if p is not None: return None
    return 'skip: moogleedge-unavailable'

def spec_p_label(p_source):
    return 'EloEdge' if p_source=='eloedge' else 'MoogleEdge'
