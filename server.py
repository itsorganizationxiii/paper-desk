# PAPER ONLY - simulated trades against real public data. NEVER places real orders.
# No order endpoints are ever called; Kalshi is used read-only for prices/results.
import json, os, time, math, threading, urllib.request, shutil
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
APP=os.path.dirname(os.path.abspath(__file__))
DATA=os.environ.get('DATA_DIR',APP); os.makedirs(DATA,exist_ok=True)
TF=os.path.join(DATA,'trades.json'); SEED=os.path.join(APP,'seed_trades.json')
PORT=int(os.environ.get('PORT','8080')); START=float(os.environ.get('START_BALANCE','10'))
KEY=os.environ.get('ELOEDGE_API_KEY','')
KALSHI='https://api.elections.kalshi.com/trade-api/v2'; CB='https://api.exchange.coinbase.com'
ELO='https://eloedge.com/api/v1/windows'; HERMES='https://hermes.pyth.network'
SERIES={'BTC':'KXBTC15M','ETH':'KXETH15M'}
PYTH={'GOLD':'765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2','SILVER':'f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e','OIL':'f2464139c45f26097e0173b03207c4a490dbd8bf1b72dfbf064eeb832c7c2933'}
WHALE_MIN=float(os.environ.get('WHALE_MIN_USD','250000')); WHALE_WIN=60; WHALE_STRONG=float(os.environ.get('WHALE_STRONG_USD','250000')); WHALE_MAX_GIVEUP=0.10
MIN_EDGE=0.03; EXIT_EDGE=0.03; STOP=0.40; KELLY=0.25; CAP=0.08; TICK=int(os.environ.get('TICK_SEC','10'))
def get(url,auth=False):
    h={'User-Agent':'paper-desk/1.1','Accept':'application/json'}
    if auth: h['Authorization']='Bearer '+KEY
    return json.load(urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=8))
def iso(s): return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def fnum(m,k):
    try: return float(m.get(k) or 0)
    except Exception: return 0.0
class PaperTrader:
    def __init__(s):
        if not os.path.exists(TF) and os.path.exists(SEED): shutil.copy(SEED,TF)
        if os.path.exists(TF): d=json.load(open(TF)); s.balance=d['balance']; s.start=d.get('startBalance',START); s.trades=d['trades']
        else: s.balance=START; s.start=START; s.trades=[]; s.save()
    def save(s):
        tmp=TF+'.tmp'; json.dump({'balance':round(s.balance,4),'startBalance':s.start,'trades':s.trades},open(tmp,'w'),indent=1); os.replace(tmp,TF)
    def openTrade(s,asset,side,ask,p_yes,strike,close,ticker,meta):
        q=p_yes if side=='YES' else 1-p_yes; edge=q-ask
        if edge<MIN_EDGE or not (0<ask<1): return None
        f=min(KELLY*edge/(1-ask),CAP); bal=s.balance; stake=round(bal*f,2)
        if stake<0.05: return None
        s.balance-=stake
        t=dict(id=len(s.trades)+1,asset=asset,side=side,price=round(ask,4),stake=stake,stakePct=round(100*stake/bal,2),contracts=round(stake/ask,3),
               edge=round(edge,3),modelP=round(q,4),strike=strike,ticker=ticker,openedAt=time.time(),closesAt=close,status='open',**meta)
        s.trades.append(t); s.save(); print('OPEN',t['id'],ticker,side,ask,stake,flush=True); return t
    def close(s,t,win,how,val=None):
        payout=t['contracts'] if win else 0
        t.update(status='won' if win else 'lost',settle=val,settledBy=how,pnl=round(payout-t['stake'],2),closedAt=time.time()); s.balance+=payout
        for k in('markPrice','unrealizedPnl','flipWarning','signalsAgree','whaleAgainst'): t.pop(k,None)
    def exit(s,t,bid,reason):
        proceeds=t['contracts']*bid
        t.update(status='exited',exitPrice=round(bid,4),exitReason=reason,settledBy='early-exit@bid',pnl=round(proceeds-t['stake'],2),closedAt=time.time())
        for k in('markPrice','unrealizedPnl','flipWarning','signalsAgree','whaleAgainst'): t.pop(k,None)
        s.balance+=proceeds; s.save(); print('EXIT',t['id'],reason,bid,flush=True)
    def settle(s,spot):
        now=time.time(); ch=False
        for t in s.trades:
            if t['status']!='open' or now<t['closesAt']: continue
            if t.get('ticker'):
                try:
                    m=get(f"{KALSHI}/markets/{t['ticker']}")['market']
                    if m.get('result') in('yes','no'):
                        s.close(t,(m['result']=='yes')==(t['side']=='YES'),'kalshi',m.get('expiration_value')); ch=True; continue
                except Exception as e: print('kalshi settle err',e,flush=True)
                if now<t['closesAt']+600: continue
            if t['asset'] in spot:
                s.close(t,(spot[t['asset']]>=t['strike'])==(t['side']=='YES'),'coinbase-spot',spot[t['asset']]); ch=True
        if ch: s.save()
    def getStats(s):
        c=[t for t in s.trades if t['status']!='open']; w=[t for t in c if t.get('pnl',0)>0]
        o=[t for t in s.trades if t['status']=='open']
        return dict(mode='paper',balance=round(s.balance,2),startBalance=s.start,
            equity=round(s.balance+sum(t.get('markPrice',t['price'])*t['contracts'] for t in o),2),
            unrealizedPnl=round(sum(t.get('unrealizedPnl',0) for t in o),2),
            winRate=round(len(w)/len(c),3) if c else 0,expectancy=round(sum(t['pnl'] for t in c)/len(c),3) if c else 0,
            closed=len(c),open=len(o),trades=s.trades[::-1])
