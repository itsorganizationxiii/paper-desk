"""Offline checks for shared live entry cooldown. Exits must stay enabled."""
import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import time
from paper_tz import ET

src=Path('server.py').read_text(); tree=ast.parse(src)
wanted={'cooldowns','shadow_asset','reentry_ok'}
assignments={'COOL_LOSSES','COOL_SEC','SHARED_COOL_LOSSES','SHARED_COOL_SEC','_cool','_cool_logged','REENTRY_WAIT','REENTRY_MIN_LEFT'}
code='\n'.join(ast.get_source_segment(src,n) for n in tree.body if getattr(n,'name',None) in wanted or isinstance(n,ast.Assign) and any(getattr(t,'id',None) in assignments for t in n.targets))
pt=SimpleNamespace(trades=[])
g=dict(time=time,pt=pt,datetime=datetime,ET=ET)
exec(code,g)
now=time.time()
def trade(asset,pnl,ago,**kw):
    return dict(mode='live',asset=asset,pnl=pnl,status='exited',closedAt=now-ago,**kw)
def check(trades):
    pt.trades=trades; g['_cool'].update(at=0,map={})
    return g['cooldowns'](now)

cd=check([trade('SOL',-0.4,100),trade('BTC',-0.5,50)])
assert 'ALL' in cd and not any(a in cd for a in ('BTC','ETH','SOL'))
assert 29<cd['ALL']['minsLeft']<30 and 'reason' in cd['ALL']
assert all(g['shadow_asset'](a) for a in ('BTC','ETH','SOL','XRP'))
print('PASS two cross-coin losses block ALL new live entries for 30 minutes')
assert 'ALL' not in check([trade('SOL',-0.4,200),trade('ETH',0.2,150),trade('BTC',-0.5,50)])
print('PASS win resets shared losing streak')
assert 'ALL' not in check([trade('SOL',-0.4,2000),trade('BTC',-0.5,1900)])
print('PASS shared cooldown expires after 30 minutes')
assert 'ALL' not in check([trade('SOL',-0.4,100),trade('XRP',-0.5,50,shadow=True)])
assert 'ALL' not in check([trade('SOL',-0.4,100),dict(trade('ETH',-0.5,50),mode='paper')])
print('PASS shadow and paper losses do not count')
cd=check([trade('BTC',-0.3,300),trade('BTC',-0.4,200),trade('BTC',-0.5,100)])
assert 'ALL' in cd and 'BTC' in cd and cd['BTC']['until']==round(now-100+3600)
print('PASS original per-coin 3-loss/60-minute cooldown remains unchanged')
check([trade('SOL',-0.4,100),trade('ETH',-0.5,50)])
k=dict(ticker='KXBTC15M-W',strike=100)
pt.trades.append(dict(mode='live',asset='BTC',ticker=k['ticker'],side='YES',status='exited',exitReason='profitlock: peak',closedAt=now-200,pnl=0.2))
assert not g['reentry_ok']('BTC',k,'YES',dict(spot=101,momentum=0.001),400,now)
print('PASS shared pause also blocks profit-lock re-entry')
# Rebuild just the cooldown functions as a new interpreter would after restart.
g2=dict(time=time,pt=pt,datetime=datetime,ET=ET); exec(code,g2)
assert 'ALL' in g2['cooldowns'](now)
print('PASS cooldown rebuilds from closed live trade history after restart')
# Live.enabled remains independent of entry_block; therefore stop/exit calls stay available.
import live
L=live.Live(); L.allowed=lambda:True; L.owner=lambda:True; L.paused=False
L.entry_block=lambda asset,ticker:g['shadow_asset'](asset)
assert L.enabled() and L.entry_block('BTC','KXBTC15M-W')
assert L.buy('BTC','KXBTC15M-W','YES',0.1,0.5) is None and L.lastBuy=='blocked'
assert L.rest_buy('BTC','KXBTC15M-W','YES',0.5,lambda p:0.1,wait=0) is None
print('PASS regular/leadlag buy and resting entries blocked, while Live.enabled for exits remains true')
print('ALL SHARED COOLDOWN TESTS PASSED (offline; no real orders)')
