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
FEED=collections.defaultdict(lambda:dict(connects=0,drops=0,stale=0,lastDrop=None,lastReason=None,exLagMs=None))
STALE_SEC=10
def _ws(name,url,sub,parse):
    def run():
        while True:
            ws=None
            try:
                ws=websocket.create_connection(url,timeout=STALE_SEC)
                if sub: ws.send(json.dumps(sub))
                FEED[name]['connects']+=1; ST['venues'][name]='up'
                B.pop(name,None)  # fresh snapshot after every (re)connect; never trust a book across a drop
                while True:
                    parse(json.loads(ws.recv()),ws)
                    k=B.get(name)
                    if k and k['t'] and time.time()-k['t']>STALE_SEC*3: raise TimeoutError('book stale')
            except Exception as e:
                f=FEED[name]; f['drops']+=1; f['lastDrop']=int(time.time()); f['lastReason']=type(e).__name__
                if 'Timeout' in type(e).__name__: f['stale']+=1
                ST['venues'][name]=f'down: {type(e).__name__}'; B.pop(name,None)
                _ev('drop',venue=name,reason=f'{type(e).__name__}: {str(e)[:80]}')
                try: ws and ws.close()
                except Exception: pass
                time.sleep(3)
    threading.Thread(target=run,daemon=True).start()
def _ev(kind,**kw):
    c=_col.get('feedevents')
    if c is None: return
    try:
        from datetime import datetime, timezone
        c.insert_one(dict(kind=kind,ts=int(time.time()),at=datetime.now(timezone.utc),boot=BOOT,**kw))
    except Exception: pass
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
        _lag('coinbase',d.get('time'))
        for sd,px,q in d['changes']: _upd('coinbase','bid' if sd=='buy' else 'ask',px,q)
def _p_kr(d,ws=None):
    if not isinstance(d,dict) or d.get('channel')!='book': return
    x=d['data'][0]; _lag('kraken',x.get('timestamp'))
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
def _lag(n,iso):
    try: FEED[n]['exLagMs']=int((time.time()-_iso(iso))*1000)
    except Exception: pass
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
BOOT=int(time.time()); _pend=[]
def _sampler():
    """1 Hz rebuilt-index sample, kept in RAM and persisted (idxsec, _id=second) so a restart can resume the window.
    Every instance writes (not only the lease owner): during a Render deploy old and new overlap, and _id dedupes."""
    while True:
        time.sleep(1-(time.time()%1))
        med,dm,n=consolidated(); t=int(time.time())
        if med:
            row=(t,dm or med,n,med); _sec.append(row); _pend.append(row)
        if len(_pend)>=5 and _col.get('idxsec') is not None: _flush()
def _flush():
    from datetime import datetime, timezone
    rows,_pend[:]=list(_pend),[]
    try: _col['idxsec'].insert_many([dict(_id=t,v=v,n=n,med=m,boot=BOOT,at=datetime.now(timezone.utc)) for t,v,n,m in rows],ordered=False)
    except Exception as e:
        if 'E11000' not in str(e) and 'duplicate' not in str(e).lower(): ST['secErr']=f'{type(e).__name__}: {str(e)[:120]}'
def _restore(now=None):
    """On boot, reload the last 20 min of persisted samples so the open window resumes instead of being discarded."""
    c=_col.get('idxsec')
    if c is None: return 0
    now=now or time.time(); have={x[0] for x in _sec}
    got=sorted((d['_id'],d['v'],d['n'],d['med']) for d in c.find({'_id':{'$gte':int(now)-1200}}))
    rows=[r for r in got if r[0] not in have]
    merged=sorted(list(_sec)+rows); _sec.clear(); _sec.extend(merged[-3600:])
    ST['restoredSamples']=len(rows); return len(rows)
def _avg60(end,sec=None):
    xs=[x[1] for x in (sec if sec is not None else _sec) if end-60<=x[0]<end]
    return (sum(xs)/len(xs),len(xs)) if xs else (None,0)
