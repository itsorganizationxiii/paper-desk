# Owner checklist #1, #2, #4 (Nov 8, 2026). Data only: no trading decisions use this yet (owner: no trading logic until 300+ windows logged).
# #1/#2: Kalshi bid/ask + top-5 depth every BOOK_SEC seconds per open window, each row with synced timestamp, window id, seconds left.
# #4: rebuilt BRTI (median of mids from CF-published BRTI constituent exchanges over public websockets: Coinbase, Kraken, Bitstamp, Gemini).
#     At each window start/end, compare our 60s average to Kalshi floor_strike (start) and expiration_value (end) from
#     GET https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M (status=open / status=settled).
import json, time, threading, statistics, urllib.request, email.utils, collections
import websocket
KALSHI='https://api.elections.kalshi.com/trade-api/v2'
BOOK_SERIES={'BTC':'KXBTC15M','ETH':'KXETH15M','SOL':'KXSOL15M','XRP':'KXXRP15M'}
BOOK_SEC=3
IDX_SERIES='KXBTC15M'
ST=dict(bookRows=0,bookErr=None,clockOffset=None,idxRows=0,idxErr=None,venues={},windows=0)
_col={}; _mids={}; _sec=collections.deque(maxlen=3600)
def _get(url):
    r=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'paper-desk'}),timeout=10)
    d=r.headers.get('Date')
    if d: ST['clockOffset']=round(email.utils.parsedate_to_datetime(d).timestamp()-time.time(),2)
    return json.loads(r.read())
