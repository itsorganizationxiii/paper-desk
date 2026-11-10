"""Offline checks for gap-live session #4 (owner t1164u): metals hours/closures, |gap|>=0.06,
no Moogle gate for metals, Moogle gate still on crypto, midnight skip, no cap. No network, no real orders."""
import os, time, importlib, tempfile
from datetime import datetime, timedelta
from paper_tz import ET
os.environ.pop('LIVE_STRATEGIES',None); os.environ['LIVE_TRADING']='0'
import gaplive as GL, moogle as M, gapshadow as G
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
def ts(y,m,d,h,mi=0,s=0):
    return datetime(y,m,d,h,mi,s,tzinfo=ET).timestamp()
# Tuesday Nov 10 2026 is a weekday
DAY=ts(2026,11,10,10,22)
NIGHT=ts(2026,11,10,2,0)
MIDNIGHT=ts(2026,11,10,0,30)
PM17=ts(2026,11,10,17,0)
PM1659=ts(2026,11,10,16,59)
FRI17=ts(2026,11,13,17,0)   # Friday
SAT=ts(2026,11,14,12,0)
SUN17=ts(2026,11,15,17,0)
SUN18=ts(2026,11,15,18,0)
MON5=ts(2026,11,9,5,59)
MON6=ts(2026,11,9,6,0)

class FakeLV:
    def __init__(s,enabled=True,fill=1.0):
        s.on=enabled; s.calls=[]; s.saved=[]; s.gapLiveUsed=0; s.gapLiveDisabled=False; s.paused=not enabled; s.killed=False; s.fill=fill
    def enabled(s): return s.on
    def allowed(s): return True
    def buy(s,asset,tk,side,frac,ref,**kw):
        s.calls.append(dict(asset=asset,tk=tk,side=side,ref=ref,**kw))
        return None if s.fill is None else dict(contracts=s.fill,price=0.5,fee=0.02,cost=round(s.fill*0.5,4),orderId=f'o{len(s.calls)}',cash=20,frac=frac,side=side)
    def persist(s): s.saved.append((s.gapLiveUsed,s.gapLiveDisabled))

opened=[]
def opener(p,fill,strike):
    t=dict(id=len(opened)+1,gapLive=True,mode='live',status='open',pnl=None,tag='gap-live',strike=strike,asset=p.get('asset'),pair=p.get('pair')); opened.append(t); return t

def P(rule='A',pair='BTC-ETH',asset='BTC',close=10_000,side='YES',gap=0.08,**extra):
    return dict(id='GP1',mode='paper',shadow=False,gap=True,rule=rule,pair=pair,asset=asset,laggard=asset,
                ticker=f'KX{asset}15M-X{close}',side=side,price=0.5,entry=0.5,stake=1.0,conf='normal',
                closesAt=close,gapDown=gap,**extra)
def Mp(asset='SILVER',gap=0.08,close=10_000,side='YES',**extra):
    return P(pair='GOLD-SILVER',asset=asset,close=close,side=side,gap=gap,**extra)
def warm(sig,p=0.70,age=0,now=None):
    now=DAY if now is None else now
    coin=sig.get('laggard') or sig.get('asset')
    yes=p if sig.get('side')=='YES' else (None if p is None else 1-p)
    if p is None: M.CUR.pop(coin,None); return now
    M.CUR[coin]=dict(ticker=sig['ticker'],t=now-age,p=yes)
    return now

# --- defaults ---
chk('session 4 defaults: test 4, no cap, $1, trail 15/10, pairs include metals',
    GL.TEST_ID=='4' and GL.MAX_TRADES==0 and not GL._has_cap() and GL.USD==1.0
    and GL.EXIT==('trail',0.15,0.10) and GL.PAIRS=={'BTC-ETH','SOL-XRP','GOLD-SILVER'}
    and GL.WATCH_ASSETS==('BTC','ETH','SOL','GOLD','SILVER') and GL.METAL_MIN_GAP==0.06)