# Counting rule (documented in WINDOWS.md):
#   window = BTC 15-min market closing at boundary b; observed span = [b-960, b) (60s pre-open for floor_strike + 900s).
#   expected = 960 one-second samples.
#   COMPLETE : >=957 samples, max gap <=5s, >=57 samples in the closing 60s, Kalshi expiration_value present.
#   RECOVERED: spanned a restart/deploy, >=912 samples (95%), max gap <=60s, >=57 in closing 60s AND >=57 in the opening 60s, expiration_value present.
#   INCOMPLETE: settlement present but coverage below that. INVALID: no settlement after 30 min, or no data at all.
#   Only COMPLETE and RECOVERED count toward 300. Nothing is interpolated; every window is kept with its reason.
EXPECTED=960
def classify(b,sec,ev,boot=0):
    ts=sorted({x[0] for x in sec if b-EXPECTED<=x[0]<b})
    gaps=[y-x for x,y in zip([b-EXPECTED-1]+ts,ts+[b])]
    gap=max(gaps) if ts else EXPECTED
    n60=sum(1 for t in ts if t>=b-60); o60=sum(1 for t in ts if t<b-900)
    restart=boot>b-EXPECTED
    q=dict(expected=EXPECTED,received=len(ts),maxGapSec=gap,closing60=n60,opening60=o60,spannedRestart=restart,firstSample=ts[0] if ts else None)
    if ev is None: st,why='INVALID','no settlement' if ts else 'no data'
    elif len(ts)>=957 and gap<=5 and n60>=57: st,why='COMPLETE',None
    elif len(ts)>=912 and gap<=60 and n60>=57 and o60>=57: st,why='RECOVERED','gap %ss'%gap
    else: st,why='INCOMPLETE',('no data' if not ts else 'closing 60s has %d'%n60 if n60<57 else 'opening 60s has %d'%o60 if o60<57 else 'gap %ss'%gap if gap>60 else '%d/%d samples'%(len(ts),EXPECTED))
    q.update(status=st,complete=st in ('COMPLETE','RECOVERED'),incompleteReason=why); return q
def _mkt(close_ts):
    ms=_get(f'{KALSHI}/markets?series_ticker={IDX_SERIES}&min_close_ts={close_ts-5}&max_close_ts={close_ts+5}')['markets']
    return ms[0] if ms else None
def finalize(b,now,sec,mkt,boot=0):
    """Build the row for the window closing at b. Returns (row, final). Not final while settlement may still post (<30 min)."""
    end=mkt(b); nxt=mkt(b+900)
    ev=float(end['expiration_value']) if end and end.get('expiration_value') else None
    if ev is None and now-b<1800: return None,False
    fs=float(nxt['floor_strike']) if nxt and nxt.get('floor_strike') is not None else None
    avg,n=_avg60(b,sec); _m=[x[3] for x in sec if b-60<=x[0]<b]; med=sum(_m)/len(_m) if _m else None
    row=dict(_id=f'{IDX_SERIES}-{b}',boundary=b,ms=int(now*1000),rebuilt=avg,rebuiltMedian=med,samples=n,floorStrike=fs,expirationValue=ev,
             startWindow=nxt['ticker'] if nxt else None,endWindow=end['ticker'] if end else None,endResult=end.get('result') if end else None,
             endStrike=float(end['floor_strike']) if end and end.get('floor_strike') is not None else None,
             errStart=(avg-fs if avg and fs else None),errEnd=(avg-ev if avg and ev else None),
             errStartMedian=(med-fs if med and fs else None),errEndMedian=(med-ev if med and ev else None),
             venues=dict(ST['venues']),feeds={k:dict(v) for k,v in FEED.items()},boot=boot)
    row.update(classify(b,sec,ev,boot))
    if avg and row['endStrike']: row['rebuiltResult']='yes' if avg>=row['endStrike'] else 'no'
    return row,True
def save_window(c,row):
    """Idempotent: first final write wins; repeats (other instance, retry, redeploy) are no-ops."""
    r=c.update_one({'boundary':row['boundary']},{'$setOnInsert':row},upsert=True)
    return bool(getattr(r,'upserted_id',None))
