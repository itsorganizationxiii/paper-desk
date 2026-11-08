"""Offline lock configuration and actual _order stop-path regressions."""
import ast
import math
import os
import types
from pathlib import Path
os.environ['LIVE_TRADING']='0'
import live

src=Path('server.py').read_text()
tree=ast.parse(src)
names={'WKND_UP_ARM','WKND_UP_DROP','WKND_LOCK_ARM_C','WKND_DROP'}
g={'WKND_MODE':'volatile','wknd_night':lambda:True}
for n in tree.body:
    if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets):
        exec(compile(ast.Module(body=[n],type_ignores=[]),'<constants>','exec'),g)
    if isinstance(n,ast.FunctionDef) and n.name in {'wknd_up','lock_arm','lock_drop'}:
        exec(ast.get_source_segment(src,n),g)
assert g['lock_arm']({'side':'YES'})==.18
assert g['lock_drop']({'side':'YES'})==.08
assert g['lock_arm']({'side':'NO'})==.20
assert g['lock_drop']({'side':'NO'})==.07
print('PASS Up +18c/8c, Down +20c/7c code constants and effective helpers')
assert live.Live.order_fields(dict(fill_count=2,average_fill_price='.20'),'NO',.01)[1]==.80
assert live.Live.order_fields(dict(fill_count_fp=2,taker_fill_cost_dollars='.60',side='no'),'NO',.01)[1]==.30
print('PASS extreme IOC limit is not mistaken for actual NO fill price')

class Clock:
    now=1000.0
    def time(self): return self.now
    def sleep(self,seconds): raise AssertionError('stop path must never sleep')

original_api,original_time=live.api,live.time
try:
    for bid,partial,expected_calls in ((.35,False,2),(.36,False,4),(.36,True,4)):
        clock=Clock(); calls=[]
        L=live.Live(); L.owner=lambda:True
        L.position=lambda *a:(_ for _ in ()).throw(AssertionError('position preflight'))
        L._quote=lambda *a:(_ for _ in ()).throw(AssertionError('quote preflight'))
        L.log=lambda **kw:calls.append(('log',kw))
        orders={}; counter=[0]
        def api(method,path,body=None):
            clock.now+=.3
            calls.append((method,path,body))
            if method=='POST':
                assert body['reduce_only'] is True
                assert body['time_in_force']=='immediate_or_cancel'
                counter[0]+=1; oid=str(counter[0])
                aggressive=float(body['price'])==.99
                fc=float(body['count']) if aggressive else (.5 if partial else 0)
                orders[oid]=dict(order_id=oid,fill_count_fp=fc,status='canceled',average_fill_price_dollars=.70,fees_dollars=.01 if fc else 0)
                return 201,dict(order_id=oid)
            assert '/portfolio/orders/' in path and '/fills' not in path
            oid=path.split('/orders/')[1].split('?')[0]
            return 200,{'order':orders[oid]}
        live.api=api; live.time=clock
        result=L.stop_sell('KXBTC15M-T','NO',2,.36,bid,clock.time())
        assert result[0]==2,result
        network=[c for c in calls if c[0]!='log']
        assert len(network)==expected_calls,network
        assert clock.now-1000<2
        posts=[c for c in network if c[0]=='POST']
        if partial: assert posts[-1][2]['count']=='1.50'
        assert posts[-1][2]['price']=='0.9900'
        log=[c[1] for c in calls if c[0]=='log' and c[1]['action']=='stop-sell'][-1]
        assert log['fallback'] and log['elapsedMs'] in (600,1200)
        assert {'slipC','fallbackSubmitAt','fillAt'}<=log.keys()
        print(f'PASS bid={bid}, partial={partial}: {len(network)} mocked 300ms HTTP calls, {log["elapsedMs"]}ms, no sleeps/preflights, reduce-only remaining size')
finally:
    live.api=original_api; live.time=original_time
