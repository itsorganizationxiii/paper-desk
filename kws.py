# Kalshi WebSocket price feed (ticker channel). Pushes best bid/ask so the bot doesn't poll REST.
# Purely a data source: callers use quote() when fresh and fall back to REST otherwise. Never raises into callers.
import json, time, threading, collections, queue
CBQ_MAX=50000
TRADE_MAX=200000; TRADE_KEEP=4*3600
WS_URL=None; FRESH=3.0
class Feed:
    def __init__(s,url,headers_fn):
        s.url=url; s.hdr=headers_fn; s.q={}; s.want=set(); s.subbed=set(); s.ws=None; s.connected=False
        s.lastMsg=None; s.err=None; s.lock=threading.Lock(); s.mid=0; s.reconnects=0; s.on_quote=None
        s.trades=collections.deque(maxlen=TRADE_MAX); s.tradeSeen=0; s.tradeLast=None; s.wantAt={}
        s.cbq=queue.Queue(maxsize=CBQ_MAX); s.cbDropped=0
        threading.Thread(target=s._run,daemon=True).start()
        threading.Thread(target=s._cbw,daemon=True).start()
    def _cbw(s):
        # on_quote callbacks (exits, tick recorder, lead-lag) run here so a slow callback never delays recv/quote cache
        while True:
            tk,v=s.cbq.get(); cb=s.on_quote
            if not cb: continue
            try: cb(tk,v)
            except Exception as e: s.err=f'on_quote: {e}'
    def watch(s,ticker):
        if not ticker: return
        with s.lock:
            if ticker in s.want: s.wantAt[ticker]=time.time(); return
            s.want.add(ticker); s.wantAt[ticker]=time.time()
        s._sub([ticker])
    def quote(s,ticker,maxage=FRESH):
        """{'yesBid','yesAsk'} if pushed within maxage seconds (by receipt and Kalshi's own ts), else None."""
        v=s.q.get(ticker); now=time.time()
        if not v or now-v['rx']>maxage or (v.get('ts') and now-v['ts']>maxage+2): return None
        return v
    def _sub(s,tks):
        ws=s.ws
        if not (ws and s.connected and tks): return
        try:
            s.mid+=1; ws.send(json.dumps(dict(id=s.mid,cmd='subscribe',params=dict(channels=['ticker','trade'],market_tickers=list(tks)))))
            s.subbed.update(tks)
        except Exception as e: s.err=f'sub: {e}'
    def _prune(s):
        # drop tickers whose 15-min window is long gone (ticker suffix is the close time; keep the set small)
        with s.lock:
            if len(s.want)>40:
                keep=sorted(s.want,key=lambda t:s.wantAt.get(t,0))[-20:]   # keep the 20 most recently watched
                s.want=set(keep); s.wantAt={t:s.wantAt.get(t,0) for t in keep}
    def _msg(s,raw):
        try: d=json.loads(raw)
        except Exception: return
        s.lastMsg=time.time()
        if d.get('type')=='error': s.err=str(d.get('msg'))[:200]; return
        if d.get('type')=='trade': s._trade(d.get('msg') or {}); return
        if d.get('type')!='ticker': return
        m=d.get('msg') or {}; tk=m.get('market_ticker')
        if not tk: return
        def f(k):
            v=m.get(k+'_dollars')
            if v not in (None,''):
                try: return float(v)
                except Exception: return None
            v=m.get(k); return v/100 if isinstance(v,(int,float)) else None
        old=s.q.get(tk) or {}
        yb=f('yes_bid'); ya=f('yes_ask')
        ts=m.get('ts_ms'); ts=ts/1000 if isinstance(ts,(int,float)) else (m.get('ts') if isinstance(m.get('ts'),(int,float)) else None)
        lp=f('price')
        if ts:
            s.lag=round(time.time()-ts,2)   # consumer lag: receipt/processing time minus Kalshi's update time
            if s.lag>getattr(s,'lagMax',0) or time.time()-getattr(s,'lagMaxAt',0)>60: s.lagMax=s.lag; s.lagMaxAt=time.time()
        s.q[tk]=dict(yesBid=yb if yb is not None else old.get('yesBid'),yesAsk=ya if ya is not None else old.get('yesAsk'),ts=ts,rx=time.time(),last=lp if lp is not None else old.get('last'),
                     noBid=f('no_bid'),noAsk=f('no_ask'))
        if s.on_quote:
            try: s.cbq.put_nowait((tk,dict(s.q[tk])))
            except queue.Full: s.cbDropped+=1
    def _trade(s,m):
        try:
            ts=m.get('ts_ms')
            if not isinstance(ts,(int,float)):
                t2=m.get('ts'); ts=int(t2*1000) if isinstance(t2,(int,float)) else int(time.time()*1000)
            def fl(k):
                try: return float(m.get(k))
                except Exception: return None
            cnt=fl('count_fp')
            if cnt is None: cnt=fl('count')
            yp=fl('yes_price_dollars')
            if yp is None and isinstance(m.get('yes_price'),(int,float)): yp=m['yes_price']/100
            np_=fl('no_price_dollars')
            if np_ is None and isinstance(m.get('no_price'),(int,float)): np_=m['no_price']/100
            s.trades.append(dict(trade_id=m.get('trade_id'),market_ticker=m.get('market_ticker'),yes_price_dollars=yp,no_price_dollars=np_,count_fp=cnt,taker_side=m.get('taker_side'),ts_ms=int(ts),rx_ms=int(time.time()*1000)))
            s.tradeSeen+=1; s.tradeLast=time.time()
            cut=(time.time()-TRADE_KEEP)*1000
            while s.trades and s.trades[0]['rx_ms']<cut: s.trades.popleft()
        except Exception as e: s.err=f'trade: {e}'
    def trades_after(s,after=0,limit=5000):
        out=[]
        for t in list(s.trades):
            if t['ts_ms']>after:
                out.append(t)
        out.sort(key=lambda t:t['ts_ms'])
        return out[:limit]
    def trade_flow(s):
        now=time.time()*1000; n=len(s.trades); old=s.trades[0]['ts_ms'] if n else None
        with s.lock: want=sorted(s.want)[-8:]
        per={}
        tr=[]
        for i in range(1,n+1):
            try: t=s.trades[-i]
            except IndexError: break
            if now-t.get('rx_ms',t['ts_ms'])>120000: break
            tr.append(t)
        tr.reverse()
        for t in tr:
            if now-t['ts_ms']>60000 or t['market_ticker'] not in want: continue
            e=per.setdefault(t['market_ticker'],dict(yes=0.0,no=0.0,n=0)); e['n']+=1
            if t['taker_side'] in ('yes','no'): e[t['taker_side']]=round(e[t['taker_side']]+(t['count_fp'] or 0),2)
        r1=len([t for t in tr if now-t.get('rx_ms',t['ts_ms'])<=60000])
        return dict(bufferSize=n,oldestTsMs=old,tradesLast60s=r1,totalSeen=s.tradeSeen,lastTradeAge=round(now/1000-s.tradeLast,1) if s.tradeLast else None,last60sByTicker=per)
    def _run(s):
        try: import websocket
        except Exception as e: s.err=f'websocket-client missing: {e}'; return
        back=2
        while True:
            try:
                s.ws=websocket.create_connection(s.url,header=s.hdr(),timeout=20)
                s.connected=True; s.err=None; back=2; s.subbed=set()
                with s.lock: tks=list(s.want)
                s._sub(tks)
                while True:
                    try: raw=s.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        s.ws.ping(); continue
                    if raw is None or raw=='': raise ConnectionError('closed')
                    s._msg(raw); s._prune()
            except Exception as e:
                s.err=f'{type(e).__name__}: {str(e)[:150]}'
            s.connected=False; s.q.clear(); s.reconnects+=1
            try: s.ws and s.ws.close()
            except Exception: pass
            s.ws=None; time.sleep(back); back=min(back*2,60)
    def info(s,ticker):
        """Read-only freshness diagnostics for one ticker (no prices)."""
        v=s.q.get(ticker); now=time.time()
        return dict(connected=s.connected,watched=ticker in s.want,have=bool(v),
                    rxAge=round(now-v['rx'],2) if v else None,tsAge=round(now-v['ts'],2) if (v and v.get('ts')) else None,
                    fresh=s.quote(ticker) is not None)
    def status(s):
        return dict(lag=getattr(s,'lag',None),lagMax60=getattr(s,'lagMax',None),cbBacklog=s.cbq.qsize() if hasattr(s,'cbq') else None,cbDropped=getattr(s,'cbDropped',0),connected=s.connected,subscribed=len(s.subbed),quotes=len(s.q),lastMsgAge=round(time.time()-s.lastMsg,1) if s.lastMsg else None,reconnects=s.reconnects,error=s.err)
