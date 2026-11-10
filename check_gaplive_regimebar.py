"""Offline checks for gap-live session #4 against-trend bar + 55c ask floor.
No network, no real orders. Metals against-trend |gap|>=0.12; crypto MoogleEdge >=0.75.
Under-55c checks the live our-side ask before buy(), not just the signal price."""
import os, time
from datetime import datetime
from paper_tz import ET
os.environ.pop('LIVE_STRATEGIES',None); os.environ['LIVE_TRADING']='0'
import gaplive as GL, moogle as M
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
def ts(y,m,d,h,mi=0,s=0):
    return datetime(y,m,d,h,mi,s,tzinfo=ET).timestamp()
DAY=ts(2026,11,10,10,22)

class FakeLV:
    def __init__(s,enabled=True,fill=1.0,ask=0.60):
        s.on=enabled; s.calls=[]; s.saved=[]; s.gapLiveUsed=0; s.gapLiveDisabled=False
        s.paused=not enabled; s.killed=False; s.fill=fill; s.ask=ask
    def enabled(s): return s.on
    def allowed(s): return True
    def wsq(s,ticker):
        if s.ask is None: return None
        a=float(s.ask)
        # yesAsk=a and noAsk=a (yesBid=1-a) so either side's live ask is s.ask
        return dict(yesAsk=a,yesBid=round(max(0.01,min(0.99,1-a)),4))
    def buy(s,asset,tk,side,frac,ref,**kw):
        s.calls.append(dict(asset=asset,tk=tk,side=side,ref=ref,**kw))
        return None if s.fill is None else dict(contracts=s.fill,price=s.ask or 0.60,fee=0.02,cost=round(s.fill*(s.ask or 0.60),4),orderId=f'o{len(s.calls)}',cash=20,frac=frac,side=side)
    def persist(s): s.saved.append((s.gapLiveUsed,s.gapLiveDisabled))

opened=[]
def opener(p,fill,strike):
    t=dict(id=len(opened)+1,gapLive=True,mode='live',status='open',pnl=None,tag='gap-live',
           strike=strike,asset=p.get('asset'),pair=p.get('pair'),side=p.get('side')); opened.append(t); return t

def P(rule='A',pair='BTC-ETH',asset='BTC',close=10_000,side='YES',gap=0.08,price=0.60,**extra):
    return dict(id='GP1',mode='paper',shadow=False,gap=True,rule=rule,pair=pair,asset=asset,laggard=asset,
                ticker=f'KX{asset}15M-X{close}',side=side,price=price,entry=price,stake=1.0,conf='normal',
                closesAt=close,gapDown=gap,**extra)
def Mp(asset='SILVER',gap=0.08,close=10_000,side='YES',price=0.60,**extra):
    return P(pair='GOLD-SILVER',asset=asset,close=close,side=side,gap=gap,price=price,**extra)
def warm(sig,p=0.70,age=0,now=None):
    now=DAY if now is None else now
    coin=sig.get('laggard') or sig.get('asset')
    yes=p if sig.get('side')=='YES' else (None if p is None else 1-p)
    if p is None: M.CUR.pop(coin,None); return now
    M.CUR[coin]=dict(ticker=sig['ticker'],t=now-age,p=yes)
    return now
def set_reg(asset,reg,now=None):
    """Fill the 2h ring so asset_regime() returns bullish/bearish/unknown."""
    now=DAY if now is None else now
    GL._PX[asset].clear()
    if reg=='unknown': return
    ago=100.0; cur=90.0 if reg=='bearish' else 110.0
    GL.note_price(asset,ago,now-GL.REGIME_LOOKBACK)
    GL.note_price(asset,cur,now)

# --- config ---
chk('session 4 unchanged: test 4, no cap, $1, trail 15/10',
    GL.TEST_ID=='4' and GL.MAX_TRADES==0 and not GL._has_cap() and GL.USD==1.0 and GL.EXIT==('trail',0.15,0.10))
chk('regimeBar defaults: enabled, 2h lookback, crypto 0.75, metals |gap| 0.12',
    GL.REGIME_BAR_ENABLED and GL.REGIME_LOOKBACK==7200 and GL.REGIME_MOOGLE_BAR==0.75 and GL.REGIME_METAL_MIN_GAP==0.12)
chk('minEntryPrice is 0.55',GL.MIN_ENTRY==0.55)
chk('normal bars still 0.65 / 0.06',GL.MOOGLE_BAR==0.65 and GL.METAL_MIN_GAP==0.06)