pt=PaperTrader(); lock=threading.Lock()
status={'eloedge':'unknown','sources':{},'signals':{},'commodities':{},'lastTick':None,'fallbackInUse':None,
 'rules':f'Entry: 15m momentum & taker CVD agree AND EloEdge model_p agrees (Coinbase-only fallback if EloEdge unavailable); edge>={MIN_EDGE:.0%}; quarter Kelly, cap {CAP:.0%}. Whale: skip entries against strong net whale flow; exit at bid on strong whale flow against (not if <90s left & mark>=0.85, nor if bid < model-10%). Exit early at bid if signals+EloEdge flip with exit edge>={EXIT_EDGE:.0%}, or stop if mark<={STOP:.0%} of entry.'}
def coinbase(a):
    c=get(f'{CB}/products/{a}-USD/candles?granularity=900')
    spot=c[0][4]; mom=(c[0][4]-c[1][3])/c[1][3]
    tr=get(f'{CB}/products/{a}-USD/trades?limit=1000')
    cvd=sum(float(x['size'])*(1 if x['side']=='sell' else -1) for x in tr); vol=sum(float(x['size']) for x in tr)  # side=maker side
    # whale detection: taker trades far above recent distribution within last WHALE_WIN sec
    usd=sorted(float(x['size'])*float(x['price']) for x in tr); p99=usd[int(0.99*(len(usd)-1))] if usd else 0
    thr=max(WHALE_MIN,p99); now=time.time(); wh=[]
    for x in tr:
        u=float(x['size'])*float(x['price'])
        if u>=thr:
            ts=iso(x['time'])
            if now-ts<=WHALE_WIN: wh.append(dict(side='buy' if x['side']=='sell' else 'sell',usd=round(u),price=float(x['price']),ago=round(now-ts)))
    net=sum(w['usd'] if w['side']=='buy' else -w['usd'] for w in wh)
    wdir='YES' if net>=WHALE_STRONG else 'NO' if net<=-WHALE_STRONG else None
    return dict(spot=spot,momentum=round(mom,5),cvd=round(cvd,4),cvdRatio=round(cvd/vol,3) if vol else 0,
        whale=dict(thresholdUsd=round(thr),p99Usd=round(p99),count=len(wh),netFlowUsd=round(net),strongDir=wdir,alerts=wh[:10]))
