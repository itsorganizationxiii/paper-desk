import os,sys,ast,time
os.environ.pop('KALSHI_API_KEY_ID',None)
import live
from datetime import datetime
from paper_tz import ET
calls=[]
live._http=lambda *a,**k:(calls.append(a) or (200,{}))
src=open('server.py').read(); tree=ast.parse(src)
code=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if getattr(n,'name',None) in ('cooldowns','shadow_asset'))
code+=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if isinstance(n,ast.Assign) and any(getattr(t,'id',None) in ('COOL_LOSSES','COOL_SEC','_cool','_cool_logged') for t in n.targets))
class PT: trades=[]
g=dict(time=time,datetime=datetime,ET=ET,pt=PT); exec(code,g)
L=live.Live(); L.owner=lambda:True
L.entry_block=lambda asset,ticker:g['shadow_asset'](str(asset).upper()) or 'XRP' in str(ticker).upper()
ok=True
def chk(name,cond):
    global ok; print(('PASS ' if cond else 'FAIL ')+name); ok&=bool(cond)
def reset(): g['_cool'].update(at=0,map={})
chk('XRP buy refused',L.buy('XRP','KXXRP15M-26NOV072015-15','YES',0.1,0.5) is None and L.lastBuy=='blocked')
chk('XRP rest_buy refused',L.rest_buy('XRP','KXXRP15M-26NOV072015-15','YES',0.5,lambda p:0.1,wait=0) is None)
chk('XRP sell refused',L.sell('KXXRP15M-26NOV072015-15','YES',1,0.5)[0]==0)
chk('XRP _order refused',L._order('KXXRP15M-26NOV072015-15','YES','buy',1,0.5)[0]==0)
chk('XRP api POST refused',live.api('POST','/portfolio/events/orders',{'ticker':'KXXRP15M-X'})[0]==403)
chk('XRP stop_sell refused',L.stop_sell('KXXRP15M-X','YES',1,0.3,0.2)[0]==0)
reset(); chk('weekend-night ETH allowed (no cooldown)',not L.entry_block('ETH','KXETH15M-26NOV072015-15'))
now=time.time()
mk=lambda a,p,ago:dict(mode='live',asset=a,status='lost' if p<0 else 'won',pnl=p,closedAt=now-ago)
PT.trades=[mk('ETH',-0.5,900),mk('ETH',-0.4,600),mk('ETH',-0.3,300)]; reset()
chk('ETH after 3 straight live losses refused',L.entry_block('ETH','KXETH15M-X') and L.buy('ETH','KXETH15M-X','YES',0.1,0.5) is None and L.lastBuy=='blocked')
chk('cooldown ~55 min left',50<g['cooldowns']()['ETH']['minsLeft']<=60)
chk('other coins unaffected',not L.entry_block('BTC','KXBTC15M-X') and not L.entry_block('SOL','KXSOL15M-X'))
PT.trades=[mk('ETH',-0.5,900),mk('ETH',0.4,600),mk('ETH',-0.3,300),mk('ETH',-0.3,200)]; reset()
chk('win resets streak (2 losses after win -> live)',not L.entry_block('ETH','KXETH15M-X'))
PT.trades=[mk('SOL',-0.5,4800),mk('SOL',-0.4,4500),mk('SOL',-0.3,4000)]; reset()
chk('cooldown expires after 60 min',not L.entry_block('SOL','KXSOL15M-X'))
PT.trades=[mk('SOL',-0.5,4800),mk('SOL',-0.4,4500),mk('SOL',-0.3,4000),mk('SOL',-0.2,100)]; reset()
chk('one loss after expired cooldown does not re-trigger',not L.entry_block('SOL','KXSOL15M-X'))
chk('no HTTP calls made',not calls)
sys.exit(0 if ok else 1)