# --- metals hours / Kalshi closures ---
chk('Tue 10:22 AM ET is metals daytime',GL.metals_daytime(DAY) and not GL.metals_session_closed(DAY) and not GL.midnight_hour(DAY))
chk('Tue 2 AM ET is not metals daytime (too early)',not GL.metals_daytime(NIGHT) and not GL.metals_session_closed(NIGHT))
chk('Mon 5:59 AM ET is not metals daytime',not GL.metals_daytime(MON5))
chk('Mon 6:00 AM ET is metals daytime',GL.metals_daytime(MON6))
chk('Tue 4:59 PM ET is metals daytime',GL.metals_daytime(PM1659) and not GL.metals_session_closed(PM1659))
chk('Tue 5:00 PM ET Kalshi metals closed (daily 5-6 PM)',GL.metals_session_closed(PM17) and not GL.metals_daytime(PM17))
chk('Fri 5:00 PM ET closed (weekend start)',GL.metals_session_closed(FRI17) and not GL.metals_daytime(FRI17))
chk('Saturday midday closed',GL.metals_session_closed(SAT) and not GL.metals_daytime(SAT))
chk('Sun 5 PM ET still closed (before Sun 6 PM reopen)',GL.metals_session_closed(SUN17) and not GL.metals_daytime(SUN17))
chk('Sun 6 PM ET Kalshi open but not weekday daytime',not GL.metals_session_closed(SUN18) and not GL.metals_daytime(SUN18))
# match server.metals_closed source
src=open('server.py').read()
chk('server still documents Kalshi metals closures 5-6 PM ET / Fri 5 PM-Sun 6 PM ET',
    'daily 17:00-18:00 ET' in src and 'Fri 17:00 ET -> Sun 18:00 ET' in src)

lv=FakeLV(); g=GL.GapLive(lv,opener,log=lambda *a:None)
# metals in hours with |gap|>=0.06 — no Moogle needed
sig=Mp(gap=0.06); M.CUR.pop('SILVER',None)
chk('metals daytime gap=0.06 allowed with no Moogle cache',g.on_paper(sig,now=DAY) is not None and lv.calls and lv.calls[-1]['asset']=='SILVER' and lv.calls[-1]['usd']==1.0)
n=len(lv.calls)
for label,when,need in (
    ('night 2 AM',NIGHT,'daytime'),
    ('5 PM close',PM17,'closed' if False else 'daytime'),
    ('Saturday',SAT,'daytime'),
    ('Sun 6 PM (open but not weekday day)',SUN18,'daytime'),
):
    chk(f'metals skipped at {label}',g.on_paper(Mp(close=11_000+int(when)%1000,gap=0.10),now=when) is None and len(lv.calls)==n and 'daytime' in g.stat['lastReason'])

# --- metals |gap|>=0.06 ---
n=len(lv.calls)
chk('metals gap 0.059 skipped',g.on_paper(Mp(close=20000,gap=0.059),now=DAY) is None and len(lv.calls)==n and '0.06' in g.stat['lastReason'])
chk('metals gap missing treated as 0 and skipped',g.on_paper(Mp(close=20001,gap=0),now=DAY) is None and '0.06' in g.stat['lastReason'])
# negative gapDown (direction) still qualifies on absolute value
chk('metals |gapDown| 0.08 (negative) allowed',g.on_paper(Mp(close=20002,gap=-0.08),now=DAY) is not None and len(lv.calls)==n+1)

# --- no Moogle gate for metals; Moogle still on crypto ---
# already traded metals with no Moogle cache above
sig=P(close=30000); M.CUR.pop('BTC',None)
n=len(lv.calls)
chk('crypto still skipped when Moogle p missing',g.on_paper(sig,now=DAY) is None and len(lv.calls)==n and 'missing/stale' in g.stat['lastReason'])
sig=P(close=30001); warm(sig,p=0.64,now=DAY)
chk('crypto still skipped when Moogle p < 0.65',g.on_paper(sig,now=DAY) is None and len(lv.calls)==n and 'moogle-below-bar' in g.stat['lastReason'])
sig=P(close=30002); warm(sig,p=0.65,now=DAY)
chk('crypto still allowed at Moogle p==0.65',g.on_paper(sig,now=DAY) is not None and len(lv.calls)==n+1)
# metals with a stale/missing Moogle still trade
n=len(lv.calls); M.CUR.pop('GOLD',None)
chk('metals allowed even if Moogle cache empty',g.on_paper(Mp(asset='GOLD',close=30003,gap=0.10),now=DAY) is not None and len(lv.calls)==n+1)
# crypto stale still skipped
sig=P(close=30004); warm(sig,p=0.80,age=61,now=DAY); n=len(lv.calls)
chk('crypto stale Moogle (>60s) still skipped',g.on_paper(sig,now=DAY) is None and len(lv.calls)==n and 'missing/stale' in g.stat['lastReason'])

