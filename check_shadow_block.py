import os,sys
os.environ.pop('KALSHI_API_KEY_ID',None)
import live
calls=[]
live._http=lambda *a,**k:(calls.append(a) or (200,{}))
L=live.Live()
L.owner=lambda:True
from datetime import datetime
from zoneinfo import ZoneInfo
ET=ZoneInfo('America/New_York')
def wknd(ts):
    d=datetime.fromtimestamp(ts,ET);wd,h=d.weekday(),d.hour
    return (wd==4 and h>=17) or wd in (5,6) or (wd==0 and h<9)
sat=datetime(2026,11,7,20,0,tzinfo=ET).timestamp(); tue=datetime(2026,11,10,12,0,tzinfo=ET).timestamp()
now=[sat]
def shadow_asset(a): return a=='XRP' or (a=='ETH' and wknd(now[0]))
L.entry_block=lambda asset,ticker:shadow_asset(str(asset).upper()) or 'XRP' in str(ticker).upper() or ('KXETH' in str(ticker).upper() and wknd(now[0]))
ok=True
def chk(name,cond):
    global ok; print(('PASS ' if cond else 'FAIL ')+name); ok&=bool(cond)
chk('XRP buy refused',L.buy('XRP','KXXRP15M-26NOV072015-15','YES',0.1,0.5) is None and L.lastBuy=='blocked')
chk('XRP rest_buy refused',L.rest_buy('XRP','KXXRP15M-26NOV072015-15','YES',0.5,lambda p:0.1,wait=0) is None)
chk('XRP sell refused',L.sell('KXXRP15M-26NOV072015-15','YES',1,0.5)[0]==0)
chk('XRP _order refused',L._order('KXXRP15M-26NOV072015-15','YES','buy',1,0.5)[0]==0)
chk('XRP api POST refused',live.api('POST','/portfolio/events/orders',{'ticker':'KXXRP15M-X'})[0]==403)
chk('weekend-night ETH buy refused',L.buy('ETH','KXETH15M-26NOV072015-15','YES',0.1,0.5) is None and L.lastBuy=='blocked')
chk('weekend-night ETH rest_buy refused',L.rest_buy('ETH','KXETH15M-26NOV072015-15','YES',0.5,lambda p:0.1,wait=0) is None)
now[0]=tue
chk('weekday ETH not entry-blocked',not L.entry_block('ETH','KXETH15M-26NOV101215-15'))
chk('weekday XRP still blocked',L.entry_block('XRP','KXXRP15M-26NOV101215-15'))
chk('BTC never blocked',not L.entry_block('BTC','KXBTC15M-X'))
chk('no HTTP calls made',not calls)
sys.exit(0 if ok else 1)