def _idx_loop(owner):
    while True:
        time.sleep(5)
        if not owner(): continue
        c=_col.get('idxcmp'); now=time.time(); last=int(now//900)*900
        for b in range(last-1800,last+1,900):   # current boundary and the two before (settlement can lag)
            if now-b<20: continue
            try:
                if c is not None and c.find_one({'boundary':b},{'_id':1}): continue
                row,final=finalize(b,now,list(_sec),_mkt,BOOT)
                if not final: continue
                if c is None or save_window(c,row): ST['idxRows']+=1
                ST['lastWindow']=dict(boundary=b,status=row['status'],reason=row['incompleteReason'])
            except Exception as e: ST['idxErr']=f'{type(e).__name__}: {str(e)[:120]}'
def stats():
    c=_col.get('idxcmp')
    out=dict(ST,feeds={k:dict(v) for k,v in FEED.items()},boot=BOOT)
    if c is None: return dict(out,n=0)
    allr=list(c.find({},{'_id':0,'venues':0,'feeds':0})); rows=[r for r in allr if r.get('complete')]
    by=collections.Counter(r.get('status') or ('LEGACY-COMPLETE' if r.get('complete') else 'LEGACY-EXCLUDED') for r in allr)
    errs=[abs(e) for r in rows for e in (r.get('errStart'),r.get('errEnd')) if e is not None]
    dis=[r for r in rows if r.get('rebuiltResult') and r.get('endResult')]
    out.update(n=len(rows),windows=len(rows),excluded=len(allr)-len(rows),byStatus=dict(by),errN=len(errs),
               recent=[{k:r.get(k) for k in ('boundary','status','incompleteReason','received','maxGapSec','closing60','spannedRestart')} for r in sorted(allr,key=lambda r:r['boundary'])[-8:]])
    if errs:
        s=sorted(errs); out.update(meanAbsErr=round(sum(s)/len(s),3),stdErr=round(statistics.pstdev(s),3),p95AbsErr=round(s[min(len(s)-1,int(0.95*len(s)))],3))
    if dis: out['upDownDisagree']=round(sum(r['rebuiltResult']!=r['endResult'] for r in dis)/len(dis),4); out['disagreeN']=len(dis)
    return out
def start(db,owner=lambda:True):
    if db is not None:
        _col['books']=db['books']; _col['idxcmp']=db['idxcmp']; _col['idxsec']=db['idxsec']; _col['feedevents']=db['feedevents']
        try:
            _col['books'].create_index([('window',1),('ms',1)]); _col['idxcmp'].create_index('boundary',unique=True)
            _col['idxsec'].create_index('at',expireAfterSeconds=3*86400); _col['feedevents'].create_index('at',expireAfterSeconds=14*86400)
        except Exception as e: ST['idxErr']=f'index: {e}'
        try: _restore()
        except Exception as e: ST['secErr']=f'restore: {e}'
        _ev('boot')
    _ws('coinbase','wss://ws-feed.exchange.coinbase.com',{'type':'subscribe','product_ids':['BTC-USD'],'channels':['level2_batch']},_p_cb)
    _ws('kraken','wss://ws.kraken.com/v2',{'method':'subscribe','params':{'channel':'book','symbol':['BTC/USD'],'depth':10}},_p_kr)
    _ws('bitstamp','wss://ws.bitstamp.net',{'event':'bts:subscribe','data':{'channel':'order_book_btcusd'}},_p_bs)
    _ws('gemini','wss://api.gemini.com/v2/marketdata',{'type':'subscribe','subscriptions':[{'name':'l2','symbols':['BTCUSD']}]},_p_gm2)
    _ws('cryptocom','wss://stream.crypto.com/exchange/v1/market',{'id':1,'method':'subscribe','params':{'channels':['book.BTC_USD.50']}},_p_cdc)
    _ws('bullish','wss://api.exchange.bullish.com/trading-api/v1/market-data/tick/BTCUSD',None,_p_bl)
    for f,a in ((_sampler,()),(_book_loop,(owner,)),(_idx_loop,(owner,))): threading.Thread(target=f,args=a,daemon=True).start()
