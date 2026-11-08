"""Offline history-derived entry-cap and actual server wiring checks."""
import ast
import json
from types import SimpleNamespace
from pathlib import Path
from altcap import state

NOW=10000

def trade(**kw):
    return dict(dict(id=1,mode='live',asset='BTC',signalSource='alt-consensus',openedAt=NOW-100,
                     closedAt=NOW-50,status='exited',pnl=.2),**kw)

assert not state([],NOW)['blocked']
assert state([trade(status='open',closedAt=None)],NOW)['blocked']
assert not state([trade(openedAt=NOW-3600)],NOW)['blocked']
assert state([trade(openedAt=NOW-3599.99)],NOW)['blocked']
print('PASS rolling 60-minute cap includes open entries; exact one-hour boundary lifts')

loss=trade(openedAt=NOW-5000,closedAt=NOW-100,pnl=-.01)
assert state([loss],NOW)['blocked']
assert 'lost' in state([loss],NOW)['reason']
assert state([loss],NOW)['blockedUntil']==NOW+3500
assert not state([loss],NOW+3500)['blocked']
assert state([loss],NOW+3499.99)['blocked']
print('PASS loss pauses 60 minutes from close independently of entry time')

for asset in ('BTC','ETH','SOL','XRP'):
    assert state([trade(asset=asset)],NOW)['blocked']
for t in (trade(mode='paper',pnl=-1),trade(shadow=True,pnl=-1),trade(signalSource='leadlag'),trade(signalSource='ordinary')):
    assert not state([t],NOW)['blocked']
assert state([trade(signalSource='ordinary',altConsensus=True)],NOW)['blocked']
assert not state([trade(pnl=0,openedAt=NOW-5000)],NOW)['blocked']
print('PASS across coins, metadata aliases, and only live alt-consensus history; breakeven is not a loss')

newer_win=trade(id=2,openedAt=NOW-5000,closedAt=NOW-10,pnl=.1)
assert not state([newer_win,loss],NOW)['blocked']
assert not state([loss,newer_win],NOW)['blocked']
assert state(json.loads(json.dumps([loss])),NOW)==state([loss],NOW)
print('PASS most recent closed trade by timestamp, restart reconstruction, and out-of-order history')

src=Path('server.py').read_text();tree=ast.parse(src)
pt=SimpleNamespace(trades=[loss]);logs=[]
g=dict(alt_cap_state=state,pt=pt,time=SimpleNamespace(time=lambda:NOW),_alt_cap_seen=set(),_alt_cap_skips=[],LV=SimpleNamespace(log=lambda **kw:logs.append(kw)))
exec(next(ast.get_source_segment(src,n) for n in tree.body if getattr(n,'name',None)=='alt_cap_allow'),g)
assert not g['alt_cap_allow']('KXBTC15M-TEST',NOW)
assert not g['alt_cap_allow']('KXBTC15M-TEST',NOW)
assert len(logs)==1 and logs[0]['action']=='alt-cap-skip' and logs[0]['reason']
pt.trades=[]
assert g['alt_cap_allow']('KXBTC15M-TEST',NOW)
part=src[src.index("        if ALT and a=='BTC'"):src.index('        reok=False')]
assert "if alt_cap_allow(k['ticker']) and" in part
assert part.index('alt_cap_allow(')<part.index('fill=LV.buy(')
assert "d['altCap']=dict(alt_cap_state(pt.trades)" in src
print('PASS live alt branch guard, deduplicated reason logs, and status wiring; no HTTP/orders')

# Execute the actual ALT branch with mocked execution, not merely its helper.
import threading
branch=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.get_source_segment(src,n.test).startswith("ALT and a=='BTC'"))
branch_src=ast.get_source_segment(src,branch)
lines=branch_src.splitlines()
normalized=lines[0]+'\n'+ '\n'.join(l[8:] for l in lines[1:])
wrapper='def run_branch():\n    for a in ["BTC"]:\n'+ '\n'.join('        '+l for l in normalized.splitlines())
buys=[]
L=SimpleNamespace(enabled=lambda:True,tried=set(),resting=set(),buy=lambda *a,**kw:buys.append(a),log=lambda **kw:None)
pt.size=lambda *a:(.1,.08,.58)
g.update(ALT=True,ALT_MAX=.60,LV=L,left=600,k=dict(ticker='KXBTC15M-NEXT',yesAsk=.5,noAsk=.5,close=NOW+600,strike=1),
         alt_votes=lambda tk:dict(SOL='YES',DOGE='YES',XRP='YES'),need_edge=lambda:.07,
         is_shadow=lambda t:False,lock=threading.RLock(),sigs={},sg={},up_tilt_skip=lambda *a:False)
original_allow=g['alt_cap_allow']
g['alt_cap_allow']=lambda ticker:original_allow(ticker,NOW)
exec(wrapper,g)
pt.trades=[loss];g['run_branch']();assert not buys
pt.trades=[];g['run_branch']();assert len(buys)==1
print('PASS actual server ALT branch refuses capped live buy and leaves uncapped live execution enabled')