def mk(m): return dict(ticker=m['ticker'],strike=m.get('floor_strike'),close=iso(m['close_time']),yesAsk=fnum(m,'yes_ask_dollars'),yesBid=fnum(m,'yes_bid_dollars'),noAsk=fnum(m,'no_ask_dollars'),noBid=fnum(m,'no_bid_dollars'),result=m.get('result') or '')
def kalshi(a):
    ms=[m for m in get(f"{KALSHI}/markets?series_ticker={SERIES[a]}&status=open&limit=5")['markets'] if iso(m['close_time'])>time.time()]
    return mk(sorted(ms,key=lambda m:m['close_time'])[0]) if ms else None
def eloedge(a):
    if not KEY: return 'unavailable',{}
    d=get(f'{ELO}?asset={a}',auth=True); return 'live',{w['market']['ticker']:w for w in d.get('windows',[])}
def pyth():
    q='&'.join('ids[]='+i for i in PYTH.values()); now=time.time()
    cur=get(f'{HERMES}/v2/updates/price/latest?{q}&parsed=true')['parsed']; out={}
    for n,i in PYTH.items():
        x=next((y for y in cur if y['id']==i),None)
        if x: out[n]=dict(price=round(int(x['price']['price'])*10**x['price']['expo'],3),age_sec=round(now-x['price']['publish_time']))
    return out
