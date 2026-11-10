"""Offline checks for the late-window SHADOW (owner t1117u). Fake CF + quotes only: no network, no real orders, live untouched."""
import math, os, sys, tempfile, time, json, subprocess
import latewindow as L
import moogle as M

def chk(msg,ok):
    print(('OK  ' if ok else 'FAIL')+' '+msg)
    if not ok: raise SystemExit(1)

# ---- fair-value math (matches research-packet/backtest/bt.py) ----
chk('n_remain=0 locked above strike -> 1', L.fair_p_yes(100,99,1010,10,0,0.1,0)==1.0)
chk('n_remain=0 locked below strike -> 0', L.fair_p_yes(100,101,990,10,0,0.1,0)==0.0)
chk('n_remain=0 locked equal strike -> 0.5', L.fair_p_yes(100,100,1000,10,0,0.1,0)==0.5)
chk('ATM zero-delay p=0.5', abs(L.fair_p_yes(100,100,0,0,60,0.1,0)-0.5)<1e-12)
chk('ATM with 180s-style delay=120 still 0.5', abs(L.fair_p_yes(100,100,0,0,60,0.1,120)-0.5)<1e-12)
z=0.5/math.sqrt(20)
want=0.5*(1+math.erf(z/math.sqrt(2)))
got=L.fair_p_yes(100,99.5,0,0,60,1.0,0)
chk('known z for P=100 K=99.5 sigma=1 delay=0', abs(got-want)<1e-12)
# T=180: delay=120, n_remain=60, n_locked=0, e_s=P, std_s=sigma*sqrt(120+20)
z2=1.0/(0.2*math.sqrt(140)); want2=0.5*(1+math.erf(z2/math.sqrt(2)))
got2=L.fair_p_yes(101,100,0,0,60,0.2,120)
chk('T=180 delay=120 P=101 K=100 sigma=0.2', abs(got2-want2)<1e-12)
# tiny residual vol collapses to a step
chk('tiny vol above -> 1', L.fair_p_yes(100,99,0,0,60,1e-16,0)==1.0)
chk('tiny vol below -> 0', L.fair_p_yes(100,101,0,0,60,1e-16,0)==0.0)
chk('bad sigma -> None', L.fair_p_yes(100,100,0,0,60,0,0) is None)
chk('ncdf(0)=0.5 and symmetric', abs(L.ncdf(0)-0.5)<1e-12 and abs(L.ncdf(1)+L.ncdf(-1)-1)<1e-12)

# locked_parts before / inside the settlement window
def gp_const(px):
    return lambda t: px
lo,hi=L.settle_bounds(10_000)
chk('settle window is close-60..close-1 (60 ticks)', (lo,hi)==(10_000-60,10_000-1) and hi-lo+1==60)
sm,nl,nr,delay,ok=L.locked_parts(10_000,10_000-180,gp_const(100))
chk('T=180: n_locked=0 n_remain=60 delay=120', (sm,nl,nr,delay,ok)==(0.0,0,60,120,True))
sm,nl,nr,delay,ok=L.locked_parts(10_000,10_000-120,gp_const(100))
chk('T=120: n_locked=0 n_remain=60 delay=60', (sm,nl,nr,delay,ok)==(0.0,0,60,60,True))
sm,nl,nr,delay,ok=L.locked_parts(10_000,10_000-60,gp_const(100))
chk('T=60: first settlement tick locked, n_remain=59 delay=0', nl==1 and abs(sm-100)<1e-9 and nr==59 and delay==0 and ok)
sm,nl,nr,delay,ok=L.locked_parts(10_000,10_000-30,gp_const(100))
chk('T=30: 31 ticks locked, n_remain=29', nl==31 and nr==29 and delay==0 and ok and abs(sm-3100)<1e-9)

# fee matches other shadows
chk('fee per contract at 50c is 2c', L.fee(1.0,0.5)==0.02)

# ---- integration: fake Moogle series + quotes, no network ----
def _px(t, mid=100.0):
    # tiny 1s oscillation so sigma > 0; mean stays at mid so ATM fair stays ~0.5
    return mid + (0.05 if (t % 2) else -0.05)

def fill_series(coin, t0, t1, px):
    s=M.Series(coin,init='zero')
    for t in range(t0,t1+1):
        s.add(t, px(t) if callable(px) else _px(t, px))
    M.SER[coin]=s
    return s

def setup(now, close, strike=100.0, px=100.0, yesAsk=0.30, yesBid=0.29, coin='BTC', ticker='KXBTC15M-LW'):
    L.reset(); M.HTTP[0]=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('no network in latewindow tests'))
    fill_series(coin, min(int(close),int(now))-400, int(close)+5, px)
    L.set_window(coin, dict(ticker=ticker,strike=strike,close=close), now=now-200)
    L.QUOTE[0]=lambda tk: dict(yesBid=yesBid,yesAsk=yesAsk) if tk==ticker else None
    L.MARKET[0]=lambda tk: (_ for _ in ()).throw(RuntimeError('MARKET should not run when WS quote exists'))
    L.SINK[0]=None
    return ticker

