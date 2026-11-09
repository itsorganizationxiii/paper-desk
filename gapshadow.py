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
entry leftSec 60-780 (A/B/gap20; gap20 keeps its 120-720).
PAPER promotion (Nov 9 2026, owner-approved): Rule A on GAP_PAPER_PAIRS (default BTC-ETH,SOL-XRP) ALSO opens a real PAPER
trade in a separate gap paper book (PAPER, mode='paper', gap=True, signalSource='gap'), $1 stake, fresh-quote fill check,
held to settlement like the shadow rule. Everything else (A GOLD-SILVER, B, C, gap20) stays shadow-only. The gap paper book
is never in pt.trades, so it cannot change Spec decisions/sizing/stops or the Spec paper balance, and this module has no
order path at all (live can never take a gap trade). Persistence: SINK hook (server wires it to async Mongo upserts)."""
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
PAPER_ENABLED=E('GAP_PAPER','1')!='0'
PAPER_RULES=set(x.strip() for x in E('GAP_PAPER_RULES','A').split(',') if x.strip())
PAPER_PAIRS=set(f'{a}-{b}' for a,b in _pairs(E('GAP_PAPER_PAIRS','BTC-ETH,SOL-XRP')))
PAPER_STAKE=float(E('GAP_PAPER_STAKE','1')); PAPER_SLIP=0.01
# Confidence sizing (Nov 9, owner-approved): strong = unanimous confirmation (agree==of, of>=GAP_STRONG_MIN_OF, DOGE incl.)
# AND |gap|>=GAP_STRONG_GAP AND GAP_STRONG_MIN_LEFT<=leftSec<=GAP_STRONG_MAX_LEFT -> GAP_PAPER_STRONG_STAKE, else GAP_PAPER_STAKE.
STRONG_STAKE=float(E('GAP_PAPER_STRONG_STAKE','3')); STRONG_GAP=float(E('GAP_STRONG_GAP','0.08'))
STRONG_MIN_OF=int(E('GAP_STRONG_MIN_OF','3')); STRONG_MIN_LEFT=float(E('GAP_STRONG_MIN_LEFT','120')); STRONG_MAX_LEFT=float(E('GAP_STRONG_MAX_LEFT','780'))
TRADES=[]; _seen=set(); _lk=threading.Lock(); _path=[None]
PAPER=[]; _pseen=set()
QUOTE=[None]   # fn(ticker)->{'yesBid','yesAsk'} fresh quote (server: websocket cache) or None
SINK=[None]    # fn(trade) persistence hook; must be non-blocking
LIVE=[None]    # fn(paper_trade,laggard_market) gap-LIVE hook (gaplive.GapLive.on_paper); hard-capped, Rule A only, never XRP
FILTER=[None]  # fn(trade,book) SHADOW-ONLY filter tagger (filtershadow); adds t['filters'] only, never blocks a trade
PSTAT=dict(opened=0,nofill=0,skipped=0)
# SHADOW exit variants (Nov 9, research only): every gap trade (shadow + paper) logs what each take-profit variant WOULD have
# done; the trade itself is untouched (still held to settlement / B cash-out). Env GAP_EXIT_VARIANTS, comma list of
#   trail:<arm>:<drop>  arm once our bid >= entry+arm, exit when bid <= peak-drop
#   tpat:<level>        exit when our bid >= level
#   tpplus:<c>          exit when our bid >= entry+c
#   time:<sec>          exit when leftSec <= sec and exit value (after fee) > stake
#   hold                never exits (hold-to-settle reference)
EXIT_VARIANTS_ENV=E('GAP_EXIT_VARIANTS','trail:0.15:0.10,tpat:0.75,time:360,hold')
# REAL gap PAPER exit (Nov 9, owner t1004): the gap PAPER book (never live) books a trailing take-profit: arm once our bid
# >= entry+ARM, sell at the bid (minus Kalshi fee) when bid <= peak-DROP; otherwise hold to settlement. Shadow book unchanged.
PAPER_EXIT_ENV=E('GAP_PAPER_EXIT','trail:0.15:0.10')
def _parse_variants(v):
    out={}
    for x in (v or '').split(','):
        p=[y.strip() for y in x.split(':') if y.strip()]
        try:
            if p[0]=='trail' and len(p)==3: out[f'trail{round(float(p[1])*100)}/{round(float(p[2])*100)}']=('trail',float(p[1]),float(p[2]))
            elif p[0]=='tpat' and len(p)==2: out[f'tp@{round(float(p[1])*100)}c']=('tpat',float(p[1]))
            elif p[0]=='tpplus' and len(p)==2: out[f'tp+{round(float(p[1])*100)}c']=('tpplus',float(p[1]))
            elif p[0]=='time' and len(p)==2: out[f'time{int(float(p[1]))}s']=('time',float(p[1]))
            elif p[0]=='hold' and len(p)==1: out['hold']=('hold',)
        except Exception: pass
    return out
VARIANTS=_parse_variants(EXIT_VARIANTS_ENV)
def _parse_paper_exit(v):
    p=[y.strip() for y in (v or '').split(':')]
    try:
        if p[0]=='trail' and len(p)==3: return ('trail',float(p[1]),float(p[2]))
    except Exception: pass
    return None   # 'hold' / anything else -> hold to settlement
PAPER_EXIT=_parse_paper_exit(PAPER_EXIT_ENV)
def paper_exit_mode(): return f'trail:+{round(PAPER_EXIT[1]*100)}c arm/{round(PAPER_EXIT[2]*100)}c giveback, else hold to settlement' if PAPER_EXIT else 'hold to settlement'
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
def is_paper(t): return bool(t.get('gap')) and t.get('mode')=='paper'
def key(t): return ('paper' if is_paper(t) else 'shadow',t.get('rule','gap20'),t.get('pair'),round(float(t.get('closesAt') or 0),3))
def doc_id(t):
    b,r,p,c=key(t); return f'{b}|{r}|{p}|{c:.3f}'
def _rank(t):
    st=t.get('status'); return (st not in ('open',None), t.get('hold_pnl') is not None, t.get('closedAt') or 0)
def _reindex():
    TRADES.sort(key=lambda t:(t.get('openedAt') or 0)); PAPER.sort(key=lambda t:(t.get('openedAt') or 0))
    for i,t in enumerate(TRADES,1): t['id']=f'G{i}'
    for i,t in enumerate(PAPER,1): t['id']=f'GP{i}'
    _seen.clear(); _seen.update((t['rule'],t['pair'],t['closesAt']) for t in TRADES)
    _pseen.clear(); _pseen.update((t['rule'],t['pair'],t['closesAt']) for t in PAPER)
def merge_in(items):
    """Merge trades (from Mongo / seed / file) into TRADES/PAPER by key; a settled record beats an open one.
    Returns the list of merged-in records that were new or better (caller may persist them)."""
    out=[]
    with _lk:
        cur={key(t):t for t in TRADES+PAPER}
        for t in items or []:
            try:
                if not isinstance(t,dict) or not t.get('pair') or not t.get('closesAt'): continue
                t={k:v for k,v in t.items() if k not in ('_id','openedAtDt')}; t.setdefault('rule','gap20')
                k=key(t); o=cur.get(k)
                if o is None:
                    (PAPER if is_paper(t) else TRADES).append(t); cur[k]=t; out.append(t)
                elif _rank(t)>_rank(o):
                    o.clear(); o.update(t); out.append(o)
            except Exception as e: print('gapshadow merge err',e,flush=True)
        _reindex()
    return out
def load(data_dir):
    _path[0]=os.path.join(data_dir,'gap_shadow.json')
    for fn in (_path[0],os.path.join(data_dir,'gap_paper.json')):
        try:
            with open(fn) as f: merge_in(json.load(f))
        except Exception: pass
def _emit(t):
    if SINK[0] is None: return
    try: SINK[0](t)
    except Exception as e: print('gapshadow sink err',e,flush=True)
def _save():
    if not _path[0]: return
    for fn,rows in ((_path[0],TRADES),(os.path.join(os.path.dirname(_path[0]),'gap_paper.json'),PAPER)):
        try:
            tmp=fn+'.tmp'
            with open(tmp,'w') as f: json.dump(rows,f,default=str)
            os.replace(tmp,fn)
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
def _add(t,out,m=None):
    k=(t['rule'],t['pair'],t['closesAt'])
    if k in _seen: return
    with _lk:
        _seen.add(k); t['id']=f'G{len(TRADES)+1}'; TRADES.append(t)
    _ftag(t,'shadow')
    out.append(t); print('GAPSHADOW open',t['id'],t['rule'],t['pair'],t['laggard'],t['side'],t['entry'],flush=True); _emit(t)
    try: p=paper_open(t,m)
    except Exception as e: p=None; print('gappaper open err',e,flush=True)
    if p and LIVE[0] is not None:
        try:
            LIVE[0](p,m)
            if p.get('liveTwinId') is not None: _emit(p)
        except Exception as e: print('gaplive hook err',e,flush=True)
def _ftag(t,book):
    if FILTER[0] is None: return
    try: FILTER[0](t,book)
    except Exception as e: print('gap filter tag err',e,flush=True)
def confidence(st,now=None):
    """'strong' or 'normal' for a Rule A signal (pure)."""
    try:
        cf=st.get('confirm') or {}
        ag,of=int(cf.get('agree') or 0),int(cf.get('of') or 0)
        left=float(st['closesAt'])-float(st.get('openedAt') if now is None else now)
        if of>=STRONG_MIN_OF and ag==of and abs(float(st.get('gapDown') or 0))>=STRONG_GAP-1e-9 and STRONG_MIN_LEFT<=left<=STRONG_MAX_LEFT: return 'strong'
    except Exception: pass
    return 'normal'
def paper_open(st,m=None,now=None):
    """Promote a shadow trade to a real PAPER trade (separate gap paper book). Never live. Returns trade or None."""
    if not PAPER_ENABLED or st.get('rule') not in PAPER_RULES or st.get('pair') not in PAPER_PAIRS: return None
    k=(st['rule'],st['pair'],st['closesAt'])
    if k in _pseen: return None
    now=st.get('openedAt') if now is None else now
    side=st['side']; ask=st['entry']; fq=None
    if QUOTE[0] is not None:
        try: fq=QUOTE[0](st['ticker'])
        except Exception: fq=None
    fa=None
    if fq and fq.get('yesAsk') is not None and fq.get('yesBid') is not None:
        fa=_f(fq['yesAsk']) if side=='YES' else round(1-_f(fq['yesBid']),4)
    if fa is not None:
        if fa>ask+PAPER_SLIP+1e-9: PSTAT['nofill']+=1; print('GAPPAPER NOFILL',st['ticker'],side,ask,fa,flush=True); return None
        ask=fa
    if not (0.10<=ask<=0.90): PSTAT['skipped']+=1; return None
    conf=confidence(st,now)
    stake=STRONG_STAKE if conf=='strong' else PAPER_STAKE
    c0=stake/ask; ef=fee(c0,ask); contracts=round(max(stake-ef,0.001)/ask,4)
    t=dict(id=None,mode='paper',shadow=False,gap=True,book='gap-paper',signalSource='gap',rule=st['rule'],pair=st['pair'],
           asset=st['laggard'],laggard=st['laggard'],leader=st.get('leader'),ticker=st['ticker'],side=side,price=round(ask,4),entry=round(ask,4),
           signalAsk=st['entry'],freshAsk=fa,stake=stake,conf=conf,tag='gap-paper-strong' if conf=='strong' else 'gap-paper',contracts=contracts,entryFee=ef,leftSec=st.get('leftSec'),closesAt=st['closesAt'],
           openedAt=now,status='open',settle=None,exit=None,pnl=None,hold_pnl=None,shadowKey=doc_id(st),
           gapDown=st.get('gapDown'),leaderYesMid=st.get('leaderYesMid'),laggardYesMid=st.get('laggardYesMid'),confirm=st.get('confirm'))
    with _lk:
        _pseen.add(k); t['id']=f'GP{len(PAPER)+1}'; PAPER.append(t)
    _ftag(t,'paper')
    PSTAT['opened']+=1; PSTAT['strong' if conf=='strong' else 'normal']=PSTAT.get('strong' if conf=='strong' else 'normal',0)+1
    print('GAPPAPER open',t['id'],t['rule'],t['pair'],t['asset'],side,t['price'],conf,stake,flush=True); _emit(t)
    return t
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
            t.update(status='cashed',exit=round(bid,4),exitFee=xf,pnl=round(val-t['stake'],2),cashedAt=now,cashLeftSec=round(left,1)); out.append(t); _emit(t)
            print('GAPSHADOW cash',t['id'],bid,t['pnl'],flush=True)
        except Exception as e: print('gapshadow exit err',e,flush=True)
    return out
def _our_bid(t,m):
    if t['side']=='YES': return _f(m.get('yesBid'))
    a=_f(m.get('yesAsk')); return None if a is None else round(1-a,4)
def _vval(t,bid):
    c=t['contracts']; return round(c*bid-fee(c,bid)-t['stake'],4)
def track_variants(mkts,now=None):
    """Update shadow exit variants on every open/pending gap trade. Never changes the trade's own status/pnl."""
    if not VARIANTS: return []
    now=time.time() if now is None else now; hit=[]
    for t in TRADES+PAPER:
        try:
            if now>=t['closesAt'] or t.get('settle') is not None: continue
            m=mkts.get(t.get('laggard') or t.get('asset'))
            if not m or m.get('ticker')!=t['ticker']: continue
            bid=_our_bid(t,m)
            if bid is None or bid<=0: continue
            ev=t.setdefault('exitVariants',{}); left=round(t['closesAt']-now,1); changed=False
            for name,spec in VARIANTS.items():
                v=ev.setdefault(name,dict(status='open',peak=None))
                if v.get('status')!='open': continue
                v['peak']=round(max(v.get('peak') or 0,bid),4); go=False
                if spec[0]=='trail': go=v['peak']>=t['entry']+spec[1]-1e-9 and bid<=v['peak']-spec[2]+1e-9
                elif spec[0]=='tpat': go=bid>=spec[1]-1e-9
                elif spec[0]=='tpplus': go=bid>=t['entry']+spec[1]-1e-9
                elif spec[0]=='time': go=left<=spec[1] and _vval(t,bid)>0
                elif spec[0]=='hold': go=False
                if go: v.update(status='exited',exit=bid,leftSec=left,at=now,pnl=_vval(t,bid)); changed=True
            if changed: hit.append(t); _emit(t)
        except Exception as e: print('gap variant err',e,flush=True)
    return hit