def heur(cb): return 1/(1+math.exp(-(cb['momentum']*300+cb['cvdRatio']*1.5)))
last_pyth=[0]
def tick_once():
    src={}; spot={}; sigs={}
    if time.time()-last_pyth[0]>60:
        last_pyth[0]=time.time()
        try: status['commodities']=pyth(); src['pyth']='live'
        except Exception as e: src['pyth']=f'error: {e}'
    else: src['pyth']=status['sources'].get('pyth','unknown')
    for a in SERIES:
        sg={}
        try: cb=coinbase(a); sg.update(cb); spot[a]=cb['spot']; src['coinbase']='live'
        except Exception as e: src['coinbase']=f'error: {e}'; sigs[a]=dict(action='skip: coinbase down'); continue
        try: k=kalshi(a); src['kalshi']='live' if k else 'no open market'; sg['kalshi']=k
        except Exception as e: k=None; src['kalshi']=f'error: {e}'
        try: est,wins=eloedge(a)
        except Exception as e: est,wins='unavailable',{}; print('eloedge err',e,flush=True)
        src['eloedge']=est
        mom,cvd=cb['momentum'],cb['cvd']
        dirn='YES' if mom>0 and cvd>0 else 'NO' if mom<0 and cvd<0 else None
        sg['direction']=dirn or 'disagree'
        def fresh(tk):
            w=wins.get(tk)
            return w['model_p'] if w and w.get('model_p') is not None and (w.get('age_sec') or 99)<60 and w.get('state')=='open' else None
        # ---- monitor open positions for this asset ----
        for t in [t for t in pt.trades if t['status']=='open' and t['asset']==a and t.get('ticker') and time.time()<t['closesAt']]:
            try: m=k if k and k['ticker']==t['ticker'] else mk(get(f"{KALSHI}/markets/{t['ticker']}")['market'])
            except Exception as e: print('mark err',e,flush=True); continue
            bid=m['yesBid'] if t['side']=='YES' else m['noBid']
            mp=fresh(t['ticker']); p_yes=mp if mp is not None else (heur(cb) if est!='live' else None)
            q=None if p_yes is None else (p_yes if t['side']=='YES' else 1-p_yes)
            sig_against=dirn is not None and dirn!=t['side']
            elo_against=(mp is not None and (mp>0.5)!=(t['side']=='YES')) or est!='live'
            agree=dirn==t['side'] and (mp is None and est!='live' or mp is not None and (mp>0.5)==(t['side']=='YES'))
            with lock:
                t.update(markPrice=round(bid,4),unrealizedPnl=round(t['contracts']*bid-t['stake'],2),signalsAgree=bool(agree),
                         flipWarning=bool(sig_against or (mp is not None and (mp>0.5)!=(t['side']=='YES'))),currentModelP=q)
                wnet=cb['whale']['netFlowUsd']*(1 if t['side']=='YES' else -1); left_t=t['closesAt']-time.time()
                whale_against=cb['whale']['strongDir'] not in (None,t['side'])
                t.update(whaleFlowUsd=wnet,whaleAgainst=whale_against,whaleCount=cb['whale']['count'])
                if bid>0 and bid<=STOP*t['price']: pt.exit(t,bid,f'stop: mark {bid:.3f} <= {STOP:.0%} of entry {t["price"]}')
                elif whale_against and bid>0 and not (left_t<90 and bid>=0.85) and (q is None or bid>=q-WHALE_MAX_GIVEUP):
                    pt.exit(t,bid,f'whale: net whale flow ${wnet:,.0f} against {t["side"]}, sold at bid {bid:.3f}')
                elif sig_against and elo_against and q is not None and bid-q>=EXIT_EDGE:
                    pt.exit(t,bid,f'flip: momentum/CVD {dirn} + {"EloEdge" if est=="live" else "fallback"} p={q:.3f} vs bid {bid:.3f}')
        if not k: sg['action']='skip: no real Kalshi market'; sigs[a]=sg; continue
        mp=fresh(k['ticker']); sg['modelP']=mp
        left=k['close']-time.time(); sg['secsToClose']=round(left)
        if not dirn: sg['action']='skip: momentum/CVD disagree'
        elif est=='live' and mp is None: sg['action']='skip: no fresh EloEdge model_p'
        elif mp is not None and (mp>0.5)!=(dirn=='YES'): sg['action']=f'skip: EloEdge disagrees (model_p {mp})'
        elif cb['whale']['strongDir'] not in (None,dirn): sg['action']=f"skip: strong whale flow against (${cb['whale']['netFlowUsd']:,.0f})"
        elif left<60: sg['action']='skip: <60s to close'
        elif any(t.get('ticker')==k['ticker'] for t in pt.trades): sg['action']='holding/traded this window'
        else:
            used='coinbase+eloedge'
            if mp is None: mp=heur(cb); used='coinbase-fallback (EloEdge unavailable)'
            ask=k['yesAsk'] if dirn=='YES' else k['noAsk']
            with lock: t=pt.openTrade(a,dirn,ask,mp,k['strike'],k['close'],k['ticker'],dict(priceSource='kalshi',signalSource=used,whaleFlowAtEntry=cb['whale']['netFlowUsd'],mom=mom,cvd=cvd,eloedge=est))
            q=mp if dirn=='YES' else 1-mp
            sg['action']=f'OPENED #{t["id"]}' if t else f'skip: edge {q-ask:+.3f} < {MIN_EDGE}'
        sigs[a]=sg
    with lock: pt.settle(spot)
    status['whaleAlerts']=sorted([dict(asset=a,**w) for a,g in sigs.items() for w in (g.get('whale') or {}).get('alerts',[])],key=lambda w:w['ago'])[:12]
    fb=src.get('eloedge')!='live'
    status.update(sources=src,signals=sigs,lastTick=time.time(),eloedge=src.get('eloedge','unknown'),fallbackInUse=fb,
        signalMode='FALLBACK: Coinbase momentum+CVD only (EloEdge unavailable)' if fb else 'Coinbase momentum+CVD + EloEdge model_p')
def loop():
    while True:
        try: tick_once()
        except Exception as e: print('tick err',e,flush=True)
        time.sleep(TICK)
class H(SimpleHTTPRequestHandler):
    def __init__(s,*a,**k): super().__init__(*a,directory=APP,**k)
    def log_message(s,*a): pass
    def do_GET(s):
        p=s.path.split('?')[0]
        if p=='/api/status':
            with lock: d=pt.getStats(); d['signalStatus']=status; d['eloedge']=status['eloedge']; d['serverTime']=time.time(); b=json.dumps(d).encode()
            s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.send_header('Access-Control-Allow-Origin','*'); s.end_headers(); s.wfile.write(b)
        elif p=='/healthz': s.send_response(200); s.end_headers(); s.wfile.write(b'ok')
        elif p in('/','/index.html','/manifest.json'): super().do_GET()
        else: s.send_error(404)
threading.Thread(target=loop,daemon=True).start()
print('Paper Desk (PAPER ONLY) on port',PORT,'data',TF,flush=True)
ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
