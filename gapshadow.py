"""Pairing-gap SHADOW strategy v2 (PAPER ONLY). Never touches pt.trades, live, or the Spec coin method.
Implements the owner's discretionary method as tagged rules (each trade has rule=...):
  A  'pair laggard'  : same 15m window, |gap|>=A_MIN_GAP between pair legs (BTC-ETH, SOL-XRP, GOLD-SILVER).
                       Leader = leg further from 50% (>=A_LEAD_MIN away). Buy the laggard in the leader's direction
                       while it's still cheaper. Crypto pairs need CONFIRMATION: strict majority of the other crypto
                       coins (BTC/ETH/SOL/XRP not in the pair, + DOGE when its Kalshi 15m market is present) on the
                       leader's side of 50%. Gold-silver: no confirmation. $1 stake.
  B  'XRP cheap catch-up': SOL Up>=74% and holding/rising (SOL mid >= mid ~30-60s ago - 2pt), XRP Up 20-58% and below
                       SOL, BTC Up>55%, XRP sporadic rise (XRP mid >= mid ~30-60s ago + 1pt). Buy XRP YES $1.
                       Exit: leftSec<=360 and every coin Up (>50%) and XRP<90% and the position is in profit after
                       exit fee -> cash out at XRP yes bid. Otherwise hold. hold_pnl always recorded at settlement.
  C  'XRP rare flip' : leftSec 30-360, the other coins (BTC/ETH/SOL [+DOGE]) all consolidating at 85-98% Up
                       (each moved <=5pt over ~60s), XRP Up<90%. Buy XRP YES with $0.10 stake; hold to settlement.
  gap20              : the old 20-pt gap rule, kept as a comparison variant.
Common gates: ask 10-90c, yes spread <=5c when known, fee ceil(7*c*p*(1-p))/100, one per rule per pair per window,
entry leftSec 60-780 (A/B/gap20; gap20 keeps its 120-720)."""
import collections, json, math, os, threading, time
E=os.environ.get
GAP_MIN=float(E('GAP_SHADOW_MIN','0.20'))           # gap20
GAP_MIN_LEFT=float(E('GAP_SHADOW_MIN_LEFT','120'))
GAP_MAX_LEFT=float(E('GAP_SHADOW_MAX_LEFT','720'))
GAP_LEAD_MIN=float(E('GAP_SHADOW_LEAD_MIN','0.15'))
A_MIN_GAP=float(E('GAP_A_MIN_GAP','0.03')); A_LEAD_MIN=float(E('GAP_A_LEAD_MIN','0.05'))
MIN_LEFT=60.0; MAX_LEFT=780.0
B_SOL_MIN=0.74; B_SOL_HOLD_TOL=0.02; B_XRP_LO=0.20; B_XRP_HI=0.58; B_BTC_MIN=0.55; B_XRP_RISE=0.01
EXIT_LEFT=360.0; XRP_TOP=0.90
C_LO=0.85; C_HI=0.98; C_CONSOL=0.05; C_MIN_LEFT=30.0; C_STAKE=0.10; C_MIN_OTHERS=3
LOOK_MIN=30.0; LOOK_MAX=60.0; HIST_SEC=300.0
ENABLED=E('GAP_SHADOW','1')!='0'
RULES=set(x.strip() for x in E('GAP_RULES','A,B,C,gap20').split(',') if x.strip())
CRYPTO=('BTC','ETH','SOL','XRP','DOGE')
def _pairs(v):
    out=[]
    for x in (v or '').split(','):
        x=x.strip().upper()
        if '-' in x:
            a,b=[y.strip() for y in x.split('-',1)]
            if a and b and a!=b: out.append((a,b))
    return tuple(out)
# User-specified pairs ONLY (no cross-mixing): BTC-ETH, GOLD-SILVER, SOL-XRP. Env GAP_SHADOW_PAIRS overrides.
PAIRS=_pairs(E('GAP_SHADOW_PAIRS','BTC-ETH,GOLD-SILVER,SOL-XRP')) or (('BTC','ETH'),('GOLD','SILVER'),('SOL','XRP'))
STAKE=1.0
TRADES=[]; _seen=set(); _lk=threading.Lock(); _path=[None]
HIST=collections.defaultdict(lambda:collections.deque(maxlen=64))  # asset -> (ts, close, yesMid)
def fee(c,p): return math.ceil(7*c*p*(1-p))/100
def _f(x):
    try: return float(x) if x is not None else None
    except Exception: return None