def manage_paper_exits(mkts,now=None):
    """REAL exits for the gap PAPER book only (never live, never Spec): trailing take-profit per PAPER_EXIT.
    Exit price = our bid (fresh websocket quote when available, else this tick's quote); pnl = contracts*bid - fee - stake."""
    if not PAPER_EXIT: return []
    now=time.time() if now is None else now; out=[]; _,arm,drop=PAPER_EXIT
    for t in PAPER:
        try:
            if t.get('status')!='open' or now>=t['closesAt']: continue
            m=mkts.get(t.get('asset') or t.get('laggard'))
            if not m or m.get('ticker')!=t['ticker']: continue
            bid=_our_bid(t,m); src='tick'
            if QUOTE[0] is not None:
                try:
                    fq=QUOTE[0](t['ticker'])
                    if fq and fq.get('yesBid') is not None and fq.get('yesAsk') is not None:
                        bid=_f(fq['yesBid']) if t['side']=='YES' else round(1-_f(fq['yesAsk']),4); src='ws'
                except Exception: pass
            if bid is None or bid<=0: continue
            entry=float(t.get('price') or t['entry'])
            t['peakBid']=round(max(t.get('peakBid') or 0,bid),4)
            if t['peakBid']>=entry+arm-1e-9:
                if not t.get('trailArmedAt'): t['trailArmedAt']=now; _emit(t)
                if bid<=t['peakBid']-drop+1e-9:
                    xf=fee(t['contracts'],bid); pnl=round(t['contracts']*bid-xf-t['stake'],2)
                    t.update(status='exited',exit=round(bid,4),exitFee=xf,pnl=pnl,closedAt=now,exitLeftSec=round(t['closesAt']-now,1),exitQuote=src,
                             exitReason=f"trail: peak {t['peakBid']:.2f} (armed at entry {entry:.2f}+{arm:.2f}) -> bid {bid:.2f}; fee ${xf:.2f}")
                    PSTAT['trailExits']=PSTAT.get('trailExits',0)+1; out.append(t); _emit(t)
                    print('GAPPAPER trail exit',t['id'],t['pair'],bid,pnl,flush=True)
        except Exception as e: print('gappaper exit err',e,flush=True)
    return out
