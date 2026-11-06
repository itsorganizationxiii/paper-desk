# Kalshi WebSocket price feed (ticker channel). Pushes best bid/ask so the bot doesn't poll REST.
# Purely a data source: callers use quote() when fresh and fall back to REST otherwise. Never raises into callers.
import json, time, threading
WS_URL=None; FRESH=3.0
class Feed:
    def __init__(s,url,headers_fn):
        s.url=url; s.hdr=headers_fn; s.q={}; s.want=set(); s.subbed=set(); s.ws=None; s.connected=False
        s.lastMsg=None; s.err=None; s.lock=threading.Lock(); s.mid=0; s.reconnects=0
        threading.Thread(target=s._run,daemon=True).start()
    def watch(s,ticker):
        if not ticker: return
        with s.lock:
            if ticker in s.want: return
            s.want.add(ticker)
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
            s.mid+=1; ws.send(json.dumps(dict(id=s.mid,cmd='subscribe',params=dict(channels=['ticker'],market_tickers=list(tks)))))
            s.subbed.update(tks)
        except Exception as e: s.err=f'sub: {e}'
    def _prune(s):
        # drop tickers whose 15-min window is long gone (ticker suffix is the close time; keep the set small)
        with s.lock:
            if len(s.want)>40: s.want=set(sorted(s.want)[-20:])
    def _msg(s,raw):
        try: d=json.loads(raw)
        except Exception: return
        s.lastMsg=time.time()
        if d.get('type')=='error': s.err=str(d.get('msg'))[:200]; return
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
        s.q[tk]=dict(yesBid=yb if yb is not None else old.get('yesBid'),yesAsk=ya if ya is not None else old.get('yesAsk'),ts=ts,rx=time.time())
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
    def status(s):
        return dict(connected=s.connected,subscribed=len(s.subbed),quotes=len(s.q),lastMsgAge=round(time.time()-s.lastMsg,1) if s.lastMsg else None,reconnects=s.reconnects,error=s.err)
