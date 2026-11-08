import math
from urllib.parse import urlencode
# Live Kalshi execution for Paper Desk. ONE bot: when live (env LIVE_TRADING=1 + keys + not paused) the strategy
# trades the real Kalshi cash directly; otherwise it paper-trades. This module never touches the paper balance.
# Keeps its own small state doc ('live'): paused flag, realized-pnl daily kill switch, order log.
import os, json, time, base64, threading, queue, uuid, urllib.request, urllib.error
from datetime import datetime
BASE=os.environ.get('KALSHI_BASE_URL','https://api.elections.kalshi.com/trade-api/v2').rstrip('/')
ENV_ALLOW=os.environ.get('LIVE_TRADING','0')=='1'
BET_USD=1.00  # flat live stake per bet, set in code (0 = old %-of-balance sizing)
def xrp_blocked(ticker): return 'XRP' in str(ticker or '').upper()
def stake(usd,cash): return min(BET_USD,0.9*cash) if BET_USD>0 else usd
MAX_USD=float(os.environ.get('LIVE_MAX_TRADE_USD','0')); DAILY_LOSS=float(os.environ.get('LIVE_DAILY_LOSS_USD','5'))
_assets=os.environ.get('LIVE_ASSETS','').strip()
ASSETS=None if not _assets or _assets.upper()=='ALL' else {a.strip().upper() for a in _assets.split(',') if a.strip()}
SHARD=int(os.environ.get('LIVE_EXCHANGE_INDEX','2')); SWEEP=os.environ.get('LIVE_SWEEP','1')=='1'; BUY_SLIP=0.02; SELL_BUF=0.02; STOP_LIMIT_BUF=0.02; STOP_LIMIT_TRIES=1; STOP_RETRY_SEC=0.0; STOP_CONFIRM=(1,0); MAX_CHASE=float(os.environ.get('LIVE_MAX_CHASE','0.05'))
def _key_id(): return os.environ.get('KALSHI_API_KEY_ID','').strip()
def load_pem():
    b=os.environ.get('KALSHI_PRIVATE_KEY_B64','').strip()
    if b:
        try: return normalize_pem(base64.b64decode(b).decode())
        except Exception: return None
    k=os.environ.get('KALSHI_PRIVATE_KEY','').strip()
    if len(k)>1 and k[0]==k[-1] and k[0] in '"\'': k=k[1:-1]
    return normalize_pem(k.replace('\\n','\n')) or None
def normalize_pem(k):
    # Render/UI pastes often collapse newlines to spaces or drop them; rebuild canonical PEM framing.
    import re
    if not k: return k
    m=re.search(r'-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----',k,re.S)
    if not m: 
        body=re.sub(r'[^A-Za-z0-9+/=]','',k); label='PRIVATE KEY' if body.startswith('MIIE') and 'MIIEv' in body[:6] else 'RSA PRIVATE KEY'
    else:
        label=m.group(1).strip(); body=re.sub(r'[^A-Za-z0-9+/=]','',m.group(2))
    if not body: return None
    return '-----BEGIN %s-----\n%s\n-----END %s-----\n'%(label,'\n'.join(body[i:i+64] for i in range(0,len(body),64)),label)
_pk=[None,None]
def private_key(pem=None):
    from cryptography.hazmat.primitives import serialization
    pem=pem if pem is not None else load_pem()
    if not pem: raise ValueError('private key missing')
    if pem is _pk[0]: return _pk[1]
    k=serialization.load_pem_private_key(pem.encode(),password=None); _pk[0]=pem; _pk[1]=k; return k
def sign(key,ts_ms,method,path):
    # Kalshi: RSA-PSS SHA256, salt=digest length, over ts+METHOD+/trade-api/v2/path (no query)
    from cryptography.hazmat.primitives import hashes; from cryptography.hazmat.primitives.asymmetric import padding
    path=path.split('?')[0]
    if not path.startswith('/trade-api/'): path='/trade-api/v2'+path
    msg=f'{ts_ms}{method.upper()}{path}'.encode()
    return base64.b64encode(key.sign(msg,padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.DIGEST_LENGTH),hashes.SHA256())).decode()
def keys_present(): return bool(_key_id() and load_pem())
def _http(method,url,headers,body):  # replaceable in tests
    req=urllib.request.Request(url,data=body,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=8) as r: return r.status,json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try: d=json.loads(e.read() or b'{}')
        except Exception: d={}
        return e.code,d