# --- midnight skip (crypto AND metals) ---
sig=P(close=40000); warm(sig,p=0.80,now=MIDNIGHT); n=len(lv.calls)
chk('crypto skipped in midnight hour',g.on_paper(sig,now=MIDNIGHT) is None and len(lv.calls)==n and 'midnight' in g.stat['lastReason'])
chk('metals skipped in midnight hour',g.on_paper(Mp(close=40001,gap=0.12),now=MIDNIGHT) is None and len(lv.calls)==n and 'midnight' in g.stat['lastReason'])
# 01:00 ET is past midnight skip (but metals still too early)
ONE=ts(2026,11,10,1,0)
sig=P(close=40002); warm(sig,p=0.80,now=ONE)
chk('crypto allowed at 1:00 AM ET (past midnight skip)',g.on_paper(sig,now=ONE) is not None and len(lv.calls)==n+1)

# --- no cap / no auto-disable ---
lv2=FakeLV(); g2=GL.GapLive(lv2,opener,log=lambda *a:None)
for i in range(8):
    sig=P(close=50000+i); warm(sig,p=0.70,now=DAY)
    g2.on_paper(sig,now=DAY)
chk('8 crypto fills do not auto-disable',lv2.gapLiveUsed==8 and not lv2.gapLiveDisabled and g2.active() and not g2.disabled() and len(lv2.calls)==8)
# leftover disabled flag is ignored
lv2.gapLiveDisabled=True
sig=P(close=50020); warm(sig,p=0.70,now=DAY)
chk('leftover auto-disable flag does not block session 4',g2.on_paper(sig,now=DAY) is not None and g2.disabled() is False and g2.summary([])['max'] is None and g2.summary([])['autoDisabled'] is False)

# --- API / brief / pairs ---
sm=g.summary([])
chk('/api-shaped summary is session 4, uncapped, lists GOLD-SILVER, metals + midnight flags',
    sm['test']=='4' and sm['max'] is None and sm['autoDisabled'] is False and 'GOLD-SILVER' in sm['pairs']
    and sm['usd']==1.0 and sm['midnightSkip'] is True and sm['metals']['minGap']==0.06 and sm['metals']['moogle'] is False
    and 'crypto only' in sm['moogleGate']['rule'] and sm['moogleGate']['bar']==0.65)
br=g.brief()
chk('brief: test 4, max None, metals + midnight in gate text',br['test']=='4' and br['max'] is None and br['midnightSkip'] and 'metals' in br['gate'] and '00:00' in br['gate'] and 'MoogleEdge' in br['gate'])
src=open('index.html').read()
chk('dashboard shows no-cap wording when max is unset',"no cap until paused" in src and "Live test #${d.gapLive.test}" in src)

# --- GOLD-SILVER shadow opens live hook, not a paper trade ---
G=importlib.reload(G); G.load(tempfile.mkdtemp()); G.HIST.clear()
seen=[]; G.LIVE[0]=lambda p,m:seen.append(dict(p))
def mm(tk,yes,close,spread=0.02):
    # mid=yes, tight book, ask in 10-90
    b=round(yes-spread/2,4); a=round(yes+spread/2,4)
    return dict(ticker=tk,close=close,yesBid=b,yesAsk=a,noBid=round(1-a,4),noAsk=round(1-b,4))
C=8_000_000.0
# |gap| = |(1-0.30)-(1-0.40)| = 0.10 >= 0.06; SILVER is laggard if GOLD is further from 50
o=G.on_tick({'GOLD':mm('KXGOLD15M-T',0.30,C),'SILVER':mm('KXSILVER15M-T',0.40,C)},C-400)
chk('A GOLD-SILVER still not added to the paper book',any(t['pair']=='GOLD-SILVER' for t in o) and not any(t.get('pair')=='GOLD-SILVER' for t in G.PAPER))
chk('A GOLD-SILVER shadow still calls the live hook with liveFromShadow',
    seen and seen[0]['pair']=='GOLD-SILVER' and seen[0].get('liveFromShadow') is True and seen[0].get('asset') in ('GOLD','SILVER')
    and seen[0].get('mode')=='paper' and seen[0].get('gap') is True and abs(float(seen[0].get('gapDown') or 0))>=0.06)
chk('paper stake / PAPER_PAIRS unchanged',G.PAPER_PAIRS=={'BTC-ETH','SOL-XRP'} and G.PAPER_STAKE==3.0 and G.STAKE==1.0)

# fix the last chk properly below
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
