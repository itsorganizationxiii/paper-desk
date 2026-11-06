# OPTIONAL live Kalshi execution that mirrors Paper Desk trades. Fully separate from paper:
# never touches the paper balance or paper trade history; keeps its own state/log ('live' doc).
# Live only when env LIVE_TRADING=1 (master allow) AND keys present AND dashboard toggle not paused.
import os, json, time, base64, threading, queue, uuid, urllib.request, urllib.error
from datetime import datetime
BASE=os.environ.get('KALSHI_BASE_URL','https://api.elections.kalshi.com/trade-api/v2').rstrip('/')
ENV_ALLOW=os.environ.get('LIVE_TRADING','0')=='1'
MAX_USD=float(os.environ.get('LIVE_MAX_TRADE_USD','0')); DAILY_LOSS=float(os.environ.get('LIVE_DAILY_LOSS_USD','5'))
_assets=os.environ.get('LIVE_ASSETS','').strip()
ASSETS=None if not _assets or _assets.upper()=='ALL' else {a.strip().upper() for a in _assets.split(',') if a.strip()}
BUY_SLIP=0.02; SELL_BUF=0.02; MAX_CHASE=float(os.environ.get('LIVE_MAX_CHASE','0.05'))
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
        s.lock=threading.Lock(); s._save=store_save
        st=(store_load() if store_load else None) or {}
        s.paused=bool(st.get('paused',False)); s.day=st.get('day') or et_day(); s.dayPnl=float(st.get('dayPnl',0))
        s.killed=bool(st.get('killed',False)); s.killReason=st.get('killReason'); s.orders=st.get('orders',[])[-200:]
        s.positions=st.get('positions',{}); s.totalPnl=float(st.get('totalPnl',0))
        s.cash=None; s.cashAt=None; s.lastError=None; s.q=queue.Queue()
        threading.Thread(target=s._worker,daemon=True).start()
    def enabled(s): return ENV_ALLOW and keys_present() and not s.paused
    def state(s): return dict(paused=s.paused,day=s.day,dayPnl=s.dayPnl,killed=s.killed,killReason=s.killReason,orders=s.orders[-200:],positions=s.positions,totalPnl=s.totalPnl)
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
    # ---- hooks called by paper engine (non-blocking; never raise) ----
    def on_open(s,t,bal_paper_before):
        try:
            if not s.enabled() or (ASSETS is not None and t['asset'] not in ASSETS): return
            s.q.put(('buy',dict(id=t['id'],asset=t['asset'],ticker=t['ticker'],side=t['side'],price=t['price'],frac=t['stake']/bal_paper_before if bal_paper_before else 0)))
        except Exception as e: s.lastError=f'on_open: {e}'
    def on_exit(s,t,bid,reason):
        try:
            if not ENV_ALLOW or not keys_present(): return  # exits still run when paused so nothing is left stranded
            if s.positions.get(t['ticker']): s.q.put(('sell',dict(id=t['id'],ticker=t['ticker'],side=t['side'],bid=bid,reason=(reason or '').split(':')[0])))
        except Exception as e: s.lastError=f'on_exit: {e}'
    def on_settle(s,t,result_yes):
        with s.lock:
            p=s.positions.pop(t.get('ticker'),None)
            if not p: return
            win=result_yes==(p['side']=='YES'); pnl=round((p['count'] if win else 0)-p['cost']-p.get('fees',0),2)
            s._book(pnl); s.orders.append(dict(at=time.time(),paperId=p['paperId'],ticker=t['ticker'],action='settle',side=p['side'],count=p['count'],result='won' if win else 'lost',pnl=pnl)); s.persist()
        t['live']=dict(t.get('live') or {},settled='won' if win else 'lost',pnl=pnl)
    def _book(s,pnl):
        s.dayPnl=round(s.dayPnl+pnl,2); s.totalPnl=round(s.totalPnl+pnl,2)
        if DAILY_LOSS>0 and s.dayPnl<=-DAILY_LOSS and not s.killed: s.killed=True; s.killReason=f'realized live loss ${-s.dayPnl:.2f} today >= ${DAILY_LOSS:.2f}'
    # ---- worker ----
    def _worker(s):
        while True:
            kind,a=s.q.get()
            try: s._buy(a) if kind=='buy' else s._sell(a)
            except Exception as e: s.lastError=f'{kind}: {type(e).__name__}: {e}'; s._log(a,kind,error=s.lastError)
    def _log(s,a,action,**kw):
        with s.lock: s.orders.append(dict(at=time.time(),paperId=a.get('id'),ticker=a.get('ticker'),action=action,side=a.get('side'),**kw)); s.orders=s.orders[-200:]; s.persist()
    def balance(s):
        c,d=api('GET','/portfolio/balance')
        if c!=200: raise RuntimeError(f'balance HTTP {c}')
        v=float(d['balance_dollars']) if d.get('balance_dollars') is not None else d['balance']/100
        s.cash=round(v,2); s.cashAt=time.time(); return s.cash
    def _quote(s,ticker,side,kind):
        try:
            c,d=api('GET',f'/markets/{ticker}'); m=(d or {}).get('market') or {}
            k=f"{side.lower()}_{kind}"; v=m.get(k+'_dollars'); v=float(v) if v not in (None,'') else (m[k]/100 if m.get(k) is not None else None)
            return v if v and v>0 else None
        except Exception: return None
    def _order(s,ticker,side,action,count,px):
        # V2 single YES book: buy YES=bid@p, buy NO=ask@(1-p); sell YES=ask@p, sell NO=bid@(1-p)
        book='bid' if (action=='buy')==(side=='YES') else 'ask'; yes=px if side=='YES' else 1-px
        body=dict(ticker=ticker,client_order_id=f'pd-{uuid.uuid4().hex[:20]}',side=book,count=f'{count:.2f}',price=f'{max(0.01,min(0.99,yes)):.4f}',
                  time_in_force='immediate_or_cancel',self_trade_prevention_type='taker_at_cross')
        if action=='sell': body['reduce_only']=True
        c,d=api('POST','/portfolio/events/orders',body); o=d.get('order',d) if isinstance(d,dict) else {}
        fc=float(o.get('fill_count') or o.get('fill_count_fp') or 0)
        avg=o.get('average_fill_price') or o.get('average_fill_price_dollars')
        fpx=None
        if fc>0: fpx=(float(avg) if side=='YES' else 1-float(avg)) if avg not in (None,'') else px
        fee=float(o.get('taker_fees_dollars') or o.get('fees_dollars') or 0)
        return c,fc,fpx,fee,o.get('order_id'),(None if c in (200,201) else json.dumps(d)[:200])
    def _buy(s,a):
        with s.lock: s._rollday()
        if s.killed: return s._log(a,'buy-skip',error=f'kill switch: {s.killReason}')
        if a['ticker'] in s.positions: return
        cash=s.cash if (s.cash is not None and s.cashAt and time.time()-s.cashAt<45) else s.balance()
        maxpx=min(0.99,round(a['price']+MAX_CHASE,2))  # never chase more than MAX_CHASE above the paper entry
        usd=a['frac']*cash  # exact paper fraction (quarter Kelly, 8% cap, night halving) x live cash
        if MAX_USD>0: usd=min(usd,MAX_USD)  # optional cap; 0 = none
        c=fc=0; fpx=None; fee=0.0; oid=None; err=None; px=None; n=0; cost=0.0
        for attempt in range(2):
            ask=s._quote(a['ticker'],a['side'],'ask')
            px=round(ask,2) if ask else min(0.99,round(a['price']+BUY_SLIP,2))
            if px>maxpx:
                err=f'ask {px} above max {maxpx} (paper {a["price"]}); skipped'
                if attempt==0 and not fc: return s._log(a,'buy-skip',error=err)
                break
            n=int(usd//px)-int(fc)
            if n<1:
                if not fc: return s._log(a,'buy-skip',error=f'<1 contract affordable (cash ${cash:.2f}, frac {a["frac"]:.3f}, px {px})')
                break
            c,f1,p1,fe1,oid,err=s._order(a['ticker'],a['side'],'buy',n,px)
            if f1>0: cost+=f1*(p1 if p1 is not None else px); fc+=f1; fee+=fe1
            if f1>=n or err: break
            time.sleep(1.0)
        fpx=round(cost/fc,4) if fc else None
        if fc: s.cashAt=None
        with s.lock:
            if fc>0: s.positions[a['ticker']]=dict(paperId=a['id'],side=a['side'],count=fc,cost=round(fc*fpx,4),fees=fee,openedAt=time.time())
            if err: s.lastError=f'buy HTTP {c}: {err}'
        s._log(a,'buy',orderId=oid,want=n,limit=px,filled=fc,fillPrice=fpx,fee=fee,http=c,error=err)
        s._tag(a['id'],dict(orderId=oid,want=n,filled=fc,fillPrice=fpx,error=err or (None if fc else 'no fill')))
    def _sell(s,a):
        p=s.positions.get(a['ticker'])
        if not p: return
        c=fc=0; fee=0.0; oid=None; err=None; px=None; proceeds=0.0
        for attempt in range(3):
            left=round(p['count']-fc,2)
            if left<=0: break
            bid=s._quote(a['ticker'],p['side'],'bid') or a['bid']
            px=max(0.01,round(bid-SELL_BUF*(attempt+1),2))
            c,f1,p1,fe1,oid,err=s._order(a['ticker'],p['side'],'sell',left,px)
            if f1>0: proceeds+=f1*(p1 if p1 is not None else px); fc+=f1; fee+=fe1
            if err: break
            time.sleep(0.7)
        fpx=round(proceeds/fc,4) if fc else None
        if fc: s.cashAt=None
        with s.lock:
            pnl=None
            if fc>0:
                share=fc/p['count']; pnl=round(fc*fpx-fee-p['cost']*share-p.get('fees',0)*share,2); s._book(pnl)
                p['count']=round(p['count']-fc,2); p['cost']*=1-share; p['fees']=p.get('fees',0)*(1-share)
                if p['count']<=0: s.positions.pop(a['ticker'],None)
            if err: s.lastError=f'sell HTTP {c}: {err}'
        s._log(a,'sell',reason=a['reason'],orderId=oid,limit=px,filled=fc,fillPrice=fpx,fee=fee,pnl=pnl,http=c,error=err or (None if fc else 'no fill; position held to settlement'))
        s._tag(a['id'],dict(sellOrderId=oid,sellFilled=fc,sellPrice=fpx,pnl=pnl,sellError=err or (None if fc else 'no fill')))
    tag_cb=None
    def _tag(s,pid,d):
        if s.tag_cb:
            try: s.tag_cb(pid,d)
            except Exception: pass
    def status(s):
        with s.lock: s._rollday()
        if ENV_ALLOW and keys_present() and (s.cashAt is None or time.time()-s.cashAt>60):
            try:
                s.balance()
                if (s.lastError or '').startswith('balance:'): s.lastError=None
            except Exception as e: s.lastError=f'balance: {type(e).__name__}: {e}'; s.cashAt=time.time()  # back off 60s on errors (avoid 429 storms)
        return dict(enabled=s.enabled(),envAllow=ENV_ALLOW,keysPresent=keys_present(),paused=s.paused,baseUrl=BASE,cash=s.cash,
            todayPnl=s.dayPnl,totalPnl=s.totalPnl,day=s.day,killSwitch=dict(tripped=s.killed,reason=s.killReason,dailyLossLimit=DAILY_LOSS),
            maxTradeUsd=MAX_USD,assets=sorted(ASSETS) if ASSETS else 'all',openPositions=s.positions,lastError=s.lastError,recentOrders=s.orders[-20:][::-1])
