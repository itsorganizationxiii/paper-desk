"""Pairing-gap SHADOW strategy (paper only). Never touches pt.trades, live, or the Spec coin method.
Rule: same 15m window, |gapDown|>=GAP_MIN, leftSec in [GAP_MIN_LEFT,GAP_MAX_LEFT]. Leader = leg further from 50%
(and >= GAP_LEAD_MIN from 50%). Bet the LAGGING leg in the leader's direction at its ask, $1 stake.
Gates: ask 10-90c, laggard yes spread <=5c when known; fee ceil(7*c*p*(1-p))/100. One per pair per window."""
import json, math, os, threading, time
GAP_MIN=float(os.environ.get('GAP_SHADOW_MIN','0.20'))
GAP_MIN_LEFT=float(os.environ.get('GAP_SHADOW_MIN_LEFT','120'))
GAP_MAX_LEFT=float(os.environ.get('GAP_SHADOW_MAX_LEFT','720'))
GAP_LEAD_MIN=float(os.environ.get('GAP_SHADOW_LEAD_MIN','0.15'))
ENABLED=os.environ.get('GAP_SHADOW','1')!='0'
def _pairs(v):
    out=[]
    for x in (v or '').split(','):
        x=x.strip().upper()
        if '-' in x:
            a,b=[y.strip() for y in x.split('-',1)]
            if a and b and a!=b: out.append((a,b))
    return tuple(out)
# User-specified pairs ONLY (no cross-mixing): BTC-ETH, GOLD-SILVER, SOL-XRP. Env GAP_SHADOW_PAIRS overrides.
PAIRS=_pairs(os.environ.get('GAP_SHADOW_PAIRS','BTC-ETH,GOLD-SILVER,SOL-XRP')) or (('BTC','ETH'),('GOLD','SILVER'),('SOL','XRP'))
STAKE=1.0
TRADES=[]; _seen=set(); _lk=threading.Lock(); _path=[None]
def fee(c,p): return math.ceil(7*c*p*(1-p))/100
def _mid(m):
    try:
        b,a=m.get('yesBid'),m.get('yesAsk')
        if b and a: return (float(b)+float(a))/2
        return float(a or b) if (a or b) else None
    except Exception: return None
def load(data_dir):
    _path[0]=os.path.join(data_dir,'gap_shadow.json')
    try:
        with open(_path[0]) as f: TRADES[:]=json.load(f)
        for t in TRADES: _seen.add((t['pair'],t['closesAt']))
    except Exception: pass
def _save():
    if not _path[0]: return
    try:
        tmp=_path[0]+'.tmp'
        with open(tmp,'w') as f: json.dump(TRADES,f,default=str)
        os.replace(tmp,_path[0])
    except Exception as e: print('gapshadow save err',e,flush=True)
def decide(a,b,ma,mb,now=None):
    """Return trade dict or None (pure; no side effects)."""
    now=time.time() if now is None else now
    if not ma or not mb or ma.get('close')!=mb.get('close') or not ma.get('ticker') or not mb.get('ticker'): return None
    left=ma['close']-now
    if not (GAP_MIN_LEFT<=left<=GAP_MAX_LEFT): return None
    ya,yb=_mid(ma),_mid(mb)
    if ya is None or yb is None: return None
    gap=(1-ya)-(1-yb)
    if abs(gap)<GAP_MIN: return None
    if abs(ya-0.5)>=abs(yb-0.5): lead,lag,yl,ml,mg=a,b,ya,mb,yb
    else: lead,lag,yl,ml,mg=b,a,yb,ma,ya
    if abs(yl-0.5)<GAP_LEAD_MIN: return None
    side='YES' if yl>0.5 else 'NO'
    ask=ml.get('yesAsk') if side=='YES' else ml.get('noAsk')
    try: ask=float(ask)
    except Exception: return None
    if not (0.10<=ask<=0.90): return None
    if ml.get('yesAsk') is not None and ml.get('yesBid') is not None and float(ml['yesAsk'])-float(ml['yesBid'])>0.05+1e-9: return None
    c0=STAKE/ask; ef=fee(c0,ask); contracts=round(max(STAKE-ef,0.01)/ask,3)
    return dict(id=None,mode='shadow',shadow=True,signalSource='gapshadow',pair=f'{a}-{b}',leader=lead,laggard=lag,
                ticker=ml['ticker'],leaderTicker=(ma if lead==a else mb)['ticker'],side=side,entry=round(ask,4),stake=STAKE,
                contracts=contracts,entryFee=ef,gapDown=round(gap,4),leaderYesMid=round(yl,4),laggardYesMid=round(mg,4),
                leftSec=round(left,1),closesAt=ma['close'],openedAt=now,ts=now,status='open',settle=None,exit=None,pnl=None,hold_pnl=None)
def on_tick(mkts,now=None):
    """mkts: {asset: kalshi mk() dict}. Opens at most one gap shadow per pair per window."""
    if not ENABLED: return []
    out=[]
    try:
        for a,b in PAIRS:
            try:
                ma,mb=mkts.get(a),mkts.get(b)
                if not ma or not mb: continue
                key=(f'{a}-{b}',ma.get('close'))
                if key in _seen: continue
                t=decide(a,b,ma,mb,now)
                if not t: continue
                with _lk:
                    _seen.add(key); t['id']=f'G{len(TRADES)+1}'; TRADES.append(t)
                out.append(t); print('GAPSHADOW open',t['id'],t['pair'],t['laggard'],t['side'],t['entry'],t['gapDown'],flush=True)
            except Exception as e: print('gapshadow pair err',e,flush=True)
        if out: _save()
    except Exception as e: print('gapshadow err',e,flush=True)
    return out
def settle(result_fn,now=None):
    """result_fn(ticker)->'yes'|'no'|None. Hold to settlement."""
    now=time.time() if now is None else now; ch=False
    for t in [t for t in TRADES if t.get('status')=='open' and now>=t['closesAt']+5][:6]:
        try:
            r=result_fn(t['ticker'])
            if r not in ('yes','no'): continue
            win=(r=='yes')==(t['side']=='YES')
            pnl=round((t['contracts'] if win else 0)-t['stake'],2)
            t.update(status='won' if win else 'lost',settle=r,exit=1.0 if win else 0.0,pnl=pnl,hold_pnl=pnl,closedAt=now); ch=True
        except Exception as e: print('gapshadow settle err',e,flush=True)
    if ch: _save()
    return ch
def snapshot():
    with _lk: return [dict(t) for t in TRADES]
