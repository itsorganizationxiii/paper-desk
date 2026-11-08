import os,sys,ast,time
os.environ.pop('KALSHI_API_KEY_ID',None)
import live
from datetime import datetime
from paper_tz import ET
calls=[]
live._http=lambda *a,**k:(calls.append(a) or (200,{}))
src=open('server.py').read(); tree=ast.parse(src)
code=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if getattr(n,'name',None) in ('cooldowns','shadow_asset'))
code+=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if isinstance(n,ast.Assign) and any(getattr(t,'id',None) in ('COOL_LOSSES','COOL_SEC','SHARED_COOL_LOSSES','SHARED_COOL_SEC','_cool','_cool_logged') for t in n.targets))
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
chk('other coins have no per-coin cooldown but obey shared pause','BTC' not in g['cooldowns']() and 'SOL' not in g['cooldowns']() and 'ALL' in g['cooldowns']() and L.entry_block('BTC','KXBTC15M-X') and L.entry_block('SOL','KXSOL15M-X'))
PT.trades=[mk('ETH',-0.5,900),mk('ETH',0.4,600),mk('ETH',-0.3,300),mk('ETH',-0.3,200)]; reset()
chk('win resets per-coin streak; 2 later losses trigger only shared cooldown','ETH' not in g['cooldowns']() and 'ALL' in g['cooldowns']() and L.entry_block('ETH','KXETH15M-X'))
PT.trades=[mk('SOL',-0.5,4800),mk('SOL',-0.4,4500),mk('SOL',-0.3,4000)]; reset()
chk('cooldown expires after 60 min',not L.entry_block('SOL','KXSOL15M-X'))
PT.trades=[mk('SOL',-0.5,4800),mk('SOL',-0.4,4500),mk('SOL',-0.3,4000),mk('SOL',-0.2,100)]; reset()
chk('one loss after expired per-coin cooldown does not re-trigger SOL; consecutive losses still trigger shared','SOL' not in g['cooldowns']() and 'ALL' in g['cooldowns']())

# ---- profit-lock re-entry ----
code3=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if getattr(n,'name',None)=='reentry_ok')
code3+=''.join(ast.get_source_segment(src,n)+'\n' for n in tree.body if isinstance(n,ast.Assign) and any(getattr(t,'id',None) in ('REENTRY_WAIT','REENTRY_MIN_LEFT') for t in n.targets))
exec(code3,g); R=g['reentry_ok']
k=dict(ticker='KXBTC15M-W',strike=100.0); cb=dict(spot=101.0,momentum=0.001); now=time.time()
ex=lambda **kw:dict(dict(mode='live',asset='BTC',ticker='KXBTC15M-W',side='YES',status='exited',exitReason='profitlock: peak',closedAt=now-90,pnl=0.2),**kw)
PT.trades=[ex()]; reset()
chk('re-entry allowed after profit lock',R('BTC',k,'YES',cb,400,now))
chk('re-entry refused within 60s of exit',not R('BTC',k,'YES',cb,400,now-60))
chk('re-entry refused with <2 min left',not R('BTC',k,'YES',cb,100,now))
chk('re-entry refused on opposite side',not R('BTC',k,'NO',dict(spot=99,momentum=-1),400,now))
chk('re-entry refused when spot and momentum against',not R('BTC',k,'YES',dict(spot=99,momentum=-0.001),400,now))
PT.trades=[ex(exitReason='stop: mark 0.2',pnl=-0.4)]; reset()
chk('re-entry refused after stop',not R('BTC',k,'YES',cb,400,now))
PT.trades=[ex(exitReason='takeprofit: bid')]; reset()
chk('re-entry refused after take-profit',not R('BTC',k,'YES',cb,400,now))
PT.trades=[ex(exitReason='paircash: up')]; reset()
chk('re-entry refused after paired cash-out',not R('BTC',k,'YES',cb,400,now))
PT.trades=[ex(),dict(ex(),reentry=True,status='exited',closedAt=now-70)]; reset()
chk('only one re-entry per window',not R('BTC',k,'YES',cb,400,now))
PT.trades=[mk('BTC',-0.5,900),mk('BTC',-0.4,600),mk('BTC',-0.3,300),ex()]; reset()
chk('re-entry refused during cooldown',not R('BTC',k,'YES',cb,400,now))
PT.trades=[dict(ex(),asset='XRP',ticker='KXXRP15M-W')]; reset()
chk('XRP never re-enters',not R('XRP',dict(ticker='KXXRP15M-W',strike=1),'YES',dict(spot=2,momentum=1),400,now))
chk('no HTTP calls made',not calls)
sys.exit(0 if ok else 1)