def _mid(m):
    if not m: return None
    b,a=_f(m.get('yesBid')),_f(m.get('yesAsk'))
    if b and a: return (b+a)/2
    return a or b or None
def load(data_dir):
    _path[0]=os.path.join(data_dir,'gap_shadow.json')
    try:
        with open(_path[0]) as f: TRADES[:]=json.load(f)
        for t in TRADES:
            t.setdefault('rule','gap20'); _seen.add((t['rule'],t['pair'],t['closesAt']))
    except Exception: pass
def _save():
    if not _path[0]: return
    try:
        tmp=_path[0]+'.tmp'
        with open(tmp,'w') as f: json.dump(TRADES,f,default=str)
        os.replace(tmp,_path[0])
    except Exception as e: print('gapshadow save err',e,flush=True)
def record(mkts,now):
    for a,m in (mkts or {}).items():
        y=_mid(m) if isinstance(m,dict) else None
        if y is None or not m.get('close'): continue
        h=HIST[a]
        if h and h[-1][1]!=m['close']: h.clear()
        h.append((now,m['close'],y))
        while h and now-h[0][0]>HIST_SEC: h.popleft()
def ago(a,now,lo=LOOK_MIN,hi=LOOK_MAX):
    """Mid from ~lo..hi seconds ago in the same window (newest qualifying point), else None."""
    for ts,_,y in reversed(HIST.get(a) or ()):
        if lo<=now-ts<=hi: return y
    return None
def _range(a,now,sec=60.0,cur=None):
    ys=[y for ts,_,y in (HIST.get(a) or ()) if now-ts<=sec]+([cur] if cur is not None else [])
    return (max(ys)-min(ys)) if len(ys)>=2 else None
def _mk_trade(rule,pair,asset,m,side,ask,stake,now,**extra):
    c0=stake/ask; ef=fee(c0,ask); contracts=round(max(stake-ef,0.001)/ask,4)
    left=m['close']-now
    t=dict(id=None,mode='shadow',shadow=True,signalSource='gapshadow',rule=rule,pair=pair,laggard=asset,ticker=m['ticker'],
           side=side,entry=round(ask,4),stake=stake,contracts=contracts,entryFee=ef,leftSec=round(left,1),closesAt=m['close'],
           openedAt=now,ts=now,status='open',settle=None,exit=None,exitFee=None,pnl=None,hold_pnl=None,cashedAt=None)
    t.update(extra); return t
def _ask_ok(m,side):
    ask=_f(m.get('yesAsk') if side=='YES' else m.get('noAsk'))
    if ask is None or not (0.10<=ask<=0.90): return None
    b,a=_f(m.get('yesBid')),_f(m.get('yesAsk'))
    if a is not None and b is not None and a-b>0.05+1e-9: return None
    return ask
def _lead(a,b,ya,yb):
    return (a,b,ya,yb) if abs(ya-0.5)>=abs(yb-0.5) else (b,a,yb,ya)
def decide(a,b,ma,mb,now=None):
    """gap20 (old rule). Return trade dict or None (pure)."""
    now=time.time() if now is None else now
    if not ma or not mb or ma.get('close')!=mb.get('close') or not ma.get('ticker') or not mb.get('ticker'): return None
    left=ma['close']-now
    if not (GAP_MIN_LEFT<=left<=GAP_MAX_LEFT): return None
    ya,yb=_mid(ma),_mid(mb)
    if ya is None or yb is None: return None
    gap=(1-ya)-(1-yb)
    if abs(gap)<GAP_MIN: return None
    lead,lag,yl,yg=_lead(a,b,ya,yb); ml=mb if lag==b else ma
    if abs(yl-0.5)<GAP_LEAD_MIN: return None
    side='YES' if yl>0.5 else 'NO'; ask=_ask_ok(ml,side)
    if ask is None: return None
    return _mk_trade('gap20',f'{a}-{b}',lag,ml,side,ask,STAKE,now,leader=lead,leaderTicker=(ma if lead==a else mb)['ticker'],
                     gapDown=round(gap,4),leaderYesMid=round(yl,4),laggardYesMid=round(yg,4))