def _finish_variants(t,hp):
    ev=t.get('exitVariants')
    if not ev: return
    for v in ev.values():
        if v.get('status')=='open': v.update(status='held',pnl=hp,exit=None,leftSec=0)
def variant_summary(book=None):
    with _lk: ts=[_cp(t) for t in (TRADES if book=='shadow' else PAPER if book=='paper' else TRADES+PAPER)]
    out={}
    done=[t for t in ts if t.get('exitVariants') and t.get('hold_pnl') is not None]
    for name in VARIANTS:
        rows=[(t,t['exitVariants'].get(name)) for t in done if t['exitVariants'].get(name) and t['exitVariants'][name].get('pnl') is not None]
        p=[v['pnl'] for _,v in rows]; h=[t['hold_pnl'] for t,_ in rows]
        cum=pk=dd=0
        for x in p: cum+=x; pk=max(pk,cum); dd=max(dd,pk-cum)
        by={}
        for t,v in rows:
            g=by.setdefault(t.get('pair'),[0,0,0.0,0.0]); g[0]+=1; g[1]+=(v['pnl']>0); g[2]+=v['pnl']; g[3]+=t['hold_pnl']
        out[name]=dict(n=len(p),wins=sum(1 for x in p if x>0),losses=sum(1 for x in p if x<=0),exited=sum(1 for _,v in rows if v.get('status')=='exited'),
                       pnl=round(sum(p),2),holdPnl=round(sum(h),2),vsHold=round(sum(p)-sum(h),2),maxDD=round(dd,2),
                       byPair={k:dict(n=g[0],wins=g[1],pnl=round(g[2],2),holdPnl=round(g[3],2)) for k,g in by.items()})
    return dict(variants={k:list(v) for k,v in VARIANTS.items()},env=EXIT_VARIANTS_ENV,summary=out,
                tracking=sum(1 for t in ts if t.get('exitVariants') and t.get('hold_pnl') is None),
                note='shadow exit variants only: what each take-profit WOULD have done; trades themselves still hold to settlement')