def _iso(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def _ins(name,doc):
    c=_col.get(name)
    if c is None: return
    try: c.insert_one(doc)
    except Exception as e: ST['bookErr' if name=='books' else 'idxErr']=f'{type(e).__name__}: {str(e)[:120]}'
def _lv(levels):
    return [[float(p),float(q)] for p,q in (levels or [])][-5:][::-1]
def _book_loop(owner):
    cur={}
    while True:
        t0=time.time()
        if owner():
            for a,ser in BOOK_SERIES.items():
                try:
                    m=cur.get(a)
                    if not m or m['close']<=time.time():
                        ms=[x for x in _get(f'{KALSHI}/markets?series_ticker={ser}&status=open&limit=5')['markets'] if _iso(x['close_time'])>time.time()]
                        if not ms: continue
                        x=sorted(ms,key=lambda x:x['close_time'])[0]; m=cur[a]=dict(ticker=x['ticker'],close=_iso(x['close_time']),strike=x.get('floor_strike'))
                    ob=_get(f"{KALSHI}/markets/{m['ticker']}/orderbook?depth=5").get('orderbook_fp') or {}
                    yes=_lv(ob.get('yes_dollars')); no=_lv(ob.get('no_dollars'))
                    now=time.time()+(ST['clockOffset'] or 0)
                    yb=yes[0][0] if yes else None; nb=no[0][0] if no else None
                    _ins('books',dict(ms=int(now*1000),asset=a,window=m['ticker'],secsLeft=round(m['close']-now,1),strike=m['strike'],
                        yesBid=yb,yesAsk=(round(1-nb,4) if nb is not None else None),noBid=nb,noAsk=(round(1-yb,4) if yb is not None else None),
                        yesDepth=yes,noDepth=no,clockOffset=ST['clockOffset']))
                    ST['bookRows']+=1
                except Exception as e: ST['bookErr']=f'{a}: {type(e).__name__}: {str(e)[:120]}'
        time.sleep(max(0.2,BOOK_SEC-(time.time()-t0)))
def _ws(name,url,sub,parse):
    def run():
        while True:
            try:
                ws=websocket.create_connection(url,timeout=20)
                if sub: ws.send(json.dumps(sub))
                ST['venues'][name]='up'
                while True:
                    m=parse(json.loads(ws.recv()))
                    if m: _mids[name]=(m,time.time())
            except Exception as e: ST['venues'][name]=f'down: {type(e).__name__}'; time.sleep(5)
    threading.Thread(target=run,daemon=True).start()
def _p_cb(d):
    if d.get('type')=='ticker' and d.get('best_bid') and d.get('best_ask'): return (float(d['best_bid'])+float(d['best_ask']))/2
def _p_kr(d):
    if isinstance(d,dict) and d.get('channel')=='ticker' and d.get('data'): x=d['data'][0]; return (x['bid']+x['ask'])/2
def _p_bs(d):
    x=d.get('data') or {}
    if d.get('event')=='data' and x.get('bids') and x.get('asks'): return (float(x['bids'][0][0])+float(x['asks'][0][0]))/2
_gm={}
def _p_gm(d):
    for e in d.get('events',[]) if d.get('type')=='update' else []:
        if e.get('type')=='change': _gm[e['side']]=float(e['price']) if e.get('reason')!='cancel' or True else None
    return None
_gb={'bid':{},'ask':{}}
def _p_gm2(d):
    if d.get('type')!='l2_updates' and d.get('type')!='update': return None
    for c in d.get('changes',[]):
        side=c[0].replace('buy','bid').replace('sell','ask'); px=float(c[1]); q=float(c[2])
        if q==0: _gb[side].pop(px,None)
        else: _gb[side][px]=q
    if _gb['bid'] and _gb['ask']: return (max(_gb['bid'])+min(_gb['ask']))/2
def _sampler():
    while True:
        time.sleep(1-(time.time()%1))
        now=time.time(); ms=[v for v,t in _mids.values() if now-t<10]
        if len(ms)>=2: _sec.append((int(now),statistics.median(ms),len(ms)))
def _avg60(end):
    xs=[v for t,v,_ in _sec if end-60<=t<end]
    return (sum(xs)/len(xs),len(xs)) if xs else (None,0)
def _idx_loop(owner):
    done=set()
    while True:
        time.sleep(5)
        now=time.time(); b=int(now//900)*900
        if b in done or now-b<20 or not owner(): continue
        try:
            avg,n=_avg60(b)
            opn=[x for x in _get(f'{KALSHI}/markets?series_ticker={IDX_SERIES}&status=open&limit=5')['markets'] if abs(_iso(x['open_time'])-b)<5]
            st=[x for x in _get(f'{KALSHI}/markets?series_ticker={IDX_SERIES}&status=settled&limit=5')['markets'] if abs(_iso(x['close_time'])-b)<5]
            if not opn and not st:
                if now-b>300: done.add(b)
                continue
            fs=opn[0].get('floor_strike') if opn else None
            ev=float(st[0]['expiration_value']) if st and st[0].get('expiration_value') else None
            if ev is None and now-b<240: continue
            kal=fs if fs is not None else ev
            row=dict(boundary=b,ms=int(now*1000),rebuilt=avg,samples=n,floorStrike=fs,expirationValue=ev,
                     startWindow=opn[0]['ticker'] if opn else None,endWindow=st[0]['ticker'] if st else None,
                     endResult=st[0].get('result') if st else None,endStrike=st[0].get('floor_strike') if st else None,
                     errStart=(avg-fs if avg and fs else None),errEnd=(avg-ev if avg and ev else None),venues=dict(ST['venues']))
            if st and avg and st[0].get('floor_strike'):
                row['rebuiltResult']='yes' if avg>=float(st[0]['floor_strike']) else 'no'
            _ins('idxcmp',row); ST['idxRows']+=1; done.add(b)
        except Exception as e: ST['idxErr']=f'{type(e).__name__}: {str(e)[:120]}'
def stats():
    c=_col.get('idxcmp')
    if c is None: return dict(ST,n=0)
    rows=list(c.find({},{'_id':0}))
    errs=[abs(e) for r in rows for e in (r.get('errStart'),r.get('errEnd')) if e is not None]
    dis=[r for r in rows if r.get('rebuiltResult') and r.get('endResult')]
    out=dict(ST,n=len(rows),errN=len(errs))
    if errs:
        s=sorted(errs); out.update(meanAbsErr=round(sum(s)/len(s),3),stdErr=round(statistics.pstdev(s),3),p95AbsErr=round(s[min(len(s)-1,int(0.95*len(s)))],3))
    if dis: out['upDownDisagree']=round(sum(r['rebuiltResult']!=r['endResult'] for r in dis)/len(dis),4); out['disagreeN']=len(dis)
    return out
def start(db,owner=lambda:True):
    if db is not None:
        _col['books']=db['books']; _col['idxcmp']=db['idxcmp']
        try: _col['books'].create_index([('window',1),('ms',1)]); _col['books'].create_index('ms',expireAfterSeconds=None) if False else None; _col['idxcmp'].create_index('boundary',unique=True)
        except Exception as e: ST['idxErr']=f'index: {e}'
    _ws('coinbase','wss://ws-feed.exchange.coinbase.com',{'type':'subscribe','product_ids':['BTC-USD'],'channels':['ticker']},_p_cb)
    _ws('kraken','wss://ws.kraken.com/v2',{'method':'subscribe','params':{'channel':'ticker','symbol':['BTC/USD']}},_p_kr)
    _ws('bitstamp','wss://ws.bitstamp.net',{'event':'bts:subscribe','data':{'channel':'order_book_btcusd'}},_p_bs)
    _ws('gemini','wss://api.gemini.com/v2/marketdata',{'type':'subscribe','subscriptions':[{'name':'l2','symbols':['BTCUSD']}]},_p_gm2)
    for f,a in ((_sampler,()),(_book_loop,(owner,)),(_idx_loop,(owner,))): threading.Thread(target=f,args=a,daemon=True).start()