def decide_a(a,b,mkts,now=None):
    """Rule A pair laggard with confirmation (pure apart from reading mkts)."""
    now=time.time() if now is None else now
    ma,mb=mkts.get(a),mkts.get(b)
    if not ma or not mb or ma.get('close')!=mb.get('close') or not ma.get('ticker') or not mb.get('ticker'): return None
    left=ma['close']-now
    if not (MIN_LEFT<=left<=MAX_LEFT): return None
    ya,yb=_mid(ma),_mid(mb)
    if ya is None or yb is None: return None
    gap=(1-ya)-(1-yb)
    if abs(gap)<A_MIN_GAP-1e-9: return None
    lead,lag,yl,yg=_lead(a,b,ya,yb); ml=mb if lag==b else ma
    if abs(yl-0.5)<A_LEAD_MIN-1e-9: return None
    side='YES' if yl>0.5 else 'NO'
    if not ((yg<yl) if side=='YES' else (yg>yl)): return None  # laggard still cheaper in leader's direction
    conf=None
    if a in CRYPTO and b in CRYPTO:
        others={x:_mid(mkts.get(x)) for x in CRYPTO if x not in (a,b) and mkts.get(x) and mkts[x].get('close')==ma['close']}
        others={x:y for x,y in others.items() if y is not None}
        if not others: return None
        agree=sum(1 for y in others.values() if (y>0.5 if side=='YES' else y<0.5))
        if agree*2<=len(others): return None
        conf=dict(agree=agree,of=len(others),others={x:round(y,4) for x,y in others.items()})
    ask=_ask_ok(ml,side)
    if ask is None: return None
    return _mk_trade('A',f'{a}-{b}',lag,ml,side,ask,STAKE,now,leader=lead,leaderTicker=(ma if lead==a else mb)['ticker'],
                     gapDown=round(gap,4),leaderYesMid=round(yl,4),laggardYesMid=round(yg,4),confirm=conf)
def decide_b(mkts,now=None):
    now=time.time() if now is None else now
    ms,mx,mb=mkts.get('SOL'),mkts.get('XRP'),mkts.get('BTC')
    if not ms or not mx or not mb or not mx.get('ticker'): return None
    c=mx.get('close')
    if ms.get('close')!=c or mb.get('close')!=c: return None
    left=c-now
    if not (MIN_LEFT<=left<=MAX_LEFT): return None
    ys,yx,yb=_mid(ms),_mid(mx),_mid(mb)
    if None in (ys,yx,yb): return None
    if not (ys>=B_SOL_MIN and B_XRP_LO<=yx<=B_XRP_HI and yx<ys and yb>B_BTC_MIN): return None
    s0,x0=ago('SOL',now),ago('XRP',now)
    if s0 is None or x0 is None: return None
    if ys<s0-B_SOL_HOLD_TOL or yx<x0+B_XRP_RISE-1e-9: return None
    ask=_ask_ok(mx,'YES')
    if ask is None: return None
    return _mk_trade('B','XRP',"XRP",mx,'YES',ask,STAKE,now,solYes=round(ys,4),sol60=round(s0,4),xrpYes=round(yx,4),xrp60=round(x0,4),btcYes=round(yb,4))
def decide_c(mkts,now=None):
    now=time.time() if now is None else now
    mx=mkts.get('XRP')
    if not mx or not mx.get('ticker'): return None
    c=mx.get('close'); left=c-now
    if not (C_MIN_LEFT<=left<=EXIT_LEFT): return None
    yx=_mid(mx)
    if yx is None or yx>=XRP_TOP: return None
    others={}
    for x in ('BTC','ETH','SOL','DOGE'):
        m=mkts.get(x)
        if m and m.get('close')==c and _mid(m) is not None: others[x]=_mid(m)
    if len(others)<C_MIN_OTHERS or any(x not in others for x in ('BTC','ETH','SOL')): return None
    for x,y in others.items():
        if not (C_LO<=y<=C_HI): return None
        r=_range(x,now,cur=y)
        if ago(x,now,LOOK_MIN,90.0) is None or r is None or r>C_CONSOL+1e-9: return None
    ask=_ask_ok(mx,'YES')
    if ask is None: return None
    return _mk_trade('C','XRP','XRP',mx,'YES',ask,C_STAKE,now,xrpYes=round(yx,4),others={x:round(y,4) for x,y in others.items()})