NOW=1_800_000_000; CLOSE=NOW+60  # 60s left
tk=setup(NOW,CLOSE)
# ATM ~0.5 vs 30c ask -> YES edge ~20c
ch=L.on_tick({'BTC':dict(ticker=tk,strike=100.0,close=CLOSE,yesAsk=0.30,yesBid=0.29)}, now=NOW)
f=[o for o in ch if o['variant']==60]
chk('t60 fires at 60s left and fills YES', len(f)==1 and f[0]['status']=='filled' and f[0]['side']=='YES')
chk('records fair, ask, bid, spread, seconds left, paper fill',
    f[0]['fair'] is not None and f[0]['ask']==0.30 and f[0]['bid']==0.29 and f[0]['spread']==0.01
    and abs(f[0]['secondsLeft']-60)<1e-6 and f[0]['entry']==0.30 and f[0]['contracts']>0 and f[0]['entryFee']>=0)
chk('idempotent: second on_tick does not duplicate t60', L.on_tick({'BTC':dict(ticker=tk,strike=100.0,close=CLOSE,yesAsk=0.30,yesBid=0.29)}, now=NOW+1)==[])
# 180/120/30 are not due at exactly 60s left (180/120 already late-but-unseen? we saw window at now-200, so 180 and 120 are late)
lates=[o for o in L.snapshot() if o['status']=='skipped' and o.get('skipReason')=='late']
chk('missed 180 and 120 recorded as late because the window was known in time', {o['variant'] for o in lates}=={180,120})
chk('t30 not yet due at 60s left', all(o['variant']!=30 for o in L.snapshot() if o['status']=='filled'))

# t30 when 30s left, same market, still idempotent vs t60
L.QUOTE[0]=lambda tk: dict(yesBid=0.29,yesAsk=0.30)
ch30=L.on_tick({'BTC':dict(ticker=tk,strike=100.0,close=CLOSE,yesAsk=0.30,yesBid=0.29)}, now=CLOSE-30)
chk('t30 fills independently of t60', len(ch30)==1 and ch30[0]['variant']==30 and ch30[0]['status']=='filled' and ch30[0]['id']!=docid if (docid:=tk) else True)
chk('t30 and t60 are distinct docs', {o['id'] for o in L.snapshot() if o['status']=='filled'}=={L.doc_id(tk,60),L.doc_id(tk,30)})

# settle hold P&L
ok=L.settle(lambda ticker: 'yes', now=CLOSE+6)
fin=[o for o in L.snapshot() if o['status']=='filled' and o.get('settle')]
chk('both books settle YES and win (we bought YES)', ok and len(fin)==2 and all(o['win'] and o['pnl']>0 and o['settle']=='yes' for o in fin))
s=L.summary()
chk('api books report n/win%/pnl/cPer1 for t60 and t30', s['books']['t60']['n']==1 and s['books']['t30']['n']==1 and s['books']['t60']['winPct']==100 and s['books']['t60']['cPer1'] is not None)
chk('t180/t120 have n=0 (late skips do not count as trades)', s['books']['t180']['n']==0 and s['books']['t120']['n']==0 and s['books']['t180']['skipped']>=1)

# edge-below skip
L.reset()
tk=setup(NOW, NOW+180, yesAsk=0.49, yesBid=0.48)  # ATM fair ~0.5, edge ~1c < 10c
ch=L.on_tick({'BTC':dict(ticker=tk,strike=100.0,close=NOW+180,yesAsk=0.49,yesBid=0.48)}, now=NOW)
chk('t180 skips when fair-ask < 0.10', len(ch)==1 and ch[0]['variant']==180 and ch[0]['status']=='skipped' and ch[0]['skipReason']=='edge-below' and ch[0]['fair'] is not None)

# NO side when no-ask is cheap
L.reset()
tk=setup(NOW, NOW+120, yesAsk=0.80, yesBid=0.79)  # no_ask = 1-0.79=0.21, fair_no ~0.5, edge ~0.29
ch=L.on_tick({'BTC':dict(ticker=tk,strike=100.0,close=NOW+120,yesAsk=0.80,yesBid=0.79)}, now=NOW)
f=[o for o in ch if o['variant']==120]
chk('t120 fills NO when NO is the cheap side', len(f)==1 and f[0]['status']=='filled' and f[0]['side']=='NO' and f[0]['ask']==0.21)

