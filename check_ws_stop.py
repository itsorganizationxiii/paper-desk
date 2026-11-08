import os,ast,threading,time,types
os.environ.pop('KALSHI_API_KEY_ID',None)
import live
live.STOP_RETRY_SEC=0.01
sent=[]
def fake_http(*a,**k): raise AssertionError('network call attempted')
live._http=fake_http
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
def mkLive(fill_limit, bids):
    L=live.Live(); L.owner=lambda:True; L.position=lambda tk:-2.0
    it=iter(bids); L._quote=lambda tk,sd,k:next(it,bids[-1])
    def order(tk,side,action,count,px,confirm=None):
        sent.append((tk,side,action,count,px))
        if fill_limit and px==fill_limit: return 201,count,px,0.01,'o1',None
        if not fill_limit and px!=0.34: return 201,count,px,0.01,"o2",None
        return 201,0,None,0.0,'o0',None
    L._order=order; return L
sent.clear(); L=mkLive(0.34,[0.35])
r=L.stop_sell('KXBTC15M-T','NO',2.0,0.359,0.35,time.time())
chk('limit fills at stop-2c on first try',r[0]==2.0 and r[1]==0.34 and len(sent)==1 and sent[0][4]==0.34)
lg=L.orders[-1]; chk('slippage fields logged',all(k in lg for k in ('stopLevel','bidAtTrigger','triggerAt','fillPrice','fillAt','slipC')) and lg['fallback'] is False)
sent.clear(); L=mkLive(None,[0.35,0.35,0.35,0.30,0.30])
r=L.stop_sell('KXBTC15M-T','NO',2.0,0.359,0.35,time.time())
lims=[s for s in sent if s[4]==0.34]
chk('1 limit try then aggressive fallback',len(lims)==1 and len(sent)>=2 and r[0]==2.0 and L.orders[-1]['fallback'] is True)
sent.clear(); L=mkLive(None,[0.10])
r=L.stop_sell('KXBTC15M-T','NO',2.0,0.359,0.10,time.time())
chk('blown-through: no limit sent, falls back so never left unsold',not [s for s in sent if s[4]==0.34] and r[0]==2.0)
chk('XRP stop_sell refused',L.stop_sell('KXXRP15M-T','YES',1,0.3,0.2)[0]==0)
# ---- server ws path: single sell under concurrent ws + tick ----
src=open('server.py').read(); tree=ast.parse(src)
want={'PaperTrader','_ws_quote'}
code=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if getattr(n,'name',None) in want)
calls=[]
class FakeLV:
    def owner(s): return True
    def enabled(s): return True
    def allowed(s): return True
    def stop_sell(s,*a):
        calls.append(a); time.sleep(0.2); return a[2],0.34,0.01,'o',None
    def sell(s,*a,**k): calls.append(a); return a[2],0.5,0.01,'o',None
    def book(s,x): pass
g=dict(threading=threading,time=time,lock=threading.Lock(),LV=FakeLV(),stop_pct=lambda:0.6,lock_active=lambda:False,LOCK_PEAK_BY_ASSET={},LOCK_PEAK=0.8,
       lock_arm=lambda t=None:0.12,lock_drop=lambda t:0.05,LOCK_HOLD_P=0.9,kalshi_fee=lambda c,p:0.01,MCOL=None,TF='/tmp/x',json=__import__('json'),os=os)
exec('import math\n'+code,g)
pt=g['PaperTrader'].__new__(g['PaperTrader']); pt.trades=[dict(id=1,mode='live',asset='BTC',side='NO',price=0.5977,contracts=1.66,stake=1.02,ticker='KXBTC15M-T',closesAt=time.time()+300,status='open')]
pt.save=lambda:None; g['pt']=pt
ths=[threading.Thread(target=g['_ws_quote'],args=('KXBTC15M-T',dict(yesBid=0.67,yesAsk=0.68))) for _ in range(5)]
for th in ths: th.start()
with g['lock']: pt.exit(pt.trades[0],0.32,'stop: tick mark 0.320')
for th in ths: th.join()
time.sleep(0.5)
t=pt.trades[0]
chk('ws quote crossing stop triggers exactly one sell',len(calls)==1 and t['status']=='exited')
chk('used limit-protected stop_sell with stop level 0.3586',abs(calls[0][3]-0.3586)<1e-3)
chk('trade logs stop slippage fields',all(k in t for k in ('stopLevel','stopBidAtTrigger','stopTriggerAt','stopFillPrice','stopFillAt','stopSlipC')))
calls.clear(); g['_ws_quote']('KXBTC15M-T',dict(yesBid=0.67,yesAsk=0.68)); chk('closed position not re-sold',not calls)

# ---- tick recording failure must not break the stop path ----
want2={'PaperTrader','_ws_quote','tick_rec'}
code2=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if getattr(n,'name',None) in want2)
class Boom:
    def append(s,*a): raise RuntimeError('mongo down')
    def __len__(s): raise RuntimeError('mongo down')
g2=dict(g); g2.update(_tq=Boom(),_tlast={},TICKS=dict(paused=False,deduped=0,dropped=0),TCOL=object(),TICK_MAXQ=10,lock=threading.Lock())
exec('import math\n'+code2,g2)
calls.clear()
pt2=g2['PaperTrader'].__new__(g2['PaperTrader']); pt2.trades=[dict(id=2,mode='live',asset='BTC',side='YES',price=0.5,contracts=2,stake=1.0,ticker='KXBTC15M-U',closesAt=time.time()+300,status='open')]
pt2.save=lambda:None; g2['pt']=pt2
g2['_ws_quote']('KXBTC15M-U',dict(yesBid=0.25,yesAsk=0.27))
time.sleep(0.5)
chk('recorder exception -> stop still fires once',len(calls)==1 and pt2.trades[0]['status']=='exited')
sent.clear(); L=mkLive(None,[0.35]); t0=time.time()
live_sleep=[]; _ts=time.sleep
import live as _lv
_lv.time.sleep=lambda x:live_sleep.append(x)
r=L.stop_sell('KXBTC15M-T','NO',2.0,0.359,0.35,time.time())
_lv.time.sleep=_ts
chk('worst-case built-in waits under 2s (limit miss + fallback)',sum(live_sleep)<2.0 and r[0]==2.0)
raise SystemExit(0 if ok else 1)