def on_tick(mkts,now=None):
    """mkts: {asset: kalshi mk() dict}. Paper only."""
    if not ENABLED: return []
    now=time.time() if now is None else now
    out=[]
    try:
        mkts={k:v for k,v in (mkts or {}).items() if isinstance(v,dict)}
        record(mkts,now)
        cashed=manage_exits(mkts,now)
        try: track_variants(mkts,now)
        except Exception as e: print('gap variants err',e,flush=True)
        try: pexits=manage_paper_exits(mkts,now)
        except Exception as e: pexits=[]; print('gappaper exits err',e,flush=True)
        for a,b in PAIRS:
            for r,fn in (('A',lambda:decide_a(a,b,mkts,now)),('gap20',lambda:decide(a,b,mkts.get(a),mkts.get(b),now))):
                if r not in RULES: continue
                try:
                    ma=mkts.get(a)
                    if not ma or (r,f'{a}-{b}',ma.get('close')) in _seen: continue
                    t=fn()
                    if t: _add(t,out,mkts.get(t['laggard']))
                except Exception as e: print('gapshadow pair err',r,e,flush=True)
        for r,fn in (('B',decide_b),('C',decide_c)):
            if r not in RULES: continue
            try:
                mx=mkts.get('XRP')
                if not mx or (r,'XRP',mx.get('close')) in _seen: continue
                t=fn(mkts,now)
                if t: _add(t,out)
            except Exception as e: print('gapshadow xrp err',r,e,flush=True)
        if out or cashed or pexits: _save()
    except Exception as e: print('gapshadow err',e,flush=True)
    return out