# --- regime from 2h prices ---
GL.clear_prices()
chk('no history -> unknown',GL.asset_regime('GOLD',DAY)=='unknown' and GL.asset_regime('BTC',DAY)=='unknown')
set_reg('GOLD','bearish'); set_reg('BTC','bullish'); set_reg('SILVER','unknown')
chk('price below 2h ago is bearish',GL.asset_regime('GOLD',DAY)=='bearish')
chk('price above 2h ago is bullish',GL.asset_regime('BTC',DAY)=='bullish')
chk('missing 2h sample is unknown',GL.asset_regime('SILVER',DAY)=='unknown')
chk('against-trend: YES in bearish, NO in bullish, not the reverse or unknown',
    GL.against_trend('YES','bearish') and GL.against_trend('NO','bullish')
    and not GL.against_trend('NO','bearish') and not GL.against_trend('YES','bullish')
    and not GL.against_trend('YES','unknown') and not GL.against_trend('NO','unknown'))

# --- API shape ---
lv=FakeLV(); g=GL.GapLive(lv,opener,log=lambda *a:None)
sm=g.summary([])
chk('/api/gaplive exposes regimeBar and minEntryPrice 0.55',
    sm.get('minEntryPrice')==0.55 and isinstance(sm.get('regimeBar'),dict)
    and sm['regimeBar']['enabled'] is True and sm['regimeBar']['cryptoMoogleBar']==0.75
    and sm['regimeBar']['metalMinGap']==0.12 and sm['regimeBar']['lookbackSec']==7200
    and sm['regimeBar']['skipReason']=='against-trend-bar'
    and 'regimes' in sm['regimeBar'] and set(sm['regimeBar']['regimes'])==set(GL.WATCH_ASSETS)
    and sm['test']=='4' and sm['usd']==1.0)
br=g.brief()
chk('brief also exposes regimeBar + minEntryPrice',br.get('minEntryPrice')==0.55 and br.get('regimeBar',{}).get('metalMinGap')==0.12)

# --- unknown / with-trend: normal bars ---
GL.clear_prices()
lv=FakeLV(); g=GL.GapLive(lv,opener,log=lambda *a:None)
sig=P(close=1); warm(sig,p=0.65,now=DAY)
chk('unknown regime: crypto allowed at Moogle 0.65',g.on_paper(sig,now=DAY) is not None and lv.calls)
n=len(lv.calls)
sig=P(close=2); warm(sig,p=0.64,now=DAY)
chk('unknown regime: crypto still skipped at Moogle 0.64',g.on_paper(sig,now=DAY) is None and len(lv.calls)==n and 'moogle-below-bar' in g.stat['lastReason'])
chk('unknown regime: metals allowed at |gap| 0.06',g.on_paper(Mp(close=3,gap=0.06),now=DAY) is not None and len(lv.calls)==n+1)
n=len(lv.calls)
chk('unknown regime: metals still skipped at |gap| 0.059',g.on_paper(Mp(close=4,gap=0.059),now=DAY) is None and len(lv.calls)==n)

# with the trend: YES while bullish, NO while bearish — normal bar
set_reg('BTC','bullish'); set_reg('GOLD','bearish')
sig=P(close=5,side='YES'); warm(sig,p=0.65,now=DAY)
chk('with-trend YES/bullish: crypto allowed at 0.65',g.on_paper(sig,now=DAY) is not None)
chk('with-trend NO/bearish: metals allowed at |gap| 0.06',g.on_paper(Mp(asset='GOLD',close=6,side='NO',gap=0.06),now=DAY) is not None)

# --- crypto against-trend: 0.75 bar ---
lv=FakeLV(); g=GL.GapLive(lv,opener,log=lambda *a:None)
set_reg('BTC','bearish')
sig=P(close=10,side='YES'); warm(sig,p=0.74,now=DAY); n=len(lv.calls)
chk('against-trend bearish YES: crypto 0.74 skipped against-trend-bar',
    g.on_paper(sig,now=DAY) is None and len(lv.calls)==n and g.stat['lastReason']=='against-trend-bar')
# recorded on the attempt
chk('against-trend skip records regime=bearish on the attempt',
    g.recent and g.recent[-1]['reason']=='against-trend-bar' and g.recent[-1]['regime']=='bearish')
sig=P(close=11,side='YES'); warm(sig,p=0.75,now=DAY)
t=g.on_paper(sig,now=DAY)
chk('against-trend bearish YES: crypto 0.75 allowed',t is not None and lv.calls)
chk('opened crypto trade records regime=bearish',t.get('regime')=='bearish')

set_reg('ETH','bullish')
sig=P(pair='BTC-ETH',asset='ETH',close=12,side='NO'); warm(sig,p=0.74,now=DAY); n=len(lv.calls)
chk('against-trend bullish NO: crypto 0.74 skipped against-trend-bar',
    g.on_paper(sig,now=DAY) is None and len(lv.calls)==n and g.stat['lastReason']=='against-trend-bar')