# metals / unknown coins ignored
L.reset()
L.set_window('GOLD', dict(ticker='KXGOLD15M-X',strike=1,close=NOW+60), now=NOW-10)
chk('metals are not tracked', 'GOLD' not in L.WIN)
ch=L.on_tick({'GOLD':dict(ticker='KXGOLD15M-X',strike=1,close=NOW+60,yesAsk=0.2,yesBid=0.1)}, now=NOW)
chk('metals produce no late-window items', ch==[])

# no-cf skip when series is empty
L.reset(); L.SINK[0]=None
L.set_window('ETH', dict(ticker='KXETH15M-LW',strike=10,close=NOW+60), now=NOW-10)
M.SER['ETH']=M.Series('ETH',init='zero')
L.QUOTE[0]=lambda tk: dict(yesBid=0.2,yesAsk=0.21)
ch=L.evaluate('ETH',60,now=NOW)
chk('missing CF skips no-cf and does not fill', ch['status']=='skipped' and ch['skipReason']=='no-cf')

# one REST quote only when WS misses, and only once
L.reset()
tk=setup(NOW, NOW+60, coin='SOL', ticker='KXSOL15M-LW')
M.SER['SOL']=M.SER['BTC']
calls=[]
L.QUOTE[0]=lambda tk: None
def market(tk):
    calls.append(tk); return dict(yesBid=0.29,yesAsk=0.30,noBid=0.70,noAsk=0.71)
L.MARKET[0]=market
L.set_window('SOL', dict(ticker='KXSOL15M-LW',strike=100.0,close=NOW+60), now=NOW-10)
ch=L.evaluate('SOL',60,now=NOW)
chk('REST quote used once when WS is empty', ch['status']=='filled' and ch['quoteSrc']=='rest' and calls==['KXSOL15M-LW'] and L.STAT['quoteRest']==1)
ch2=L.evaluate('SOL',60,now=NOW+1)
chk('no second REST on the same variant', ch2 is None and calls==['KXSOL15M-LW'])

# settle NO loss
L.reset(); tk=setup(NOW,CLOSE,yesAsk=0.30,yesBid=0.29)
L.on_tick({'BTC':dict(ticker=tk,strike=100.0,close=CLOSE,yesAsk=0.30,yesBid=0.29)}, now=NOW)
L.settle(lambda ticker:'no', now=CLOSE+6)
o=[x for x in L.snapshot() if x['variant']==60 and x['status']=='filled'][0]
chk('losing fill records pnl = -1', o['win'] is False and o['pnl']==-1 and o['settle']=='no')

# persist merge + file load, sink gets a copy
L.reset()
got=[]
L.SINK[0]=got.append
td=tempfile.mkdtemp(); L.load(td)
tk=setup(NOW,CLOSE)  # setup resets SINK, restore it
L.SINK[0]=got.append
L.on_tick({'BTC':dict(ticker=tk,strike=100.0,close=CLOSE,yesAsk=0.30,yesBid=0.29)}, now=NOW)
chk('sink received the filled item', any(x.get('status')=='filled' and x.get('variant')==60 for x in got))
# force file save via settle
L.settle(lambda ticker:'yes', now=CLOSE+6)
L2_items=json.load(open(os.path.join(td,'gap_latewindow.json')))['items']
chk('file persist wrote gap_latewindow.json', any(x.get('variant')==60 and x.get('settle')=='yes' for x in L2_items))

# dashboard rows: one per variant
rs=L.brief_rows()
chk('dashboard has four rows t180 t120 t60 t30', len(rs)==4 and [r.split()[0] for r in rs]==['t180','t120','t60','t30'] and 'n=' in rs[2])
a=L.api()
chk('/api/latewindow shape: summary.books + items, shadowOnly', a['summary']['shadowOnly'] is True and set(a['summary']['books'])=={'t180','t120','t60','t30'} and isinstance(a['items'],list))

# no order / live surface in this module
src=open(L.__file__).read()
banned=("'/portfolio'","'/balance'","create_order","place_order","/api/status","live.status","live.balance")
chk('latewindow.py has no order or balance calls', all(b not in src for b in banned) and 'PAPER/SHADOW ONLY' in src)
chk('latewindow never imports live at module level', 'import live' not in src)

# isolation: do not touch live / other shadow books
try:
    df=subprocess.run(['git','diff','--name-only','HEAD','--','live.py','gaplive.py','kws.py','trader_lease.py','spec.py','moogle.py','gaplimit.py','gapshadow.py','gapregime.py','gapnight.py'],capture_output=True,text=True).stdout.strip()
    chk('no diff in live/Spec/MoogleEdge/limit/gap/regime/night code',df=='')
except Exception as e:
    chk('git diff check ran: '+str(e),False)

print('all latewindow checks passed')