def settle(result_fn,now=None):
    """result_fn(ticker)->'yes'|'no'|None. Open -> settle; cashed -> record hold_pnl only."""
    now=time.time() if now is None else now; ch=False
    todo=[t for t in TRADES if (t.get('status')=='open' or (t.get('status')=='cashed' and t.get('hold_pnl') is None)) and now>=t['closesAt']+5][:6]
    todo+=[t for t in PAPER if (t.get('status')=='open' or (t.get('status')=='exited' and t.get('hold_pnl') is None)) and now>=t['closesAt']+5][:6]
    for t in todo:
        try:
            r=result_fn(t['ticker'])
            if r not in ('yes','no'): continue
            win=(r=='yes')==(t['side']=='YES')
            hp=round((t['contracts'] if win else 0)-t['stake'],2)
            _finish_variants(t,hp)
            if t['status']=='cashed': t.update(settle=r,hold_pnl=hp,closedAt=now)
            elif t['status']=='exited': t.update(settle=r,hold_pnl=hp,settledAt=now)   # paper trail exit: keep booked pnl, record hold
            else: t.update(status='won' if win else 'lost',settle=r,exit=1.0 if win else 0.0,pnl=hp,hold_pnl=hp,closedAt=now)
            if is_paper(t): t['settledBy']='kalshi'
            ch=True; _emit(t)
        except Exception as e: print('gapshadow settle err',e,flush=True)
    if ch: _save()
    return ch