sig=P(pair='BTC-ETH',asset='ETH',close=13,side='NO'); warm(sig,p=0.75,now=DAY)
t=g.on_paper(sig,now=DAY)
chk('against-trend bullish NO: crypto 0.75 allowed and regime=bullish on trade',t is not None and t.get('regime')=='bullish')

# --- metals against-trend: |gap| 0.12 ---
lv=FakeLV(); g=GL.GapLive(lv,opener,log=lambda *a:None)
set_reg('GOLD','bearish')
n=len(lv.calls)
chk('against-trend bearish YES: metals |gap| 0.119 skipped against-trend-bar',
    g.on_paper(Mp(asset='GOLD',close=20,side='YES',gap=0.119),now=DAY) is None and len(lv.calls)==n and g.stat['lastReason']=='against-trend-bar')
t=g.on_paper(Mp(asset='GOLD',close=21,side='YES',gap=0.12),now=DAY)
chk('against-trend bearish YES: metals |gap| 0.12 allowed, regime recorded',t is not None and t.get('regime')=='bearish' and len(lv.calls)==n+1)

set_reg('SILVER','bullish')
n=len(lv.calls)
chk('against-trend bullish NO: metals |gap| 0.119 skipped against-trend-bar',
    g.on_paper(Mp(asset='SILVER',close=22,side='NO',gap=0.119),now=DAY) is None and len(lv.calls)==n and g.stat['lastReason']=='against-trend-bar')
t=g.on_paper(Mp(asset='SILVER',close=23,side='NO',gap=0.12),now=DAY)
chk('against-trend bullish NO: metals |gap| 0.12 allowed, regime=bullish',t is not None and t.get('regime')=='bullish')

# negative gapDown still uses absolute value against the 0.12 bar
set_reg('GOLD','bearish'); n=len(lv.calls)
chk('against-trend metals |gapDown| 0.12 (negative) allowed',
    g.on_paper(Mp(asset='GOLD',close=24,side='YES',gap=-0.12),now=DAY) is not None and len(lv.calls)==n+1)

# --- under-55c: live ask, not just signal price ---
GL.clear_prices()
lv=FakeLV(ask=0.54); g=GL.GapLive(lv,opener,log=lambda *a:None)
sig=P(close=30,price=0.59); warm(sig,p=0.80,now=DAY)
chk('signal 0.59 but live ask 0.54 skipped under-55c, no buy()',
    g.on_paper(sig,now=DAY) is None and not lv.calls and g.stat['lastReason']=='under-55c')
chk('under-55c attempt records the live ask and a regime',
    g.recent and g.recent[-1]['reason']=='under-55c' and g.recent[-1].get('ask')==0.54 and g.recent[-1].get('regime') in ('bullish','bearish','unknown'))

lv=FakeLV(ask=0.55); g=GL.GapLive(lv,opener,log=lambda *a:None)
sig=P(close=31,price=0.55); warm(sig,p=0.80,now=DAY)
chk('live ask == 0.55 is allowed',g.on_paper(sig,now=DAY) is not None and lv.calls)

# NO-side ask = 1 - yesBid. yesAsk=0.40, yesBid=0.38 => NO ask = 0.62, allowed
lv=FakeLV(ask=0.40); g=GL.GapLive(lv,opener,log=lambda *a:None)  # wsq yesAsk=0.40, yesBid=0.38
# wait: for NO, ask = 1-yesBid = 1-0.38 = 0.62 >= 0.55, would allow. Need NO ask < 0.55, so yesBid > 0.45
# Custom quote: yesBid=0.47 => NO ask=0.53
class FakeLVNO(FakeLV):
    def wsq(s,ticker):
        return dict(yesAsk=0.50,yesBid=0.47)  # NO ask = 0.53
lv=FakeLVNO(); g=GL.GapLive(lv,opener,log=lambda *a:None)
sig=P(close=32,side='NO',price=0.59); warm(sig,p=0.80,now=DAY)
chk('NO-side live ask 0.53 skipped under-55c, no buy()',
    g.on_paper(sig,now=DAY) is None and not lv.calls and g.stat['lastReason']=='under-55c')

# metals also respect the ask floor
lv=FakeLV(ask=0.50); g=GL.GapLive(lv,opener,log=lambda *a:None)
chk('metals live ask 0.50 skipped under-55c',
    g.on_paper(Mp(close=33,gap=0.20,price=0.62),now=DAY) is None and not lv.calls and g.stat['lastReason']=='under-55c')

print('ALL PASS' if ok else 'SOME FAILED')
raise SystemExit(0 if ok else 1)
