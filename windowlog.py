# Owner checklist #1, #2, #4 (Nov 8, 2026). Data only: no trading decisions use this yet (owner: no trading logic until 300+ windows logged).
# #1/#2: Kalshi bid/ask + top-5 depth every BOOK_SEC seconds per open window, each row with synced timestamp, window id, seconds left.
# #4: rebuilt BRTI from a consolidated level-2 order book (mid of 5 BTC depth VWAP; median of venue mids also kept) built from CF-published BRTI constituents (CME CF Constituent Exchanges PDF, docs.cfbenchmarks.com): Bitstamp, Coinbase, Kraken, Gemini, Bullish, Crypto.com over public websockets. LMAX Digital is a constituent but has no public feed; itBit suspended 27-Jul-26.
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
                    parse(json.loads(ws.recv()),ws)
            except Exception as e: ST['venues'][name]=f'down: {type(e).__name__}'; time.sleep(5)
    threading.Thread(target=run,daemon=True).start()
B={}
def _bk(n): return B.setdefault(n,{'bid':{},'ask':{},'t':0})
def _snap(n,bids,asks):
    k=_bk(n); k['bid']={float(p):float(q) for p,q in bids if float(q)>0}; k['ask']={float(p):float(q) for p,q in asks if float(q)>0}; k['t']=time.time()
def _upd(n,side,px,q):
    k=_bk(n); px=float(px); q=float(q)
    if q==0: k[side].pop(px,None)
    else: k[side][px]=q
    k['t']=time.time()
def _p_cb(d,ws=None):
    if d.get('type')=='snapshot': _snap('coinbase',d['bids'],d['asks'])
    elif d.get('type')=='l2update':
        for sd,px,q in d['changes']: _upd('coinbase','bid' if sd=='buy' else 'ask',px,q)
def _p_kr(d,ws=None):
    if not isinstance(d,dict) or d.get('channel')!='book': return
    x=d['data'][0]
    if d.get('type')=='snapshot': _snap('kraken',[(l['price'],l['qty']) for l in x['bids']],[(l['price'],l['qty']) for l in x['asks']])
    else:
        for l in x.get('bids',[]): _upd('kraken','bid',l['price'],l['qty'])
        for l in x.get('asks',[]): _upd('kraken','ask',l['price'],l['qty'])
        k=_bk('kraken')
        for sd,rev in (('bid',True),('ask',False)):
            for px in sorted(k[sd],reverse=rev)[10:]: k[sd].pop(px,None)
def _p_bs(d,ws=None):
    x=d.get('data') or {}
    if d.get('event')=='data' and x.get('bids'): _snap('bitstamp',x['bids'][:50],x['asks'][:50])
def _p_gm2(d,ws=None):
    if d.get('type') not in ('l2_updates','update'): return
    for c in d.get('changes',[]): _upd('gemini','bid' if c[0]=='buy' else 'ask',c[1],c[2])
def _p_cdc(d,ws=None):
    if d.get('method')=='public/heartbeat': ws.send(json.dumps({'id':d.get('id'),'method':'public/respond-heartbeat'})); return
    x=((d.get('result') or {}).get('data') or [None])[0]
    if x and x.get('bids'): _snap('cryptocom',[l[:2] for l in x['bids']],[l[:2] for l in x['asks']])
def _p_bl(d,ws=None):
    x=d.get('data') or {}
    if x.get('bestBid') and x.get('bestAsk'): _snap('bullish',[(x['bestBid'],x.get('bidVolume') or 0.01)],[(x['bestAsk'],x.get('askVolume') or 0.01)])
DEPTH_BTC=5.0
def consolidated(now=None):
    """Consolidated L2 book across fresh venues -> (median of venue mids, depth-weighted mid at DEPTH_BTC, venues used)."""
    now=now or time.time(); live={n:k for n,k in B.items() if now-k['t']<10 and k['bid'] and k['ask'] and max(k['bid'])<min(k['ask'])}
    if len(live)<2: return None,None,0
    mids=[(max(k['bid'])+min(k['ask']))/2 for k in live.values()]; med=statistics.median(mids)
    bids=sorted(((p,q) for k in live.values() for p,q in k['bid'].items() if p>med*0.995),reverse=True)
    asks=sorted((p,q) for k in live.values() for p,q in k['ask'].items() if p<med*1.005)
    def vwap(lv):
        got=cost=0.0
        for p,q in lv:
            t=min(q,DEPTH_BTC-got); got+=t; cost+=t*p
            if got>=DEPTH_BTC: break
        return cost/got if got else None
    vb,va=vwap(bids),vwap(asks)
    return med,((vb+va)/2 if vb and va else None),len(live)
def _sampler():
    while True:
        time.sleep(1-(time.time()%1))
        med,dm,n=consolidated()
        if med: _sec.append((int(time.time()),dm or med,n,med))
def _avg60(end):
    xs=[x[1] for x in _sec if end-60<=x[0]<end]
    return (sum(xs)/len(xs),len(xs)) if xs else (None,0)
