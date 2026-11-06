import math
# Live Kalshi execution for Paper Desk. ONE bot: when live (env LIVE_TRADING=1 + keys + not paused) the strategy
# trades the real Kalshi cash directly; otherwise it paper-trades. This module never touches the paper balance.
# Keeps its own small state doc ('live'): paused flag, realized-pnl daily kill switch, order log.
import os, json, time, base64, threading, queue, uuid, urllib.request, urllib.error
from datetime import datetime
BASE=os.environ.get('KALSHI_BASE_URL','https://api.elections.kalshi.com/trade-api/v2').rstrip('/')
ENV_ALLOW=os.environ.get('LIVE_TRADING','0')=='1'
MAX_USD=float(os.environ.get('LIVE_MAX_TRADE_USD','0')); DAILY_LOSS=float(os.environ.get('LIVE_DAILY_LOSS_USD','5'))
_assets=os.environ.get('LIVE_ASSETS','').strip()
ASSETS=None if not _assets or _assets.upper()=='ALL' else {a.strip().upper() for a in _assets.split(',') if a.strip()}
SHARD=int(os.environ.get('LIVE_EXCHANGE_INDEX','2')); SWEEP=os.environ.get('LIVE_SWEEP','1')=='1'; BUY_SLIP=0.02; SELL_BUF=0.02; MAX_CHASE=float(os.environ.get('LIVE_MAX_CHASE','0.05'))
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
        s.totalPnl=float(st.get('totalPnl',0)); s.tried=set(); s.edgeLogged=set(); s.fracOK=True; s.resting=set(); s.lastBuy=None
        s.cash=None; s.cashAt=None; s.lastError=None; s.ws=None
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
    def enabled(s): return s.allowed() and not s.paused
    def state(s): return dict(paused=s.paused,day=s.day,dayPnl=s.dayPnl,killed=s.killed,killReason=s.killReason,orders=s.orders[-200:],totalPnl=s.totalPnl)
    def persist(s):
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
        if time.time()-getattr(s,'_sweepAt',0)<60: return
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
    def _order(s,ticker,side,action,count,px):
        # V2 single YES book: buy YES=bid@p, buy NO=ask@(1-p); sell YES=ask@p, sell NO=bid@(1-p)
        book='bid' if (action=='buy')==(side=='YES') else 'ask'; yes=px if side=='YES' else 1-px
        body=dict(ticker=ticker,client_order_id=f'pd-{uuid.uuid4().hex[:20]}',side=book,count=f'{count:.2f}',price=f'{max(0.01,min(0.99,yes)):.4f}',
                  time_in_force='immediate_or_cancel',self_trade_prevention_type='taker_at_cross')
        if action=='sell': body['reduce_only']=True
        c,d=api('POST','/portfolio/events/orders',body); o=d.get('order',d) if isinstance(d,dict) else {}
        def _fc(o):
            best=0.0
            for k in ('fill_count','fill_count_fp','taker_fill_count','taker_fill_count_fp','filled_count'):
                try: best=max(best,float(o.get(k) or 0))
                except Exception: pass
            return best
        fc=_fc(o); oid0=o.get('order_id') or o.get('id')
        if c in (200,201) and fc==0 and oid0:
            # IOC responses can return before fills are reported (a real 1-contract fill was missed); confirm from the order
            for _ in range(3):
                time.sleep(0.6)
                try:
                    c2,d2=api('GET',f'/portfolio/orders/{oid0}'); o2=(d2 or {}).get('order') or {}
                    if o2: o={**o,**o2}; fc=_fc(o)
                    if fc>0 or str(o2.get('status','')) in ('canceled','cancelled','executed'): break
                except Exception: pass
        avg=o.get('average_fill_price') or o.get('average_fill_price_dollars')
        fpx=None
        if fc>0:
            if avg in (None,''): fpx=px
            else:
                a=float(avg); fpx=a if side=='YES' else 1-a
                # V2 prices are YES-book; if the side-price reading is far off our limit, the API returned side terms
                if side!='YES' and abs(a-px)<abs(fpx-px): fpx=a
        fee=float(o['average_fee_paid'])*fc if o.get('average_fee_paid') not in (None,'') else float(o.get('taker_fees_dollars') or o.get('fees_dollars') or 0)
        return c,fc,fpx,round(fee,4),o.get('order_id') or oid0,(None if c in (200,201) else json.dumps(d)[:200])
    def buy(s,asset,ticker,side,frac,ref,sizer=None):
        """Synchronous real buy. Returns dict(contracts,price,fee,cost,orderId,cash) when anything filled, else None (logged)."""
        a=dict(asset=asset,ticker=ticker,side=side)
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
                if not fc: frac=f2; usd=f2*cash; usd=min(usd,MAX_USD) if MAX_USD>0 else usd
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
        return dict(contracts=fc,price=round(cost/fc,4),fee=round(fee,4),cost=round(cost,4),orderId=oid,cash=cash,frac=frac)
    def rest_buy(s,asset,ticker,side,ref,sizer,wait=30,close_at=None):
        """Resting limit buy at the signal price: waits up to `wait`s for Kalshi to fill, then cancels the rest."""
        a=dict(asset=asset,ticker=ticker,side=side)
        if ticker in s.resting or s.killed: return None
        s.resting.add(ticker)
        try:
            px=round(ref,2); f=sizer(px)
            if not f: return None
            cash=s.cash if s.cash is not None else s.balance()
            usd=f*cash; usd=min(usd,MAX_USD) if MAX_USD>0 else usd
            n=round(math.floor(usd/px*100)/100,2) if s.fracOK is not False else int(usd//px)
            if n<(0.01 if s.fracOK is not False else 1): return None
            book='bid' if side=='YES' else 'ask'; yes=px if side=='YES' else 1-px
            body=dict(ticker=ticker,client_order_id=f'pd-{uuid.uuid4().hex[:20]}',side=book,count=f'{n:.2f}',price=f'{max(0.01,min(0.99,yes)):.4f}',
                      time_in_force='good_till_canceled',self_trade_prevention_type='taker_at_cross',expiration_ts=int(time.time()+wait+2))
            c,d=api('POST','/portfolio/events/orders',body); o=(d or {}).get('order',d) if isinstance(d,dict) else {}
            oid=o.get('order_id') or o.get('id')
            if c not in (200,201) or not oid:
                s.log(action='rest-buy',want=n,limit=px,filled=0,http=c,error=json.dumps(d)[:200],**a); return None
            fc=0.0; o2={}
            t0=time.time()
            while time.time()-t0<wait:
                time.sleep(3)
                try:
                    c2,d2=api('GET',f'/portfolio/orders/{oid}'); o2=(d2 or {}).get('order') or {}
                    fc=max([fc]+[float(o2.get(k) or 0) for k in ('fill_count','fill_count_fp','filled_count')])
                    if fc>=n-1e-9 or str(o2.get('status','')) in ('executed','canceled','cancelled'): break
                except Exception: pass
            cancel=[]
            if fc<n-1e-9:
                for p in (f'/portfolio/orders/{oid}',f'/portfolio/events/orders/{oid}'):
                    try:
                        cc,dd=api('DELETE',p); cancel.append(f'{p.split("/")[2]}:{cc}')
                        if cc in (200,201,204): break
                    except Exception as e: cancel.append(f'err {e}')
                # never lose track of an order: if it is still resting, keep watching until it fills/cancels/expires or the window ends
                end=time.time()+max(0,close_at-time.time()) if close_at else time.time()+5
                while True:
                    time.sleep(0.5 if not cancel or time.time()<t0+wait+3 else 5)
                    try:
                        c2,d2=api('GET',f'/portfolio/orders/{oid}'); o2=(d2 or {}).get('order') or o2
                        fc=max([fc]+[float(o2.get(k) or 0) for k in ('fill_count','fill_count_fp','filled_count')])
                    except Exception: pass
                    stt=str(o2.get('status',''))
                    if stt!='resting' or fc>=n-1e-9 or time.time()>end: break
                    if time.time()>t0+wait+3:
                        try: api('DELETE',f'/portfolio/orders/{oid}')
                        except Exception: pass
            avg=o2.get('average_fill_price') or o2.get('average_fill_price_dollars'); fpx=px
            if fc and avg not in (None,''):
                v=float(avg); fpx=v if side=='YES' else 1-v
                if side!='YES' and abs(v-px)<abs(fpx-px): fpx=v
            fee=float(o2['average_fee_paid'])*fc if o2.get('average_fee_paid') not in (None,'') else float(o2.get('taker_fees_dollars') or o2.get('maker_fees_dollars') or o2.get('fees_dollars') or 0)
            s.log(action='rest-buy',orderId=oid,want=n,limit=px,filled=fc,fillPrice=fpx if fc else None,fee=round(fee,4),http=c,error=((f'no fill in {wait}s' if not fc else 'filled while resting')+(f'; cancel {cancel} status {o2.get("status")}' if cancel else '')) if (not fc or cancel) else None,**a)
            if not fc: return None
            s.tried.add(ticker); s.cashAt=None
            return dict(contracts=fc,price=round(fpx,4),fee=round(fee,4),cost=round(fc*fpx,4),orderId=oid,cash=cash,frac=f)
        except Exception as e:
            s.log(action='rest-buy',error=f'{type(e).__name__}: {e}',**a); return None
        finally: s.resting.discard(ticker)
    def sell(s,ticker,side,count,bid,floor=None):
        """Synchronous real sell of up to count. Returns (filled, avg price, fee, orderId, error)."""
        c=fc=0; fee=0.0; oid=None; err=None; px=None; proceeds=0.0
        for attempt in range(3):
            left=round(count-fc,2)
            if left<=0: break
            q=s._quote(ticker,side,'bid') or bid
            px=max(0.01,round(q-SELL_BUF*(attempt+1),2))
            if floor is not None and px<floor:
                if attempt==0 and q<floor: err=f'live bid {q} below floor {floor} (entry+1c); holding instead of selling at a loss'; break
                px=floor
            c,f1,p1,fe1,oid,err=s._order(ticker,side,'sell',left,px)
            if f1>0: proceeds+=f1*(p1 if p1 is not None else px); fc+=f1; fee+=fe1
            if err: break
            if fc<count: time.sleep(0.7)
        if err: s.lastError=f'sell HTTP {c}: {err}'
        if fc: s.cashAt=None
        s.log(action='sell',ticker=ticker,side=side,orderId=oid,want=count,limit=px,filled=fc,fillPrice=round(proceeds/fc,4) if fc else None,fee=fee,http=c,error=err or (None if fc>=count else 'partial/no fill; rest held'))
        return fc,(round(proceeds/fc,4) if fc else None),round(fee,4),oid,err
    def status(s):
        with s.lock: s._rollday()
        if s.allowed() and (s.cashAt is None or time.time()-s.cashAt>60):
            try:
                s.balance()
                if (s.lastError or '').startswith('balance'): s.lastError=None
            except Exception as e: s.lastError=f'balance: {type(e).__name__}: {e}'; s.cashAt=time.time()  # back off 60s on errors (avoid 429 storms)
        return dict(enabled=s.enabled(),envAllow=ENV_ALLOW,keysPresent=keys_present(),paused=s.paused,baseUrl=BASE,cash=s.cash,
            todayPnl=s.dayPnl,totalPnl=s.totalPnl,day=s.day,killSwitch=dict(tripped=s.killed,reason=s.killReason,dailyLossLimit=DAILY_LOSS),
            maxTradeUsd=MAX_USD,maxChase=MAX_CHASE,assets=sorted(ASSETS) if ASSETS else 'all',fractional=s.fracOK,ws=(s.ws.status() if s.ws else None),lastError=s.lastError,recentOrders=s.orders[-20:][::-1])
