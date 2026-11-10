"""Offline checks for manual-flip logging (owner t1141u). No network, no real orders."""
import os, re, time
os.environ.pop('LIVE_STRATEGIES',None); os.environ['LIVE_TRADING']='0'
import manualflip as MF, gaplive as GL, live as L, moogle as M
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
src=open('manualflip.py').read()
banned=("'/portfolio'","'/balance'","create_order","place_order","/api/status","live.status","live.balance",".buy(",".sell(","LIVE_TRADING")
chk('module has no order/portfolio/balance/status path',not re.search(r'/portfolio|/balance|create_order|place_order|/api/status|live\.status\(|live\.balance\(|\.buy\(|\.sell\(',src))
MF.reset()
# --- record shape + first-seen context (caches only) ---
now=time.time()
M.CUR['BTC']=dict(ticker='KXBTC15M-26NOV100315-15',t=now-5,p=0.72)
class FakeQ:
    def wsq(s,tk): return dict(yesAsk=0.54,yesBid=0.51)
t=dict(id=99,mode='live',shadow=False,adopted=True,asset='BTC',side='YES',contracts=0.57,price=0.40,entryFee=0.01,
       ticker='KXBTC15M-26NOV100315-15',openedAt=now-60,closesAt=now+40,status='open')
rec=MF.seen(t,lv=FakeQ(),now=now-60)
chk('seen tags the trade',t.get('tag')=='manual-flip' and t.get('manualFlip') is True)
chk('record fields',rec and rec['ticker']=='KXBTC15M-26NOV100315-15' and rec['coin']=='BTC' and rec['side']=='YES' and rec['contracts']==0.57 and rec['entry']==0.40 and rec['tag']=='manual-flip')
chk('seconds left at first seen',rec['secondsLeft'] is not None and 90<=rec['secondsLeft']<=110)
chk('moogle p for YES side from cache',rec['moogleP']==0.72 and rec['moogleAgeSec'] is not None)
chk('ask/bid from existing WS quote cache',rec['ask']==0.54 and rec['bid']==0.51)
# regime/volume may be None if gapregime cache is empty — that's ok
chk('is_manual true, bot gap-live false',MF.is_manual(t) and not MF.is_manual(dict(gapLive=True,liveOrderId='x',ticker='KXBTC15M-Z',side='YES')))
# --- sells + pnl ---
t.update(status='exited',exitPrice=0.51,exitFee=0.009975,pnl=round(0.57*0.51-0.009975-0.57*0.40-0.01,4),closedAt=now)
MF.on_sell(t,0.57,0.51,fee=0.009975,at=now)
rec=MF.snapshot()[0]
chk('sell logged',len(rec['sells'])==1 and rec['sells'][0]['price']==0.51 and rec['sells'][0]['qty']==0.57)
chk('status exited and pnl from entry+sell',rec['status']=='exited' and rec['pnl'] is not None and rec['pnl']>0 and rec['win'] is True)
# settlement (ended opposite)
t.update(finalResult='no',wouldHaveWon=False,settle=None)
MF.on_settle(t)
rec=MF.snapshot()[0]
chk('settlement recorded (wouldHaveWon false)',rec.get('settle')=='no' and rec.get('wouldHaveWon') is False)
# --- backfill the 3:14 AM sale from recentOrders; skip bot sells that have a matching buy ---
MF.reset()
orders=[
    dict(action='buy',ticker='KXETH15M-26NOV092330-30',side='NO',filled=1.92,fillPrice=0.52,fee=0.0336,at=1794284277.05),
    dict(action='sell',ticker='KXETH15M-26NOV092330-30',side='NO',filled=1.92,fillPrice=0.48,fee=0.03,at=1794284300.0),
    dict(action='order-submitted',ticker='KXBTC15M-26NOV100315-15',side='YES',at=1794298447.619504),
    dict(action='sell',ticker='KXBTC15M-26NOV100315-15',side='YES',want=0.57,filled=0.57,fillPrice=0.51,fee=0.009975,at=1794298452.668627),
]
n=MF.backfill(orders=orders,trades=[])
its=MF.snapshot()
chk('backfill creates the 3:14 record only (bot ETH sell skipped)',n==1 and len(its)==1 and its[0]['ticker']=='KXBTC15M-26NOV100315-15')
chk('backfill first-seen from order-submitted, ~47s left',its[0]['secondsLeft'] is not None and 40<=its[0]['secondsLeft']<=55)
chk('backfill sell + entry null (no fills in the log)',len(its[0]['sells'])==1 and its[0]['sells'][0]['price']==0.51 and its[0].get('entry') is None)
chk('backfill does not invent pnl without entry',its[0].get('pnl') is None)
# --- cap: manual positions never increment test #3 ---
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
    t=dict(id=len(opened)+1,gapLive=True,mode='live',status='open',pnl=None,tag='gap-live',strike=strike,liveOrderId=fill.get('orderId')); opened.append(t); return t
lv=FakeLV(); g=GL.GapLive(lv,opener,log=lambda *a:None)
# adopt / sell a manual flip while the engine is live
man=dict(id=500,mode='live',shadow=False,adopted=True,tag='manual-flip',manualFlip=True,asset='BTC',side='YES',
         contracts=0.57,price=0.40,ticker='KXBTC15M-26NOV100315-15',openedAt=now,closesAt=now+40,status='open')
MF.seen(man); MF.on_sell(dict(man,status='exited'),0.57,0.51,fee=0.01,at=now)
chk('manual seen/sell does not increment cap',g.used()==0 and lv.gapLiveUsed==0 and not lv.calls and not lv.gapLiveDisabled)
sm=g.summary([man, dict(man,status='exited',pnl=0.12)])
chk('manual flip (no gapLive / no liveOrderId) is excluded from test #3 stats',sm['thisTest']['trades']==0 and sm['thisTest']['closed']==0 and sm['used']==0)
# a real gap-live fill still counts
sig=dict(id='GP1',mode='paper',shadow=False,gap=True,rule='A',pair='BTC-ETH',asset='BTC',laggard='BTC',ticker='KXBTC15M-X1',side='YES',price=0.5,entry=0.5,stake=1,conf='normal',closesAt=time.time()+1000)
M.CUR['BTC']=dict(ticker=sig['ticker'],t=time.time(),p=0.70)
g.on_paper(sig)
chk('real gap-live fill still counts 1/5',lv.gapLiveUsed==1 and g.used()==1 and len(lv.calls)==1)
# --- api / brief / dashboard ---
MF.reset()
MF.backfill(orders=orders,trades=[])
# give the backfilled record a known pnl so the dashboard line has a number
with MF._lk:
    MF.ITEMS[0]['entry']=0.40; MF.ITEMS[0]['entryFee']=0.01; MF._touch(MF.ITEMS[0])
api=MF.api()
chk('/api/manualflips shape',set(api)=={'summary','items'} and api['summary']['n']==1 and api['items'][0]['ticker'].startswith('KXBTC15M-26NOV100315-15'))
chk('brief matches dashboard line',MF.brief()=='1, 1 win, '+f"{api['summary']['pnl']:+.2f}")
html=open('index.html').read(); srv=open('server.py').read()
chk('dashboard line 🎯 Manual flips', '🎯 Manual flips:' in html and 'd.manualFlips' in html)
chk('server exposes /api/manualflips and status field',"/api/manualflips" in srv and "d['manualFlips']" in srv)
chk('server tags adopt as manual-flip',"tag='manual-flip'" in srv or 'tag="manual-flip"' in srv or "t['tag']='manual-flip'" in srv)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