def _cp(t):
    d=dict(t)
    if isinstance(d.get('exitVariants'),dict): d['exitVariants']={k:dict(v) for k,v in list(d['exitVariants'].items())}
    return d
def snapshot():
    with _lk: return [_cp(t) for t in TRADES]
def paper_snapshot():
    with _lk: return [_cp(t) for t in PAPER]
def paper_summary():
    with _lk: ps=[dict(t) for t in PAPER]
    def agg(ts):
        c=[t for t in ts if t.get('status') not in ('open',None)]
        w=sum(1 for t in c if (t.get('pnl') or 0)>0)
        h=[t for t in c if t.get('hold_pnl') is not None]
        return dict(trades=len(ts),closed=len(c),open=len(ts)-len(c),wins=w,losses=len(c)-w,trailExits=sum(1 for t in c if t.get('status')=='exited'),staked=round(sum(t.get('stake') or 0 for t in c),2),pnl=round(sum(t.get('pnl') or 0 for t in c),2),
                    holdPnl=round(sum(t['hold_pnl'] for t in h),2),pnlVsHold=round(sum((t.get('pnl') or 0)-t['hold_pnl'] for t in h),2))
    by={}
    for t in ps: by.setdefault(f"{t.get('rule')} {t.get('pair')}",[]).append(t)
    tiers={k:agg([t for t in ps if (t.get('conf') or 'normal')==k]) for k in ('strong','normal')}
    return dict(enabled=PAPER_ENABLED,exitMode=paper_exit_mode(),exitEnv=PAPER_EXIT_ENV,rules=sorted(PAPER_RULES),pairs=sorted(PAPER_PAIRS),stake=PAPER_STAKE,strongStake=STRONG_STAKE,
                strongGate=dict(unanimousConfirm=True,minOf=STRONG_MIN_OF,minGap=STRONG_GAP,leftSec=[STRONG_MIN_LEFT,STRONG_MAX_LEFT]),
                all=agg(ps),byTier=tiers,byRulePair={k:agg(v) for k,v in by.items()},
                exitVariants=variant_summary('paper'),stats=dict(PSTAT),note='gap PAPER book: separate from Spec paper balance/ledger math; never live. Exits: '+paper_exit_mode()+'; exitVariants stay shadow comparisons')
