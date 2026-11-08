"""Offline exchange mocks only: never authenticate or submit a real order."""
import ast
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
os.environ.pop('KALSHI_API_KEY_ID',None)
import live
from reconcile import Reconciler
from trader_lease import TraderLease

SRC=Path('server.py').read_text(); TREE=ast.parse(SRC)
CODE='\n'.join(ast.get_source_segment(SRC,n) for n in TREE.body if getattr(n,'name',None)=='PaperTrader')

class Tests(unittest.TestCase):
    def engine(self):
        L=live.Live(); L.allowed=lambda:True; L.lease_ok=lambda:True
        g=dict(time=time,LV=L,perp_premium=lambda a:None,regime=lambda a:None,flow=lambda a:None)
        exec(CODE,g); P=g['PaperTrader'].__new__(g['PaperTrader']); P.trades=[]; P.save=lambda:None
        R=Reconciler(L,P,threading.RLock()); return L,P,R
    def response(self,positions):
        def api(method,path,body=None):
            self.assertEqual(method,'GET')
            if path.startswith('/portfolio/positions'): return 200,dict(market_positions=positions,cursor='')
            if path.startswith('/portfolio/fills'): return 200,dict(fills=[],cursor='')
            if path.startswith('/markets/'):
                return 200,dict(market=dict(close_time=datetime.fromtimestamp(time.time()+300,timezone.utc).isoformat(),floor_strike=75000,status='active'))
            self.fail(path)
        return api
    def test_adopts_unknown_position_no_duplicates_and_ignores_shadow(self):
        L,P,R=self.engine(); tk='KXBTC15M-TEST'; P.trades=[dict(mode='shadow',shadow=True,status='open',ticker=tk,side='NO',contracts=2,id=1)]
        row=dict(ticker=tk,position_fp='-2.04',market_exposure_dollars='0.9996')
        with patch.object(live,'api',side_effect=self.response([row])):
            R.run(); R.run()
        adopted=[t for t in P.trades if t.get('adopted')]
        self.assertEqual(len(adopted),1); t=adopted[0]
        self.assertEqual(t['mode'],'live'); self.assertFalse(t['shadow']); self.assertEqual(t['contracts'],2.04)
        self.assertAlmostEqual(t['price'],0.49); self.assertEqual(len(R.state['adopted']),1); self.assertIsNone(R.state['lastError'])
    def test_zero_settled_and_xrp_are_not_adopted(self):
        L,P,R=self.engine()
        with patch.object(live,'api',side_effect=self.response([dict(ticker='KXBTC15M-T',position_fp='0'),dict(ticker='KXXRP15M-T',position_fp='-2')])):
            R.run()
        self.assertFalse(P.trades); self.assertIn('XRP',R.state['mismatches'][0]['reason'])
    def test_late_fill_after_cancel_is_tracked(self):
        L,P,R=self.engine(); calls=[]; canceled=[False]; tk='KXBTC15M-T'; L._quote=lambda *a:0.49
        L.cash=10; L.cashAt=time.time(); L.balance=lambda:9; L.position=lambda ticker:-2.04
        def api(method,path,body=None):
            calls.append((method,path))
            if method=='POST': return 201,dict(order=dict(order_id='late',fill_count_fp='0',status='resting'))
            if method=='DELETE':
                self.assertIn('/portfolio/events/orders/late?',path)
                self.assertIn('market_ticker=KXBTC15M-T',path)
                self.assertIn('exchange_index=2',path)
                canceled[0]=True; return 200,dict(order_id='late',reduced_by='0')
            if '/portfolio/orders/late' in path:
                return 200,dict(order=dict(order_id='late',fill_count_fp='2.04' if canceled[0] else '0',status='executed' if canceled[0] else 'resting'))
            if '/portfolio/fills?' in path:
                return 200,dict(fills=[dict(order_id='late',count_fp='2.04',yes_price_dollars='0.51',no_price_dollars='0.49',fee_cost='0.03')] if canceled[0] else [],cursor='')
            self.fail(path)
        with patch.object(live,'api',side_effect=api),patch.object(live.time,'sleep'):
            f=L.buy('BTC',tk,'NO',0.1,0.49)
            self.assertIsNotNone(f)
            t=P.openLive('BTC','NO',f,75000,time.time()+300,tk,dict(signalSource='test'),0.07,0.6)
        self.assertEqual(t['contracts'],2.04); self.assertAlmostEqual(t['price'],0.49); self.assertAlmostEqual(t['entryFee'],0.03)
        self.assertEqual(sum(m=='POST' for m,p in calls),1)
        cancel_index=next(i for i,c in enumerate(calls) if c[0]=='DELETE')
        self.assertTrue(any('/portfolio/orders/' in p for m,p in calls[cancel_index+1:]))
        self.assertTrue(any('/portfolio/fills?' in p for m,p in calls[cancel_index+1:]))
        self.assertFalse(L.pending_fills)
    def test_rest_buy_late_fill_is_returned_for_tracking(self):
        L,P,R=self.engine(); L.cash=10; canceled=[False]
        def api(method,path,body=None):
            if method=='POST': return 201,dict(order=dict(order_id='rest-late',fill_count_fp='0',status='resting'))
            if method=='DELETE': canceled[0]=True; return 200,{}
            if path.startswith('/portfolio/orders/'):
                return 200,dict(order=dict(fill_count_fp='2' if canceled[0] else '0',status='executed' if canceled[0] else 'resting'))
            if path.startswith('/portfolio/fills'):
                return 200,dict(fills=[dict(order_id='rest-late',count_fp='2',yes_price_dollars='0.50',no_price_dollars='0.50',fee_cost='0.01')] if canceled[0] else [],cursor='')
            self.fail(path)
        with patch.object(live,'api',side_effect=api),patch.object(live.time,'sleep'):
            fill=L.rest_buy('ETH','KXETH15M-T','NO',0.5,lambda p:0.1,wait=0)
        self.assertTrue(canceled[0]); self.assertEqual(fill['contracts'],2)
        self.assertEqual(fill['price'],0.5); self.assertEqual(fill['fee'],0.01)
    def test_terminal_zero_fill_still_cancel_attempts_and_rechecks(self):
        L,P,R=self.engine(); calls=[]
        def api(method,path,body=None):
            calls.append((method,path))
            if method=='POST': return 201,dict(order=dict(order_id='zero',fill_count_fp='0'))
            if method=='DELETE': return 200,{}
            if path.startswith('/portfolio/orders/'): return 200,dict(order=dict(status='canceled',fill_count_fp='0'))
            if path.startswith('/portfolio/fills'): return 200,dict(fills=[],cursor='')
            self.fail(path)
        with patch.object(live,'api',side_effect=api),patch.object(live.time,'sleep'):
            result=L._order('KXBTC15M-T','NO','buy',2,0.5,confirm=(1,0))
        self.assertEqual(result[1],0)
        idx=next(i for i,c in enumerate(calls) if c[0]=='DELETE')
        self.assertTrue(any('/portfolio/fills?' in p for m,p in calls[idx+1:]))
    def test_adopted_trade_immediately_uses_normal_stop(self):
        L,P,R=self.engine(); state_lock=threading.RLock(); event=threading.Event()
        t=P.adopt('BTC','NO',2,0.49,0,'KXBTC15M-T',75000,time.time()+300,[],False)
        def stop(*a,**kw):
            return 2,0.17,0,'stop',None
        L.stop_sell=stop; L.book=lambda pnl:event.set()
        g=dict(time=time,threading=threading,lock=state_lock,LV=L,pt=P,stop_pct=lambda:0.6,
               lock_active=lambda:False,LOCK_PEAK_BY_ASSET={},LOCK_PEAK=0.8,lock_arm=lambda t:0.18,
               lock_drop=lambda t:0.08,LOCK_HOLD_P=0.9,kalshi_fee=lambda c,p:0)
        code='\n'.join(ast.get_source_segment(SRC,n) for n in TREE.body if getattr(n,'name',None) in ('PaperTrader','_ws_quote'))
        exec(code,g)
        P.__class__=g['PaperTrader']
        g['_ws_quote']('KXBTC15M-T',dict(yesBid=0.82,yesAsk=0.83))
        self.assertTrue(event.wait(1)); self.assertEqual(t['status'],'exited'); self.assertTrue(t['exitReason'].startswith('stop'))
    def test_partial_fill_rechecked_after_cancel(self):
        L,P,R=self.engine(); canceled=[False]
        def api(m,p,b=None):
            if m=='POST': return 201,dict(order=dict(order_id='partial',fill_count_fp='0.50'))
            if m=='DELETE': canceled[0]=True; return 200,{}
            if p.startswith('/portfolio/orders/'):
                return 200,dict(order=dict(fill_count_fp='1.00' if canceled[0] else '0.50',status='canceled' if canceled[0] else 'resting'))
            if p.startswith('/portfolio/fills'): return 200,dict(fills=[],cursor='')
            self.fail(p)
        with patch.object(live,'api',side_effect=api),patch.object(live.time,'sleep'):
            result=L._order('KXETH15M-T','NO','buy',2,0.5,confirm=(1,0))
        self.assertEqual(result[1],1.0); self.assertTrue(canceled[0])
    def test_no_lease_shadow_and_lease_exception_never_submit(self):
        L,P,R=self.engine()
        with patch.object(live,'api',side_effect=AssertionError('unexpected network')):
            L.lease_ok=lambda:False
            self.assertEqual(L._order('KXBTC15M-T','NO','buy',2,0.5)[0],0)
            self.assertEqual(L._order('KXBTC15M-T','NO','sell',2,0.5)[0],0)
            self.assertIsNone(L.rest_buy('BTC','KXBTC15M-T','NO',0.5,lambda p:0.1))
            self.assertFalse(R.run())
            L.lease_ok=lambda:(_ for _ in ()).throw(RuntimeError('mongo down'))
            self.assertFalse(L.owner())
            L.lease_ok=None; self.assertFalse(L.owner())
            L.lease_ok=lambda:True
            self.assertIsNone(L.buy('BTC','KXBTC15M-T','NO',0.1,0.5,shadow=True))
            self.assertIsNone(L.rest_buy('BTC','KXBTC15M-T','NO',0.5,lambda p:0.1,mode='shadow'))
            self.assertEqual(L._order('KXBTC15M-T','NO','sell',2,0.5,shadow=True)[0],0)
    def test_cancel_only_process_owned_ids(self):
        L,P,R=self.engine(); L.own_orders['mine']=dict(ticker='KXBTC15M-T',side='NO',price=0.5,action='buy')
        L.submitted_orders=dict(L.own_orders)
        L.lease_ok=lambda:False; L._lost_cleanup=True
        with patch.object(live,'api',return_value=(200,{})) as api:
            self.assertFalse(L.cancel_order('foreign','KXBTC15M-T'))
            self.assertTrue(L.cancel_order('mine','KXBTC15M-T'))
            self.assertEqual(api.call_count,1)
    def test_failed_positions_query_does_not_invent_flatness(self):
        L,P,R=self.engine()
        with patch.object(live,'api',return_value=(503,{})): R.run()
        self.assertIn('503',R.state['lastError']); self.assertFalse(P.trades)
    def test_inflight_snapshot_is_not_adopted(self):
        L,P,R=self.engine(); tk='KXBTC15M-T'; L.active_orders.add(tk)
        with patch.object(live,'api',side_effect=self.response([dict(ticker=tk,position_fp='2',market_exposure_dollars='1')])): R.run()
        self.assertFalse(P.trades)
    def test_lease_cas_does_not_replace_existing_owner_and_fails_closed(self):
        class Collection:
            def __init__(s): s.doc=None; s.fail=False
            def update_one(s,q,u,upsert=False):
                if s.fail: raise RuntimeError('db down')
                if s.doc is None: s.doc={'_id':'lease',**u['$setOnInsert']}
            def find_one_and_update(s,q,u,return_document=None):
                if s.fail: raise RuntimeError('db down')
                if s.doc['inst']==q['$or'][0]['inst'] or s.doc['hb']<q['$or'][1]['hb']['$lt']:
                    s.doc.update(u['$set']); return dict(s.doc)
        C=Collection(); a=TraderLease(C,'a'); b=TraderLease(C,'b')
        self.assertTrue(a.check()); self.assertFalse(b.check()); self.assertEqual(C.doc['inst'],'a')
        C.doc['hb']=time.time()-46
        self.assertTrue(b.check()); self.assertFalse(a.check()); self.assertTrue(a.retired)
        C.fail=True; self.assertFalse(b.check())

if __name__=='__main__': unittest.main(verbosity=2)