def _add(t,out):
    key=(t['rule'],t['pair'],t['closesAt'])
    if key in _seen: return
    with _lk:
        _seen.add(key); t['id']=f'G{len(TRADES)+1}'; TRADES.append(t)
    out.append(t); print('GAPSHADOW open',t['id'],t['rule'],t['pair'],t['laggard'],t['side'],t['entry'],flush=True)
def manage_exits(mkts,now=None):
    """Rule B early cash-out (paper). Returns list of trades cashed this tick."""
    now=time.time() if now is None else now; out=[]
    for t in TRADES:
        try:
            if t.get('rule')!='B' or t.get('status')!='open': continue
            mx=mkts.get('XRP')
            if not mx or mx.get('ticker')!=t['ticker']: continue
            left=t['closesAt']-now
            if left>EXIT_LEFT or left<=0: continue
            ys={x:_mid(mkts.get(x)) for x in CRYPTO if mkts.get(x) and mkts[x].get('close')==t['closesAt']}
            ys={x:y for x,y in ys.items() if y is not None}
            if 'XRP' not in ys or not all(x in ys for x in ('BTC','ETH','SOL')): continue
            if not all(y>0.5 for y in ys.values()) or ys['XRP']>=XRP_TOP: continue
            bid=_f(mx.get('yesBid'))
            if not bid: continue
            xf=fee(t['contracts'],bid); val=t['contracts']*bid-xf
            if val<=t['stake']: continue
            t.update(status='cashed',exit=round(bid,4),exitFee=xf,pnl=round(val-t['stake'],2),cashedAt=now,cashLeftSec=round(left,1)); out.append(t)
            print('GAPSHADOW cash',t['id'],bid,t['pnl'],flush=True)
        except Exception as e: print('gapshadow exit err',e,flush=True)
    return out
def on_tick(mkts,now=None):
    """mkts: {asset: kalshi mk() dict}. Paper only."""
    if not ENABLED: return []
    now=time.time() if now is None else now
    out=[]
    try:
        mkts={k:v for k,v in (mkts or {}).items() if isinstance(v,dict)}
        record(mkts,now)
        cashed=manage_exits(mkts,now)
        for a,b in PAIRS:
            for r,fn in (('A',lambda:decide_a(a,b,mkts,now)),('gap20',lambda:decide(a,b,mkts.get(a),mkts.get(b),now))):
                if r not in RULES: continue
                try:
                    ma=mkts.get(a)
                    if not ma or (r,f'{a}-{b}',ma.get('close')) in _seen: continue
                    t=fn()
                    if t: _add(t,out)
                except Exception as e: print('gapshadow pair err',r,e,flush=True)
        for r,fn in (('B',decide_b),('C',decide_c)):
            if r not in RULES: continue
            try:
                mx=mkts.get('XRP')
                if not mx or (r,'XRP',mx.get('close')) in _seen: continue
                t=fn(mkts,now)
                if t: _add(t,out)
            except Exception as e: print('gapshadow xrp err',r,e,flush=True)
        if out or cashed: _save()
    except Exception as e: print('gapshadow err',e,flush=True)
    return out
def settle(result_fn,now=None):
    """result_fn(ticker)->'yes'|'no'|None. Open -> settle; cashed -> record hold_pnl only."""
    now=time.time() if now is None else now; ch=False
    todo=[t for t in TRADES if (t.get('status')=='open' or (t.get('status')=='cashed' and t.get('hold_pnl') is None)) and now>=t['closesAt']+5][:6]
    for t in todo:
        try:
            r=result_fn(t['ticker'])
            if r not in ('yes','no'): continue
            win=(r=='yes')==(t['side']=='YES')
            hp=round((t['contracts'] if win else 0)-t['stake'],2)
            if t['status']=='cashed': t.update(settle=r,hold_pnl=hp,closedAt=now)
            else: t.update(status='won' if win else 'lost',settle=r,exit=1.0 if win else 0.0,pnl=hp,hold_pnl=hp,closedAt=now)
            ch=True
        except Exception as e: print('gapshadow settle err',e,flush=True)
    if ch: _save()
    return ch
def snapshot():
    with _lk: return [dict(t) for t in TRADES]
