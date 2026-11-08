"""Portfolio truth checking. No order submission; adopted trades use the existing engine."""
import time
from datetime import datetime
from urllib.parse import urlencode
import live

CRYPTO=('BTC','ETH','SOL','XRP')

class Reconciler:
    def __init__(self,execution,trader,state_lock):
        self.live=execution; self.trader=trader; self.lock=state_lock
        self.state=dict(intervalSec=15,lastRun=None,lastSuccess=None,adopted=[],mismatches=[],lastError=None,runs=0)
    def run(self):
        L=self.live
        if not (L and L.allowed() and L.owner()): return
        now=time.time(); self.state.update(lastRun=now,runs=self.state['runs']+1)
        mismatches=[]; adopted=[]
        try:
            # Retain unresolved IDs until the exchange confirms their terminal state.
            for oid,info in list(L.own_orders.items()):
                if info['action']!='buy' or info['ticker'] in L.resting or info['ticker'] in L.active_orders: continue
                L.cancel_order(oid,info['ticker'])
                L.order_result(oid,info['side'],info['price'])
            with L._activity: generation=L.position_generation
            rows=L.pages('/portfolio/positions?'+urlencode(dict(count_filter='position',settlement_status='unsettled',exchange_index=live.SHARD,subaccount=0,limit=1000)),'market_positions')
            held={}
            for p in rows:
                tk=p.get('ticker',''); asset=next((a for a in CRYPTO if tk.startswith('KX'+a+'15M-')),None)
                if not asset: continue
                net=float(p.get('position_fp') if p.get('position_fp') not in (None,'') else p.get('position') or 0)
                if not net: continue
                side='YES' if net>0 else 'NO'; qty=round(abs(net),2); held[(tk,side)]=qty
                if live.xrp_blocked(tk):
                    mismatches.append(dict(ticker=tk,side=side,contracts=qty,reason='XRP hard block; not adopted or ordered'))
                    continue
                with L._activity:
                    busy=tk in L.active_orders or tk in L.resting
                pending=any(v['ticker']==tk and now-v['at']<60 for v in list(L.pending_fills.values()))
                if busy or pending:
                    mismatches.append(dict(ticker=tk,reason='execution in flight; retry next reconciliation'))
                    continue
                with self.lock:
                    matches=[t for t in self.trader.trades if t.get('mode')=='live' and not t.get('shadow') and t.get('status')=='open' and t.get('ticker')==tk and t.get('side')==side]
                    tracked=sum(t['contracts'] for t in matches)
                    if abs(tracked-qty)<0.005: continue
                if tracked>qty:
                    mismatches.append(dict(ticker=tk,side=side,actual=qty,tracked=tracked,reason='quantity deficit; not fabricating an exit/PnL'))
                    continue
                m=L.market(tk)
                if m.get('result') in ('yes','no') or m.get('status') in ('settled','finalized'):
                    continue
                close=datetime.fromisoformat(m['close_time'].replace('Z','+00:00')).timestamp()
                fills=L.pages('/portfolio/fills?'+urlencode(dict(ticker=tk,exchange_index=live.SHARD,subaccount=0,limit=1000)),'fills')
                basis=p.get('market_exposure_dollars')
                if basis is None and p.get('market_exposure') is not None: basis=float(p['market_exposure'])/100
                orders=sorted({f['order_id'] for f in fills if f.get('order_id')})
                # Position cost basis is specifically remaining cost excluding fees, never total_traded.
                if basis is None:
                    same=[f for f in fills if str(f.get('side') or f.get('outcome_side') or '').upper()==side and f.get('action','buy')=='buy']
                    total=sum(float(f.get('count_fp') or f.get('count') or 0) for f in same)
                    if total<qty-0.005: raise ValueError('remaining cost basis unavailable')
                    basis=sum(float(f.get('count_fp') or f.get('count') or 0)*float(f[side.lower()+'_price_dollars']) for f in same)/total*qty
                avg=float(basis)/qty
                if not 0<avg<1: raise ValueError('invalid position cost basis')
                fees=0.0; fee_known=False
                # Historical fees include earlier exits, so only allocate when all fills are buys of this holding.
                same=[f for f in fills if f.get('action')=='buy' and str(f.get('side') or f.get('outcome_side') or '').upper()==side]
                total=sum(float(f.get('count_fp') or f.get('count') or 0) for f in same)
                if len(same)==len(fills) and abs(total-qty)<0.005 and fills:
                    fees=sum(float(f.get('fee_cost') or 0) for f in same); fee_known=True
                if not L.owner(): return
                with self.lock:
                    with L._activity:
                        if generation!=L.position_generation:
                            mismatches.append(dict(ticker=tk,reason='exchange orders changed during snapshot; retry'))
                            continue
                    if any(v['ticker']==tk and time.time()-v['at']<60 for v in list(L.pending_fills.values())): continue
                    # Re-evaluate AFTER I/O: strategy callbacks may have recorded the fill meanwhile.
                    matches=[t for t in self.trader.trades if t.get('mode')=='live' and not t.get('shadow') and t.get('status')=='open' and t.get('ticker')==tk and t.get('side')==side]
                    if any(t.get('exiting') for t in matches):
                        mismatches.append(dict(ticker=tk,reason='sell in flight; retry'))
                        continue
                    tracked=sum(t['contracts'] for t in matches); extra=round(qty-tracked,2)
                    if extra<0.005: continue
                    remaining=float(basis)-sum(t['contracts']*t['price'] for t in matches)
                    extra_avg=remaining/extra
                    if not 0<extra_avg<1:
                        mismatches.append(dict(ticker=tk,reason='cost basis disagrees with tracked holding'))
                        continue
                    t=self.trader.adopt(asset,side,extra,extra_avg,fees*extra/qty,tk,m.get('floor_strike'),close,orders,fee_known)
                    ev=dict(id=t['id'],ticker=tk,side=side,contracts=extra,entry=avg,at=now,orderIds=orders)
                    adopted.append(ev)
                L.tried.add(tk); L.wsq(tk)
                self.state['adopted']=(self.state['adopted']+[ev])[-100:]
                L.log(action='reconcile-adopt',**{k:v for k,v in ev.items() if k!='at'})
            with self.lock:
                for t in self.trader.trades:
                    if t.get('mode')!='live' or t.get('shadow') or t.get('status')!='open' or now>=t['closesAt'] or t.get('exiting'): continue
                    if t.get('asset') not in CRYPTO: continue
                    if (t['ticker'],t['side']) not in held:
                        mismatches.append(dict(ticker=t['ticker'],side=t['side'],actual=0,tracked=t['contracts'],reason='tracked holding absent on exchange'))
            self.state.update(lastSuccess=time.time(),lastError=None,mismatches=mismatches)

        except Exception as e:
            self.state.update(lastError=f'{type(e).__name__}: {str(e)[:120]}',mismatches=mismatches)
            L.log(action='reconcile-error',error=self.state['lastError'])
        return adopted