def api(method,path,body=None):
    if method!='GET' and isinstance(body,dict) and xrp_blocked(body.get('ticker')): return 403,{'error':'blocked: XRP is shadow-only, never live'}
    ts=str(int(time.time()*1000))
    h={'Content-Type':'application/json','Accept':'application/json','User-Agent':'paper-desk-live/1.0',
       'KALSHI-ACCESS-KEY':_key_id(),'KALSHI-ACCESS-TIMESTAMP':ts,'KALSHI-ACCESS-SIGNATURE':sign(private_key(),ts,method,path)}
    return _http(method,BASE+path,h,json.dumps(body).encode() if body is not None else None)
def et_day(ts=None):
    from paper_tz import ET
    return datetime.fromtimestamp(ts or time.time(),ET).strftime('%Y-%m-%d')
class Live:
    def __init__(s,store_load=None,store_save=None):
        s.lock=threading.RLock(); s._save=store_save
        st=(store_load() if store_load else None) or {}
        s.paused=bool(st.get('paused',False)); s.day=st.get('day') or et_day(); s.dayPnl=float(st.get('dayPnl',0))
        s.killed=bool(st.get('killed',False)); s.killReason=st.get('killReason'); s.orders=st.get('orders',[])[-200:]
        s.totalPnl=float(st.get('totalPnl',0)); s.tried=set(); s.edgeLogged=set(); s.fracOK=True; s.resting=set(); s.lastBuy=None; s.entry_block=None
        s.cash=None; s.cashAt=None; s.lastError=None; s.ws=None
        s.lease_ok=None
        s.instance=uuid.uuid4().hex[:12]; s.own_orders={}; s.submitted_orders={}; s._lost_cleanup=False
        s.pending_fills={}; s.active_orders=set(); s.position_generation=0; s._activity=threading.RLock()
        if s.allowed():
            try:
                import kws
                url=BASE.replace('https://','wss://').replace('http://','ws://').rsplit('/trade-api',1)[0]+'/trade-api/ws/v2'
                def _hdr():
                    ts=str(int(time.time()*1000))
                    return {'KALSHI-ACCESS-KEY':_key_id(),'KALSHI-ACCESS-TIMESTAMP':ts,'KALSHI-ACCESS-SIGNATURE':sign(private_key(),ts,'GET','/trade-api/ws/v2')}
                s.ws=kws.Feed(url,_hdr)
            except Exception as e: s.lastError=f'ws init: {e}'
    def wsq(s,ticker):
        try:
            if s.ws: s.ws.watch(ticker); return s.ws.quote(ticker)
        except Exception: pass
        return None
    def allowed(s): return ENV_ALLOW and keys_present()
    def owner(s):
        try: ok=s.lease_ok is not None and bool(s.lease_ok())
        except Exception: ok=False
        if not ok and s.own_orders and not s._lost_cleanup:
            s._lost_cleanup=True
            threading.Thread(target=s.cancel_owned,daemon=True).start()
        return ok
    def enabled(s): return s.allowed() and not s.paused and s.owner()
    def position(s,ticker):
        """Net Kalshi position for a ticker: >0 YES contracts, <0 NO contracts, None if unknown."""
        try:
            c,d=api('GET',f'/portfolio/positions?ticker={ticker}')
            if c!=200: return None
            for p in (d or {}).get('market_positions') or []:
                if p.get('ticker')==ticker:
                    v=p.get('position_fp'); return float(v) if v not in (None,'') else float(p.get('position') or 0)
            return 0.0
        except Exception: return None
    def verify(s,ticker,side,want,a):
        """After a fill, read the real Kalshi position and log it; returns the side/size actually held (net)."""
        time.sleep(0.5); p=s.position(ticker)
        if p is None: s.log(action='position-check',error='positions unavailable',expect=f'{side} {want}',**a); return side,want
        held='YES' if p>0 else ('NO' if p<0 else None); n=round(abs(p),2)
        ok=(held==side and n>=want-0.011)
        s.log(action='position-check' if ok else 'position-mismatch',expect=f'{side} {want}',actual=f'{held} {n}' if held else 'flat',error=None if ok else 'Kalshi position differs from recorded trade; recording what Kalshi holds',**a)
        return (held,min(n,want) if held==side else n) if held else (side,0.0)
    def state(s): return dict(paused=s.paused,day=s.day,dayPnl=s.dayPnl,killed=s.killed,killReason=s.killReason,orders=s.orders[-200:],totalPnl=s.totalPnl)
    def persist(s):
        if not s.owner(): return  # stale deploy instance must not overwrite the new instance's order log
        if s._save:
            try: s._save(s.state())
            except Exception as e: s.lastError=f'persist: {e}'
    def _rollday(s):
        d=et_day()
        if d!=s.day: s.day=d; s.dayPnl=0.0; s.killed=False; s.killReason=None; s.persist()
    def reset(s):
        with s.lock: s.day=et_day(); s.dayPnl=0.0; s.killed=False; s.killReason=None; s.lastError=None; s.persist()
    def set_paused(s,p):
        with s.lock: s.paused=bool(p); s.persist()
    def book(s,pnl):
        # kill switch counts ONLY realized pnl from live fills/settlements; balance changes (withdrawals/deposits) are ignored
        with s.lock:
            s._rollday(); s.dayPnl=round(s.dayPnl+pnl,2); s.totalPnl=round(s.totalPnl+pnl,2)
            if DAILY_LOSS>0 and s.dayPnl<=-DAILY_LOSS and not s.killed: s.killed=True; s.killReason=f'realized live loss ${-s.dayPnl:.2f} today >= ${DAILY_LOSS:.2f}'
            s.persist()
    def log(s,**kw):
        with s.lock: s.orders.append(dict(at=time.time(),**kw)); s.orders=s.orders[-200:]; s.persist()
    def balance(s):
        # Kalshi balance endpoint: 1 call/sec, no burst. Space calls >=2s apart and reuse the last value in between.
        with s.lock:
            w=2.0-(time.time()-getattr(s,'_balAt',0))
            if w>0 and s.cash is not None: return s.cash
            if w>0: time.sleep(w)
            s._balAt=time.time()
        c,d=api('GET','/portfolio/balance')
        if c!=200: raise RuntimeError(f'balance HTTP {c}')
        v=float(d['balance_dollars']) if d.get('balance_dollars') is not None else d['balance']/100
        # Kalshi exchange sharding: crypto + commodities markets trade on shard 2 and API orders can only use that shard's cash
        main=0.0
        for b in d.get('balance_breakdown') or []:
            if int(b.get('exchange_index',-1))==SHARD: v=float(b.get('balance') or 0)
            if int(b.get('exchange_index',-1))==0: main=float(b.get('balance') or 0)
        if SWEEP and SHARD!=0 and main>=0.50: s.sweep(main)  # move main-pool cash into the crypto pool, like the Kalshi app does
        s.cash=round(v,2); s.cashAt=time.time(); return s.cash
    def sweep(s,amt):
        if not s.owner() or time.time()-getattr(s,'_sweepAt',0)<60: return
        s._sweepAt=time.time()
        try:
            cc=int(math.floor(amt*100))*100  # centicents, whole cents
            c,d=api('POST','/portfolio/intra_exchange_instance_transfer',dict(client_transfer_id=str(uuid.uuid4()),source='event_contract',destination='event_contract',
                    amount=cc,source_exchange_shard=0,destination_exchange_shard=SHARD))
            s.log(action='pool-transfer',amount=round(cc/10000,2),toShard=SHARD,http=c,error=None if c in (200,201,202) else str(d)[:200])
        except Exception as e: s.log(action='pool-transfer',error=f'{type(e).__name__}: {e}')
    def diag_balance(s):
        # On insufficient_balance: record Kalshi's raw balance fields + resting orders (at most once a minute)
        if time.time()-getattr(s,'_diagAt',0)<60: return
        s._diagAt=time.time()
        try:
            c,d=api('GET','/portfolio/balance'); c2,d2=api('GET','/portfolio/orders?status=resting&limit=50')
            ro=[dict(ticker=o.get('ticker'),side=o.get('side'),count=o.get('remaining_count_fp') or o.get('remaining_count'),price=o.get('yes_price_dollars') or o.get('yes_price')) for o in (d2 or {}).get('orders',[])]
            s.log(action='balance-diag',balance=d if c==200 else f'HTTP {c}',resting=ro[:10],restingCount=len(ro))
        except Exception as e: s.log(action='balance-diag',error=f'{type(e).__name__}: {e}')
    def market(s,ticker):
        c,d=api('GET',f'/markets/{ticker}'); return (d or {}).get('market') or {}
    def _quote(s,ticker,side,kind):
        w=s.wsq(ticker)
        if w and w.get('yesBid') is not None and w.get('yesAsk') is not None:
            v=(w['yesAsk'] if kind=='ask' else w['yesBid']) if side=='YES' else (1-w['yesBid'] if kind=='ask' else 1-w['yesAsk'])
            if 0<v<1: return round(v,4)
        try:
            m=s.market(ticker); k=f"{side.lower()}_{kind}"; v=m.get(k+'_dollars'); v=float(v) if v not in (None,'') else (m[k]/100 if m.get(k) is not None else None)
            return v if v and v>0 else None
        except Exception: return None
    def entry_allowed(s,asset,ticker,shadow=False,mode='live'):
        return (not shadow and mode=='live' and not xrp_blocked(ticker) and not xrp_blocked(asset)
                and s.enabled() and not s.killed and not (s.entry_block and s.entry_block(asset,ticker)))
    def cancel_order(s,oid,ticker):
        # Cleanup exception: a lost owner may cancel ONLY IDs submitted by this process.
        if oid not in s.submitted_orders or s.submitted_orders[oid]['ticker']!=ticker:
            return False
        path=f'/portfolio/events/orders/{oid}?'+urlencode(dict(market_ticker=ticker,exchange_index=SHARD))
        try:
            c,d=api('DELETE',path)
            s.log(action='cancel-own',orderId=oid,ticker=ticker,http=c,error=None if c in (200,204) else 'cancel not confirmed')
            return c in (200,204)
        except Exception as e:
            s.log(action='cancel-own',orderId=oid,ticker=ticker,error=type(e).__name__)
            return False
    def cancel_owned(s):
        for oid,info in list(s.own_orders.items()):
            s.cancel_order(oid,info['ticker'])
            try: s.order_result(oid,info['side'],info['price'])
            except Exception: pass
    @staticmethod
    def fill_count(o):
        return max([0.0]+[float(o.get(k) or 0) for k in ('fill_count','fill_count_fp','taker_fill_count','taker_fill_count_fp','filled_count')])
    def pages(s,path,key):
        rows=[]; cursor=None; seen=set()
        while True:
            url=path+('&' if '?' in path else '?')+urlencode(dict(cursor=cursor)) if cursor else path
            c,d=api('GET',url)
            if c!=200: raise RuntimeError(f'{key} HTTP {c}')
            if key not in d: raise RuntimeError(f'{key} response missing')
            rows.extend(d[key] or []); cursor=d.get('cursor')
            if not cursor: return rows
            if cursor in seen: raise RuntimeError('pagination cursor repeated')
            seen.add(cursor)
    @staticmethod
    def order_fields(o,side,px):
        fc=Live.fill_count(o); avg=o.get('average_fill_price_dollars') or o.get('average_fill_price')
        fpx=px
        if avg not in (None,''):
            v=float(avg); fpx=v if side=='YES' else 1-v
            # V2 averages are YES-book prices, including marketable NO sells.
        elif fc>0 and any(o.get(k) not in (None,'') for k in ('taker_fill_cost_dollars','maker_fill_cost_dollars')):
            fpx=(float(o.get('taker_fill_cost_dollars') or 0)+float(o.get('maker_fill_cost_dollars') or 0))/fc
        fee=float(o['average_fee_paid'])*fc if o.get('average_fee_paid') not in (None,'') else float(o.get('fees_dollars') or (float(o.get('taker_fees_dollars') or 0)+float(o.get('maker_fees_dollars') or 0)))
        return fc,fpx,fee
    def order_result(s,oid,side,px,initial=None,check_fills=True):
        o=initial or {}; status_known=False
        try:
            c,d=api('GET',f'/portfolio/orders/{oid}?exchange_index={SHARD}')
            if c==200 and d.get('order'):
                o={**o,**d['order']}; status_known=True
        except Exception: pass
        fc,fpx,fee=s.order_fields(o,side,px)
        if s.fill_count(initial or {})>fc: fc,fpx,fee=s.order_fields(initial,side,px)
        try:
            if not check_fills: raise LookupError('status-only confirmation')
            fills=s.pages('/portfolio/fills?'+urlencode(dict(order_id=oid,exchange_index=SHARD,subaccount=0,limit=1000)),'fills')
            fills=[f for f in fills if f.get('order_id')==oid]
            qty=sum(float(f.get('count_fp') or f.get('count') or 0) for f in fills)
            if qty>0 and qty>=fc-0.001:
                costs=[]
                for f in fills:
                    v=f.get(side.lower()+'_price_dollars')
                    if v is None:
                        other=f.get(('no' if side=='YES' else 'yes')+'_price_dollars')
                        if other is not None: v=1-float(other)
                    if v is None: raise ValueError('fill price missing')
                    costs.append(float(f.get('count_fp') or f.get('count') or 0)*float(v))
                fc=qty; fpx=sum(costs)/qty; fee=sum(float(f.get('fee_cost') or 0) for f in fills)
        except Exception: pass
        terminal=status_known and str(o.get('status','')).lower() in ('executed','canceled','cancelled')
        if terminal: s.own_orders.pop(oid,None)
        return fc,fpx,round(fee,4),terminal
    def pending(s,ticker,side,fc,price,fee,oid):
        if fc>0:
            s.pending_fills[oid]=dict(ticker=ticker,side=side,contracts=fc,price=price,fee=fee,at=time.time())
    def _order(s,ticker,side,action,count,px,confirm=(3,0.6),shadow=False,mode='live'):
        if shadow or mode!='live' or xrp_blocked(ticker): return 0,0,None,0.0,None,'blocked: shadow-only, never live'
        asset=ticker.split('15M',1)[0].removeprefix('KX')
        book='bid' if (action=='buy')==(side=='YES') else 'ask'; yes=px if side=='YES' else 1-px
        client=f'pd-{s.instance}-{uuid.uuid4().hex[:12]}'
        body=dict(ticker=ticker,client_order_id=client,side=book,count=f'{count:.2f}',price=f'{max(0.01,min(0.99,yes)):.4f}',
                  time_in_force='immediate_or_cancel',self_trade_prevention_type='taker_at_cross')
        if action=='sell': body['reduce_only']=True
        if not s.owner(): return 0,0,None,0.0,None,'stale instance: no lease'
        if action=='buy' and not s.entry_allowed(asset,ticker,shadow,mode): return 0,0,None,0.0,None,'entry blocked'
        with s._activity: s.active_orders.add(ticker); s.position_generation+=1
        try:
            c,d=api('POST','/portfolio/events/orders',body); o=d.get('order',d) if isinstance(d,dict) else {}
            oid=o.get('order_id') or o.get('id'); fc,fpx,fee=s.order_fields(o,side,px)
            if oid:
                s.own_orders[oid]=dict(ticker=ticker,side=side,price=px,action=action,clientId=client)
                s.submitted_orders[oid]=s.own_orders[oid]
                s.log(action='order-submitted',ticker=ticker,side=side,orderId=oid,clientId=client,http=c)
                terminal=False
                for _ in range(confirm[0]):
                    if action=='sell' and fc>0: break
                    if confirm[1]>0: time.sleep(confirm[1])
                    fc,fpx,fee,terminal=s.order_result(oid,side,px,o,check_fills=action=='buy')
                    if fc>=count-0.001 or terminal: break
                if action=='buy' and fc<count-0.001:
                    s.cancel_order(oid,ticker)
                    # A cancel acknowledgment never proves zero fill. Re-read BOTH status and fills.
                    for _ in range(3):
                        f2,p2,fe2,terminal=s.order_result(oid,side,px,o)
                        if f2>=fc: fc,fpx,fee=f2,p2,fe2
                        if fc>=count-0.001: break
                        time.sleep(0.2)
                    if oid in s.own_orders:
                        s.lastError='buy cancellation/status unresolved; reconciliation will retry'
                if action=='buy': s.pending(ticker,side,fc,fpx,fee,oid)
            return c,fc,fpx if fc else None,fee,oid,('buy cancel unresolved' if action=='buy' and oid in s.own_orders and fc<count-0.001 else (None if c in (200,201) else f'order HTTP {c}'))
        finally:
            with s._activity: s.active_orders.discard(ticker); s.position_generation+=1
    def buy(s,asset,ticker,side,frac,ref,sizer=None,shadow=False,mode='live'):
        """Synchronous real buy. Returns dict(contracts,price,fee,cost,orderId,cash) when anything filled, else None (logged)."""
        a=dict(asset=asset,ticker=ticker,side=side)
        if xrp_blocked(ticker) or xrp_blocked(asset): s.lastBuy='blocked'; return None
        if not s.entry_allowed(asset,ticker,shadow,mode): s.lastBuy='blocked'; return None
        with s.lock: s._rollday()
        if ASSETS is not None and asset not in ASSETS: return None
        if ticker in s.tried: return None
        s.tried.add(ticker)
        if s.killed: s.log(action='buy-skip',error=f'kill switch: {s.killReason}',**a); return None
        try: cash=s.cash if (s.cash is not None and s.cashAt and time.time()-s.cashAt<45) else s.balance()
        except Exception as e: s.lastError=f'balance: {e}'; s.log(action='buy-skip',error=s.lastError,**a); return None
        maxpx=min(0.99,round(ref+MAX_CHASE,2)) if sizer is None else 0.99
        usd=frac*cash
        if MAX_USD>0: usd=min(usd,MAX_USD)
        usd=stake(usd,cash)
        c=fc=0; fee=0.0; oid=None; err=None; px=None; n=0; cost=0.0
        for attempt in range(2):
            ask=s._quote(ticker,side,'ask')
            px=round(ask,2) if ask else min(0.99,round(ref+BUY_SLIP,2))
            if sizer is not None:
                f2=sizer(px)
                if f2 is None:
                    if fc: break
                    s.lastBuy='edge'; s.tried.discard(ticker)  # edge gone at the live price; re-check next tick
                    if ticker not in s.edgeLogged: s.edgeLogged.add(ticker); s.log(action='buy-skip',error=f'edge gone at live ask {px} (signal {ref}); will keep checking',**a)
                    return None
                if not fc: frac=f2; usd=f2*cash; usd=min(usd,MAX_USD) if MAX_USD>0 else usd; usd=stake(usd,cash)
            if px>maxpx:
                err=f'ask {px} above max {maxpx} (signal {ref})'
                if not fc: s.log(action='buy-skip',error=err,**a); return None
                break
            if s.fracOK is not False: n=round(math.floor((usd/px-fc)*100)/100,2); mn=0.01
            else: n=int(usd//px)-int(fc); mn=1
            if n<mn:
                if not fc: s.log(action='buy-skip',error=f'<{mn} contract affordable (cash ${cash:.2f}, frac {frac:.3f}, px {px})',**a); return None
                break
            c,f1,p1,fe1,oid,err=s._order(ticker,side,'buy',n,px)
            if s.fracOK is None and n!=int(n):
                if err and c not in (200,201) and not f1 and 'insufficient' not in str(err):
                    s.fracOK=False; s.log(action='frac-test',error=f'Kalshi rejected fractional count {n}: {err}; using whole contracts',**a)
                    err=None; n2=int(usd//px)
                    if n2<1: s.log(action='buy-skip',error=f'<1 whole contract affordable (cash ${cash:.2f}, frac {frac:.3f}, px {px})',**a); return None
                    c,f1,p1,fe1,oid,err=s._order(ticker,side,'buy',n2,px); n=n2
                elif c in (200,201): s.fracOK=True; s.log(action='frac-test',error=f'Kalshi accepted fractional count {n} (filled {f1})',**a)
            if f1>0: cost+=f1*(p1 if p1 is not None else px); fc+=f1; fee+=fe1
            if f1>=n or err: break
            time.sleep(1.0)
        if err: s.lastError=f'buy HTTP {c}: {err}'
        if err and 'insufficient' in str(err): s.diag_balance()
        s.log(action='buy',orderId=oid,want=n,limit=px,filled=fc,fillPrice=round(cost/fc,4) if fc else None,fee=fee,http=c,error=err or (None if fc else 'no fill'),**a)
        if not fc: s.lastBuy='nofill' if (c in (200,201) or not err) else 'error'; return None
        s.cashAt=None
        s.verify(ticker,side,fc,a)
        return dict(contracts=fc,price=round(cost/fc,4),fee=round(fee,4),cost=round(cost,4),orderId=oid,cash=cash,frac=frac,side=side)
    def rest_buy(s,asset,ticker,side,ref,sizer,wait=30,close_at=None,shadow=False,mode='live'):
        a=dict(asset=asset,ticker=ticker,side=side)
        if not s.entry_allowed(asset,ticker,shadow,mode): return None
        with s.lock:
            if ticker in s.resting: return None
            s.resting.add(ticker)
        oid=None
        try:
            px=round(ref,2); f=sizer(px)
            if not f: return None
            cash=s.cash if s.cash is not None else s.balance()
            usd=f*cash; usd=min(usd,MAX_USD) if MAX_USD>0 else usd; usd=stake(usd,cash)
            n=round(math.floor(usd/px*100)/100,2) if s.fracOK is not False else int(usd//px)
            if n<(0.01 if s.fracOK is not False else 1): return None
            book='bid' if side=='YES' else 'ask'; yes=px if side=='YES' else 1-px
            client=f'pd-{s.instance}-{uuid.uuid4().hex[:12]}'
            body=dict(ticker=ticker,client_order_id=client,side=book,count=f'{n:.2f}',price=f'{max(0.01,min(0.99,yes)):.4f}',
                      time_in_force='good_till_canceled',self_trade_prevention_type='taker_at_cross',expiration_ts=int(time.time()+wait+2))
            if not s.entry_allowed(asset,ticker,shadow,mode): return None
            with s._activity: s.position_generation+=1
            c,d=api('POST','/portfolio/events/orders',body); o=(d or {}).get('order',d) if isinstance(d,dict) else {}
            oid=o.get('order_id') or o.get('id')
            if oid:
                s.own_orders[oid]=dict(ticker=ticker,side=side,price=px,action='buy',clientId=client)
                s.submitted_orders[oid]=s.own_orders[oid]
            s.log(action='rest-buy-placed',orderId=oid,clientId=client,want=n,limit=px,http=c,error=None if c in (200,201) else f'HTTP {c}',**a)
            if not oid: return None
            fc,fpx,fee,terminal=s.order_result(oid,side,px,o); end=time.time()+wait
            while not terminal and fc<n-0.001 and time.time()<end and s.owner():
                time.sleep(min(1,max(0,end-time.time())))
                f2,p2,fe2,terminal=s.order_result(oid,side,px,o)
                if f2>=fc: fc,fpx,fee=f2,p2,fe2
            if fc<n-0.001:
                s.cancel_order(oid,ticker)
                for _ in range(3):
                    f2,p2,fe2,terminal=s.order_result(oid,side,px,o)
                    if f2>=fc: fc,fpx,fee=f2,p2,fe2
                    if fc>=n-0.001: break
                    time.sleep(0.2)
            s.pending(ticker,side,fc,fpx,fee,oid)
            s.log(action='rest-buy',orderId=oid,want=n,limit=px,filled=fc,fillPrice=fpx if fc else None,fee=fee,http=c,error=None if fc else ('no fill; canceled and rechecked' if terminal else 'no fill; cancellation unresolved'),**a)
            if not fc: return None
            s.tried.add(ticker); s.cashAt=None
            return dict(contracts=fc,price=round(fpx,4),fee=fee,cost=round(fc*fpx,4),orderId=oid,cash=cash,frac=f)
        except Exception as e:
            if oid: s.cancel_order(oid,ticker)
            s.log(action='rest-buy',orderId=oid,error=type(e).__name__,**a); return None
        finally:
            s.resting.discard(ticker)
            with s._activity: s.position_generation+=1
    def sell(s,ticker,side,count,bid,floor=None,fast=False,skip_pos=False):
        """Synchronous real sell of up to count. Returns (filled, avg price, fee, orderId, error)."""
        if xrp_blocked(ticker): return 0,None,0.0,None,'blocked: XRP is shadow-only, never live'
        c=fc=0; fee=0.0; oid=None; err=None; px=None; proceeds=0.0
        p=None if skip_pos else s.position(ticker)
        if p is not None:
            have=p if side=='YES' else -p
            if have<=0.0:
                s.log(action='sell',ticker=ticker,side=side,want=count,filled=0,error=f'Kalshi holds no {side} here (net {p}); not selling'); return 0,None,0.0,None,'no position'
            count=min(count,round(have,2))
        for attempt in range(3):
            left=round(count-fc,2)
            if left<=0: break
            q=s._quote(ticker,side,'bid') or bid
            px=max(0.01,round(q-SELL_BUF*(attempt+1),2))
            if floor is not None and px<floor:
                if attempt==0 and q<floor: err=f'live bid {q} below floor {floor} (entry+1c); holding instead of selling at a loss'; break
                px=floor
            c,f1,p1,fe1,oid,err=s._order(ticker,side,'sell',left,px,confirm=(1,0.4) if fast else (3,0.6))
            if f1>0: proceeds+=f1*(p1 if p1 is not None else px); fc+=f1; fee+=fe1
            if err: break
            if fc<count: time.sleep(0.15 if fast else 0.7)
        if err: s.lastError=f'sell HTTP {c}: {err}'
        if fc: s.cashAt=None
        s.log(action='sell',ticker=ticker,side=side,orderId=oid,want=count,limit=px,filled=fc,fillPrice=round(proceeds/fc,4) if fc else None,fee=fee,http=c,error=err or (None if fc>=count else 'partial/no fill; rest held'))
        return fc,(round(proceeds/fc,4) if fc else None),round(fee,4),oid,err
    def stop_sell(s,ticker,side,count,stop,bid,trig_at=None):
        """One protected IOC if the trigger bid permits; otherwise an immediate marketable IOC.

        Exchange reduce_only bounds sales to the actual position without a blocking
        position/quote REST preflight. Each IOC gets at most one immediate status
        confirmation; no retry sleeps, fill pagination, or unrelated quote fetches.
        """
        if xrp_blocked(ticker): return 0,None,0.0,None,'blocked: XRP is shadow-only, never live'
        trig_at=trig_at or time.time(); lim=max(0.01,round(stop-STOP_LIMIT_BUF,2))
        fc=0.0; fee=0.0; proceeds=0.0; oid=None; err=None; tries=0
        if bid is not None and bid>=stop:
            tries=1
            c,f1,p1,fe1,oid,err=s._order(ticker,side,'sell',count,lim,confirm=STOP_CONFIRM)
            if f1>0: proceeds+=f1*(p1 if p1 is not None else lim); fc+=f1; fee+=fe1
        lim_fc=fc; fb=False; fallback_at=None
        if fc<count-0.001 and not (err and 'stale instance' in str(err)):
            fb=True; fallback_at=time.time()
            c,f2,p2,fe2,o2,err=s._order(ticker,side,'sell',round(count-fc,2),0.01,confirm=STOP_CONFIRM)
            if f2>0: proceeds+=f2*(p2 if p2 is not None else 0.01); fc+=f2; fee+=fe2; oid=o2 or oid
        if fc: s.cashAt=None
        if err: s.lastError=f'stop sell: {err}'
        fpx=round(proceeds/fc,4) if fc else None
        s.log(action='stop-sell',ticker=ticker,side=side,want=count,stopLevel=round(stop,4),limit=lim,bidAtTrigger=bid,triggerAt=round(trig_at,3),limitTries=tries,limitFilled=lim_fc,
              fallback=fb,fallbackSubmitAt=round(fallback_at,3) if fallback_at else None,elapsedMs=round((time.time()-trig_at)*1000),filled=fc,fillPrice=fpx,fillAt=round(time.time(),3),slipC=round((stop-fpx)*100,1) if fpx is not None else None,fee=round(fee,4),error=err)
        return fc,fpx,round(fee,4),oid,err
    def status(s):
        with s.lock: s._rollday()
        if s.allowed() and (s.cashAt is None or time.time()-s.cashAt>60):
            try:
                s.balance()
                if (s.lastError or '').startswith('balance'): s.lastError=None
            except Exception as e: s.lastError=f'balance: {type(e).__name__}: {e}'; s.cashAt=time.time()-55  # retry in ~5s
        return dict(enabled=s.enabled(),leaseHeld=s.allowed() and s.owner(),envAllow=ENV_ALLOW,keysPresent=keys_present(),paused=s.paused,baseUrl=BASE,cash=s.cash,
            todayPnl=s.dayPnl,totalPnl=s.totalPnl,day=s.day,killSwitch=dict(tripped=s.killed,reason=s.killReason,dailyLossLimit=DAILY_LOSS),
            maxTradeUsd=MAX_USD,maxChase=MAX_CHASE,assets=sorted(ASSETS) if ASSETS else 'all',fractional=s.fracOK,ws=(s.ws.status() if s.ws else None),lastError=s.lastError,recentOrders=s.orders[-20:][::-1])