def _idx_loop(owner):
    done=set()
    while True:
        time.sleep(5)
        now=time.time(); b=int(now//900)*900
        if b in done or now-b<20 or not owner(): continue
        try:
            avg,n=_avg60(b)
            _m=[x[3] for x in _sec if b-60<=x[0]<b]; med=sum(_m)/len(_m) if _m else None
            opn=[x for x in _get(f'{KALSHI}/markets?series_ticker={IDX_SERIES}&status=open&limit=5')['markets'] if abs(_iso(x['open_time'])-b)<5]
            st=[x for x in _get(f'{KALSHI}/markets?series_ticker={IDX_SERIES}&status=settled&limit=5')['markets'] if abs(_iso(x['close_time'])-b)<5]
            if not opn and not st:
                if now-b>300: done.add(b)
                continue
            fs=opn[0].get('floor_strike') if opn else None
            ev=float(st[0]['expiration_value']) if st and st[0].get('expiration_value') else None
            if ev is None and now-b<240: continue
            kal=fs if fs is not None else ev
            row=dict(boundary=b,ms=int(now*1000),rebuilt=avg,rebuiltMedian=med,errStartMedian=(med-fs if med and fs else None),errEndMedian=(med-ev if med and ev else None),samples=n,floorStrike=fs,expirationValue=ev,
                     startWindow=opn[0]['ticker'] if opn else None,endWindow=st[0]['ticker'] if st else None,
                     endResult=st[0].get('result') if st else None,endStrike=st[0].get('floor_strike') if st else None,
                     errStart=(avg-fs if avg and fs else None),errEnd=(avg-ev if avg and ev else None),venues=dict(ST['venues']))
            ts=[x[0] for x in _sec if b-960<=x[0]<b]
            gap=max((y-x for x,y in zip(ts,ts[1:])),default=999)
            row['maxGapSec']=gap; row['firstSample']=ts[0] if ts else None
            row['complete']=bool(ts and ts[0]<=b-955 and gap<=5 and n>=57 and ev is not None)
            row['incompleteReason']=None if row['complete'] else ('restart/no data' if not ts or ts[0]>b-955 else ('stream gap %ss'%gap if gap>5 else ('few samples %d'%n if n<57 else 'no settlement')))
            if st and avg and st[0].get('floor_strike'):
                row['rebuiltResult']='yes' if avg>=float(st[0]['floor_strike']) else 'no'
            _ins('idxcmp',row); ST['idxRows']+=1; done.add(b)
        except Exception as e: ST['idxErr']=f'{type(e).__name__}: {str(e)[:120]}'
def stats():
    c=_col.get('idxcmp')
    if c is None: return dict(ST,n=0)
    allr=list(c.find({},{'_id':0})); rows=[r for r in allr if r.get('complete')]
    errs=[abs(e) for r in rows for e in (r.get('errStart'),r.get('errEnd')) if e is not None]
    dis=[r for r in rows if r.get('rebuiltResult') and r.get('endResult')]
    out=dict(ST,n=len(rows),excluded=len(allr)-len(rows),errN=len(errs))
    if errs:
        s=sorted(errs); out.update(meanAbsErr=round(sum(s)/len(s),3),stdErr=round(statistics.pstdev(s),3),p95AbsErr=round(s[min(len(s)-1,int(0.95*len(s)))],3))
    if dis: out['upDownDisagree']=round(sum(r['rebuiltResult']!=r['endResult'] for r in dis)/len(dis),4); out['disagreeN']=len(dis)
    return out
def start(db,owner=lambda:True):
    if db is not None:
        _col['books']=db['books']; _col['idxcmp']=db['idxcmp']
        try: _col['books'].create_index([('window',1),('ms',1)]); _col['books'].create_index('ms',expireAfterSeconds=None) if False else None; _col['idxcmp'].create_index('boundary',unique=True)
        except Exception as e: ST['idxErr']=f'index: {e}'
    _ws('coinbase','wss://ws-feed.exchange.coinbase.com',{'type':'subscribe','product_ids':['BTC-USD'],'channels':['level2_batch']},_p_cb)
    _ws('kraken','wss://ws.kraken.com/v2',{'method':'subscribe','params':{'channel':'book','symbol':['BTC/USD'],'depth':10}},_p_kr)
    _ws('bitstamp','wss://ws.bitstamp.net',{'event':'bts:subscribe','data':{'channel':'order_book_btcusd'}},_p_bs)
    _ws('gemini','wss://api.gemini.com/v2/marketdata',{'type':'subscribe','subscriptions':[{'name':'l2','symbols':['BTCUSD']}]},_p_gm2)
    _ws('cryptocom','wss://stream.crypto.com/exchange/v1/market',{'id':1,'method':'subscribe','params':{'channels':['book.BTC_USD.50']}},_p_cdc)
    _ws('bullish','wss://api.exchange.bullish.com/trading-api/v1/market-data/tick/BTCUSD',None,_p_bl)
    for f,a in ((_sampler,()),(_book_loop,(owner,)),(_idx_loop,(owner,))): threading.Thread(target=f,args=a,daemon=True).start()
