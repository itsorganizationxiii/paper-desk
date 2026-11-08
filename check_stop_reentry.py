"""Offline checks for post-stop re-entry gate."""
import ast, time
from pathlib import Path
from types import SimpleNamespace
src=Path('server.py').read_text(); tree=ast.parse(src)
fns={'reentry_ok','stop_reentry_confirm','_side_trade_prices'}
code='\n'.join(ast.get_source_segment(src,n) for n in tree.body if getattr(n,'name',None) in fns or isinstance(n,ast.Assign) and any(str(getattr(t,'id','')).startswith(('REENTRY_','STOP_REENTRY_')) for t in n.targets))
pt=SimpleNamespace(trades=[]); g=dict(time=time,pt=pt,shadow_asset=lambda a:a=='XRP'); exec(code,g)
R=g['reentry_ok']; now=time.time()
assert g['STOP_REENTRY_ENABLED'] is True
def feed(trs): return SimpleNamespace(trades=trs)
def tr(no,taker,ago,cnt=10): return dict(market_ticker='KXBTC15M-W',yes_price_dollars=round(1-no,2),no_price_dollars=no,taker_side=taker,count_fp=cnt,ts_ms=int((now-ago)*1000))
def stopped(**kw): return dict(dict(mode='live',asset='BTC',ticker='KXBTC15M-W',side='NO',status='exited',exitReason='stop: ws mark 0.30',exitPrice=0.30,closedAt=now-90,pnl=-0.4),**kw)
k=dict(ticker='KXBTC15M-W',strike=100,noAsk=0.40,noBid=0.38,yesAsk=0.62,yesBid=0.60)
down=dict(spot=99,momentum=-0.001)
F=feed([tr(0.27,'yes',80),tr(0.36,'no',20),tr(0.37,'no',10)])
pt.trades=[stopped()]
assert R('BTC',k,'NO',down,400,now,F)=='stop'; print('PASS confirmed flip after stop -> stop re-entry')
assert R('BTC',k,'NO',down,400,now-40,F) is False; print('PASS waits 60s after stop')
assert R('BTC',k,'NO',down,100,now,F) is False; print('PASS needs 2 min left')
assert R('BTC',k,'NO',dict(spot=101,momentum=0.001),400,now,F) is False; print('PASS momentum must be on our side')
assert R('BTC',dict(k,noBid=0.31),'NO',down,400,now,F) is False; print('PASS needs 5c recovery off post-stop low')
assert R('BTC',dict(k,noAsk=0.72,noBid=0.70),'NO',down,400,now,F) is False; print('PASS ask must be in 40-69c band')
assert R('BTC',k,'NO',down,400,now,feed([tr(0.27,'yes',80),tr(0.36,'yes',20,50),tr(0.37,'no',10,5)])) is False; print('PASS taker flow against blocks')
assert R('BTC',k,'YES',dict(spot=101,momentum=0.001),400,now,F) is False; print('PASS same side only')
pt.trades=[stopped(),dict(stopped(),reentry=True,reentryAfter='stop',closedAt=now-70)]
assert R('BTC',k,'NO',down,400,now,F) is False; print('PASS no re-entry after a re-entry stop / max one per window')
pt.trades=[stopped(asset='XRP',ticker='KXXRP15M-W')]
assert R('XRP',dict(k,ticker='KXXRP15M-W'),'NO',down,400,now,F) is False; print('PASS XRP hard block')
pt.trades=[stopped(shadow=True)]
assert R('BTC',k,'NO',down,400,now,F) is False; print('PASS shadow trades never trigger')
g['STOP_REENTRY_ENABLED']=False; pt.trades=[stopped()]
assert R('BTC',k,'NO',down,400,now,F) is False; print('PASS flag disables'); g['STOP_REENTRY_ENABLED']=True
pt.trades=[stopped(exitReason='profitlock: x',exitPrice=0.7)]
assert R('BTC',k,'NO',down,400,now,F)=='lock'; print('PASS profit-lock path still returns lock')
assert "reentryAfter=" in src and "d['reentries'][_ty]" in src; print('PASS tagging and status split present')
print('ALL PASS')
