# One strategy engine, two modes. PAPER (default): simulated trades/balance, exactly as before.
# LIVE (env LIVE_TRADING=1 + Kalshi keys + dashboard not paused): same engine trades the real Kalshi cash; trades are
# recorded only when contracts actually fill (mode='live'). Live never touches the paper balance (it stays frozen).
import json, os, time, math, threading, urllib.request, shutil, csv, io
from datetime import timezone, timedelta
try:
    from zoneinfo import ZoneInfo; ET=ZoneInfo('America/New_York')
except Exception: ET=timezone(timedelta(hours=-5))
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
APP=os.path.dirname(os.path.abspath(__file__))
DATA=os.environ.get('DATA_DIR',APP); os.makedirs(DATA,exist_ok=True)
TF=os.path.join(DATA,'trades.json'); SEED=os.path.join(APP,'seed_trades.json')
PORT=int(os.environ.get('PORT','8080')); START=float(os.environ.get('START_BALANCE','10'))
KEY=os.environ.get('ELOEDGE_API_KEY','')
KALSHI='https://api.elections.kalshi.com/trade-api/v2'; CB='https://api.exchange.coinbase.com'
ELO='https://eloedge.com/api/v1/windows'; HERMES='https://hermes.pyth.network'
SERIES={'BTC':'KXBTC15M','ETH':'KXETH15M','SOL':'KXSOL15M','GOLD':'KXGOLD15M','SILVER':'KXSILVER15M'}
METALS={'GOLD':'PF_XAUUSD','SILVER':'PF_XAGUSD'}  # spot/momentum/flow from Kraken public futures (Coinbase has no metals)
KF='https://futures.kraken.com'; ELO_COM='https://eloedge.com/api/v1/commodity15m/windows'; ELO_COM_TTL=int(os.environ.get('ELO_COM_TTL_SEC','30'))
PYTH={'GOLD':'765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2','SILVER':'f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e','OIL':'f2464139c45f26097e0173b03207c4a490dbd8bf1b72dfbf064eeb832c7c2933'}
WHALE_WIN=60; WHALE_MAX_GIVEUP=0.10
# per-asset whale cutoff, calibrated to 6h of Coinbase trades (Nov 2-3 2026): BTC max $200k, p99.99 $101k; ETH max $125k (outlier), p99.99 $38k
WHALE_CFG={'BTC':dict(floor=float(os.environ.get('WHALE_FLOOR_BTC','100000')),cap=float(os.environ.get('WHALE_CAP_BTC','150000')),p99mult=10,volfrac=0.5),
           'ETH':dict(floor=float(os.environ.get('WHALE_FLOOR_ETH','35000')),cap=float(os.environ.get('WHALE_CAP_ETH','60000')),p99mult=4,volfrac=0.5),
           'SOL':dict(floor=float(os.environ.get('WHALE_FLOOR_SOL','25000')),cap=float(os.environ.get('WHALE_CAP_SOL','45000')),p99mult=4,volfrac=0.5)}
# take-profit: sell at bid (minus Kalshi taker fee) once our side's bid >= TP_BID, unless very confident it won't flip
# profit lock arms once the bid is LOCK_ARM_C above ENTRY (so mid-range peaks 55c->76c->12c get protected),
# or at the old fixed peaks, whichever comes first. LOCK_ARM_C=0 disables the entry-relative arm.
LOCK_PEAK=0.80; LOCK_DROP=0.10; BIG_RUN=float(os.environ.get('LOCK_BIG_RUN','0.25')); BIG_DROP=float(os.environ.get('LOCK_BIG_DROP','0.05')); LOCK_HOLD_P=0.90
LOCK_ARM_C=float(os.environ.get('LOCK_ARM_C','0.10'))
WKND_LOCK_ARM_C=float(os.environ.get('WKND_LOCK_ARM_C','0.15'))  # Fri 5PM-Mon 9AM ET
def lock_arm(): return WKND_LOCK_ARM_C if wknd_night() else LOCK_ARM_C
# lock used to be night-only; replay of 153 peak-recorded trades showed arming entry+10c all day
# improves 64 exits by +$31.18 with no offsetting cost, so it now runs around the clock (LOCK_ALL_DAY=0 reverts)
LOCK_ALL_DAY=os.environ.get('LOCK_ALL_DAY','1')!='0'
def lock_active(ts=None): return LOCK_ALL_DAY or night(ts)
# metals flip harder off their peaks (thin book), so they lock in lower: 65c peak instead of 80c
LOCK_PEAK_BY_ASSET={'GOLD':float(os.environ.get('LOCK_PEAK_GOLD','0.65')),'SILVER':float(os.environ.get('LOCK_PEAK_SILVER','0.65'))}
TP_BID=0.91; TP_HOLD_P=0.97; TP_HOLD_Z=3.0; TP_HOLD_MAX_LEFT=180
# metals only: once the bid is 10c above entry, never give back past entry (thin books gap through the 40% stop)
GUARD_ASSETS={'GOLD','SILVER'}; GUARD_GIVEBACK=float(os.environ.get('GUARD_GIVEBACK','0.0'))
def kalshi_fee(c,p): return math.ceil(7*c*p*(1-p))/100  # Kalshi taker fee: ceil(0.07*C*P*(1-P)) in $
MIN_EDGE=0.03; NIGHT_EDGE=0.05; WKND_EDGE=float(os.environ.get('WKND_NIGHT_EDGE','0.07')); REV=os.environ.get('REVERSAL','1')!='0'; REV_MAX=float(os.environ.get('REVERSAL_MAX_PRICE','0.60')); FADE=os.environ.get('WKND_FADE','0')!='0'; WKND_MAX_EDGE=float(os.environ.get('WKND_NIGHT_MAX_EDGE','0.20')); EXIT_EDGE=0.03; STOP=0.40; KELLY=0.25; CAP=0.08; TICK=int(os.environ.get('TICK_SEC','10')); REST_SEC=int(os.environ.get('LIVE_REST_SEC','30')); LIVE_TICK=int(os.environ.get('LIVE_TICK_SEC','5'))
def get(url,auth=False):
    h={'User-Agent':'paper-desk/1.1','Accept':'application/json'}
    if auth: h['Authorization']='Bearer '+KEY
    return json.load(urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=8))
def iso(s): return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def fnum(m,k):
    try: return float(m.get(k) or 0)
    except Exception: return 0.0
# ---- optional MongoDB persistence (state only; db 'paper_desk' by default, separate from XIII Trades Pro) ----
MURI=os.environ.get('MONGODB_URI',''); MDB=os.environ.get('MONGODB_DB','paper_desk'); MCOL=None; PERSIST='file'; _dirty=threading.Event()
if MURI:
    try:
        from pymongo import MongoClient
        _mc=MongoClient(MURI,serverSelectionTimeoutMS=8000); _mc.admin.command('ping')
        MCOL=_mc[MDB]['paper_desk_state']; PERSIST='mongo'; print('persistence: mongo db',MDB,flush=True)
    except Exception as e: print('mongo unavailable, using file:',e,flush=True)
def _mongo_writer():
    while True:
        _dirty.wait(); time.sleep(2); _dirty.clear()
        try:
            with lock: doc={'balance':round(pt.balance,4),'startBalance':pt.start,'trades':json.loads(json.dumps(pt.trades)),'updatedAt':time.time()}
            MCOL.replace_one({'_id':'state'},doc,upsert=True)
        except Exception as e: print('mongo write err',e,flush=True); _dirty.set(); time.sleep(10)
class PaperTrader:
    def __init__(s):
        if MCOL is not None:
            try:
                doc=MCOL.find_one({'_id':'state'})
                if doc: json.dump({k:doc[k] for k in('balance','startBalance','trades')},open(TF,'w'),indent=1); print('loaded state from mongo:',len(doc['trades']),'trades',flush=True)
                elif os.path.exists(SEED): shutil.copy(SEED,TF); _dirty.set(); print('mongo empty; seeding from seed_trades.json',flush=True)
            except Exception as e: print('mongo load err',e,flush=True)
        # boot MERGES seed + file/mongo state (never replaces): union by (ticker,openedAt), newest/closed record wins
        cur=json.load(open(TF)) if os.path.exists(TF) else {}
        seed=json.load(open(SEED)) if os.path.exists(SEED) else []
        if isinstance(cur,list): cur={'trades':cur}
        if isinstance(seed,dict): seed=seed.get('trades',[])
        s.start=cur.get('startBalance',START); s.trades=[]; s.balance=s.start
        for t in cur.get('trades',[]): t.pop('exiting',None)  # an in-flight live sell can't survive a restart
        n=s.merge(seed+cur.get('trades',[])); print('boot merge:',n,'trades, balance',round(s.balance,2),flush=True)
    @staticmethod
    def _key(t): return (t.get('ticker'),round(float(t.get('openedAt') or 0),3))
    def merge(s,incoming):
        m={s._key(t):t for t in s.trades}
        for t in incoming:
            k=s._key(t); o=m.get(k)
            if o is None or (o.get('status')=='open' and t.get('status')!='open') or (o.get('status')==t.get('status') and (t.get('closedAt') or 0)>(o.get('closedAt') or 0)): m[k]=t
        tr=sorted(m.values(),key=lambda t:(t.get('openedAt') or 0))
        for i,t in enumerate(tr,1): t['id']=i
        s.trades=tr
        pp=[t for t in tr if t.get('mode')!='live']  # paper balance comes from paper trades only
        s.balance=s.start+sum(t.get('pnl') or 0 for t in pp if t.get('status')!='open')-sum(t.get('stake') or 0 for t in pp if t.get('status')=='open')
        s.save(); return len(tr)
    def save(s):
        tmp=TF+'.tmp'; json.dump({'balance':round(s.balance,4),'startBalance':s.start,'trades':s.trades},open(tmp,'w'),indent=1); os.replace(tmp,TF)
        if MCOL is not None: _dirty.set()
    @staticmethod
    def size(side,ask,p_yes):
        # quarter Kelly fraction (cap, night halving); shared by paper and live
        q=p_yes if side=='YES' else 1-p_yes; edge=q-ask
        nt=night()
        if edge<need_edge() or not (0<ask<1): return None
        if WKND_MAX_EDGE>0 and wknd_night() and edge>WKND_MAX_EDGE: return None
        return min(KELLY*edge/(1-ask),CAP)*(0.5 if nt else 1),edge,q
    def openLive(s,asset,side,fill,strike,close,ticker,meta,edge,q):
        stake=round(fill['cost']+fill['fee'],4)
        t=dict(id=max([x.get('id',0) for x in s.trades]+[0])+1,mode='live',asset=asset,side=side,price=fill['price'],stake=stake,stakePct=round(100*fill['frac'],2),
               contracts=fill['contracts'],entryFee=fill['fee'],liveOrderId=fill['orderId'],liveCashAtEntry=fill['cash'],
               edge=round(edge,3),modelP=round(q,4),strike=strike,ticker=ticker,openedAt=time.time(),closesAt=close,status='open',**meta)
        t['perpPremium']=perp_premium(asset); t['regime']=regime(asset)
        s.trades.append(t); s.save(); print('OPEN LIVE',t['id'],ticker,side,fill['price'],fill['contracts'],flush=True); return t
    def openTrade(s,asset,side,ask,p_yes,strike,close,ticker,meta):
        sz=s.size(side,ask,p_yes)
        if not sz: return None
        f,edge,q=sz; bal=s.balance; stake=round(bal*f,2)
        if stake<0.05: return None
        s.balance-=stake
        t=dict(id=max([x.get('id',0) for x in s.trades]+[0])+1,asset=asset,side=side,price=round(ask,4),stake=stake,stakePct=round(100*stake/bal,2),contracts=round(stake/ask,3),
               edge=round(edge,3),modelP=round(q,4),strike=strike,ticker=ticker,openedAt=time.time(),closesAt=close,status='open',**meta)
        t['perpPremium']=perp_premium(asset); t['regime']=regime(asset)
        s.trades.append(t); s.save(); print('OPEN',t['id'],ticker,side,ask,stake,flush=True)
        return t
    def close(s,t,win,how,val=None):
        payout=t['contracts'] if win else 0
        if t.get('mode')=='live':
            pnl=round(payout-t['stake'],2); t.update(status='won' if win else 'lost',settle=val,settledBy=how,pnl=round(pnl+(t.get('partialPnl') or 0),2),closedAt=time.time())
            if LV: LV.book(pnl); LV.log(action='settle',ticker=t['ticker'],side=t['side'],count=t['contracts'],result=t['status'],pnl=pnl)
        else:
            t.update(status='won' if win else 'lost',settle=val,settledBy=how,pnl=round(payout-t['stake'],2),closedAt=time.time()); s.balance+=payout
        for k in('markPrice','unrealizedPnl','flipWarning','signalsAgree','whaleAgainst'): t.pop(k,None)
    def exit(s,t,bid,reason,fee=0.0):
        if t.get('mode')=='live': return s.exitLive(t,bid,reason)
        proceeds=t['contracts']*bid-fee
        t.update(status='exited',exitPrice=round(bid,4),exitReason=reason,exitFee=round(fee,2),settledBy='early-exit@bid',pnl=round(proceeds-t['stake'],2),closedAt=time.time())
        for k in('markPrice','unrealizedPnl','flipWarning','signalsAgree','whaleAgainst'): t.pop(k,None)
        s.balance+=proceeds; s.save(); print('EXIT',t['id'],reason,bid,flush=True)
    def exitLive(s,t,bid,reason):
        # real sell runs off the lock; trade stays 'open' (flagged exiting) until the fill is known
        if not (LV and LV.allowed()): t['liveSellError']='live disabled (env/keys); holding to settlement'; return
        t['exiting']=reason; threading.Thread(target=s._sellLive,args=(t,bid,reason),daemon=True).start()
    def _sellLive(s,t,bid,reason):
        floor=round(t['price']+0.01,2) if str(reason).lower().startswith(('profitlock','take','tp','breakeven')) is True and not str(reason).lower().startswith('breakeven') else None  # profit exits never sell below entry
        try: fc,fpx,fee,oid,err=LV.sell(t['ticker'],t['side'],t['contracts'],bid,floor=floor)
        except Exception as e: fc,fpx,fee,oid,err=0,None,0.0,None,f'{type(e).__name__}: {e}'
        with lock:
            t.pop('exiting',None)
            if fc<=0: t.update(liveSellError=err or 'no fill',liveSellAt=time.time()); s.save(); return
            share=min(1.0,fc/t['contracts']); realized=round(fc*fpx-fee-t['stake']*share,2)
            if share>=0.999:
                t.update(status='exited',exitPrice=fpx,exitReason=reason,exitFee=round(fee,4),liveSellOrderId=oid,settledBy='live-sell',pnl=round(realized+(t.get('partialPnl') or 0),2),closedAt=time.time())
                for k in('markPrice','unrealizedPnl','flipWarning','signalsAgree','whaleAgainst','liveSellError'): t.pop(k,None)
                print('EXIT LIVE',t['id'],reason,fpx,flush=True)
            else:
                t.update(contracts=round(t['contracts']-fc,2),stake=round(t['stake']*(1-share),4),partialPnl=round((t.get('partialPnl') or 0)+realized,2),liveSellError=f'partial {fc} filled; rest open')
            s.save()
        LV.book(realized)
    def settle(s,spot):
        now=time.time(); ch=False
        for t in s.trades:
            if t['status']!='open' or now<t['closesAt'] or t.get('exiting'): continue
            if t.get('ticker'):
                try:
                    m=get(f"{KALSHI}/markets/{t['ticker']}")['market']
                    if m.get('result') in('yes','no'):
                        s.close(t,(m['result']=='yes')==(t['side']=='YES'),'kalshi',m.get('expiration_value')); ch=True
                        continue
                except Exception as e: print('kalshi settle err',e,flush=True)
                if now<t['closesAt']+600: continue
            if t['asset'] in spot:
                s.close(t,(spot[t['asset']]>=t['strike'])==(t['side']=='YES'),'coinbase-spot',spot[t['asset']]); ch=True
            # counterfactual for early exits: what holding to settlement would have paid
        for t in s.trades:
            if t['status']=='exited' and t.get('ticker') and 'wouldHaveWon' not in t and now>=t['closesAt']:
                try:
                    m=get(f"{KALSHI}/markets/{t['ticker']}")['market']
                    if m.get('result') in('yes','no'):
                        win=(m['result']=='yes')==(t['side']=='YES'); hold=round((t['contracts'] if win else 0)-t['stake'],2)
                        t.update(finalResult=m['result'],wouldHaveWon=win,counterfactualPnl=hold,exitSavedPnl=round(t['pnl']-hold,2)); ch=True
                except Exception as e: print('cf err',e,flush=True)
        if ch: s.save()
    def exitSummary(s):
        ex=[t for t in s.trades if t['status']=='exited']; done=[t for t in ex if 'wouldHaveWon' in t]
        def agg(ts):
            return dict(count=len(ts),resolved=len([t for t in ts if 'wouldHaveWon' in t]),wouldHaveWon=len([t for t in ts if t.get('wouldHaveWon')]),
                exitPnl=round(sum(t['pnl'] for t in ts),2),holdPnl=round(sum(t.get('counterfactualPnl',0) for t in ts if 'wouldHaveWon' in t),2),
                netSaved=round(sum(t.get('exitSavedPnl',0) for t in ts),2),
                savedOnLosers=round(sum(t['exitSavedPnl'] for t in ts if 'wouldHaveWon' in t and not t['wouldHaveWon']),2),
                lostOnWinners=round(-sum(t['exitSavedPnl'] for t in ts if t.get('wouldHaveWon')),2))
        by={}
        for t in ex: by.setdefault((t.get('exitReason') or 'other').split(':')[0],[]).append(t)
        hd=[t for t in s.trades if t.get('tpHeld') and t['status'] in('won','lost')]
        held=dict(count=len([t for t in s.trades if t.get('tpHeld')]),resolved=len(hd),won=len([t for t in hd if t['status']=='won']),
            lost=len([t for t in hd if t['status']=='lost']),holdPnl=round(sum(t['pnl'] for t in hd),2),
            tpPnlIfSold=round(sum(t.get('tpSellPnl',0) for t in hd),2),holdGain=round(sum(t['pnl']-t.get('tpSellPnl',0) for t in hd),2))
        return dict(all=agg(ex),byReason={k:agg(v) for k,v in by.items()},pending=len(ex)-len(done),takeProfitHolds=held)
    def getStats(s):
        def st(ts):
            c=[t for t in ts if t['status']!='open']; w=[t for t in c if t.get('pnl',0)>0]; o=[t for t in ts if t['status']=='open']
            return dict(trades=len(ts),closed=len(c),open=len(o),wins=len(w),winRate=round(len(w)/len(c),3) if c else 0,pnl=round(sum(t['pnl'] for t in c),2),
                expectancy=round(sum(t['pnl'] for t in c)/len(c),3) if c else 0,unrealizedPnl=round(sum(t.get('unrealizedPnl',0) for t in o),2))
        pp=[t for t in s.trades if t.get('mode')!='live']; lt=[t for t in s.trades if t.get('mode')=='live']
        P=st(pp); Lv=st(lt); A=st(s.trades); o=[t for t in pp if t['status']=='open']
        return dict(mode='paper',balance=round(s.balance,2),paperBalance=round(s.balance,2),startBalance=s.start,
            equity=round(s.balance+sum(t.get('markPrice',t['price'])*t['contracts'] for t in o),2),
            unrealizedPnl=P['unrealizedPnl'],winRate=P['winRate'],expectancy=P['expectancy'],paperStats=P,liveStats=Lv,
            closed=A['closed'],open=A['open'],earlyExits=s.exitSummary(),trades=s.trades[::-1])
LV=None
lock=threading.Lock(); pt=PaperTrader()
# ---- live mode: separate small state doc ('live' in mongo / live.json), never mixed with paper state ----
LF=os.path.join(DATA,'live.json')
def _live_load():
    if MCOL is not None:
        try: return MCOL.find_one({'_id':'live'})
        except Exception as e: print('live load err',e,flush=True)
    try: return json.load(open(LF))
    except Exception: return None
def _live_save(st):
    json.dump(st,open(LF,'w'))
    if MCOL is not None: MCOL.replace_one({'_id':'live'},dict(st,_id='live'),upsert=True)
try:
    import live as _live; LV=_live.Live(_live_load,_live_save)
    print('live mode:', 'LIVE' if LV.enabled() else ('paused' if LV.allowed() else 'off'),flush=True)
except Exception as e: LV=None; LIVE_INIT_ERR=f'{type(e).__name__}: {e}'; print('live init err',LIVE_INIT_ERR,flush=True)
if MCOL is not None: threading.Thread(target=_mongo_writer,daemon=True).start()
status={'eloedge':'unknown','sources':{},'signals':{},'commodities':{},'lastTick':None,'fallbackInUse':None,
 'rules':f'Entry: 15m momentum & taker CVD agree AND EloEdge model_p agrees (Coinbase-only fallback if EloEdge unavailable); edge>={MIN_EDGE:.0%}; quarter Kelly, cap {CAP:.0%}. Whale: skip entries against strong net whale flow; exit at bid on strong whale flow against (not if <90s left & mark>=0.85, nor if bid < model-10%). Take profit: sell at bid minus Kalshi fee once bid>={TP_BID:.0%}, unless EloEdge>={TP_HOLD_P:.0%} AND spot>={TP_HOLD_Z} sigma past strike AND <={TP_HOLD_MAX_LEFT}s left. Profit lock (all day): armed {LOCK_ARM_C*100:.0f}c above entry (or at the fixed peak), sells on a {LOCK_DROP*100:.0f}c giveback while still above entry. Exit early at bid if signals+EloEdge flip with exit edge>={EXIT_EDGE:.0%}, or stop if mark<={STOP:.0%} of entry.'}
def coinbase(a):
    c=get(f'{CB}/products/{a}-USD/candles?granularity=900')
    spot=c[0][4]; mom=(c[0][4]-c[1][3])/c[1][3]
    tr=get(f'{CB}/products/{a}-USD/trades?limit=1000')
    cvd=sum(float(x['size'])*(1 if x['side']=='sell' else -1) for x in tr); vol=sum(float(x['size']) for x in tr)  # side=maker side
    # whale detection: taker trades far above recent distribution within last WHALE_WIN sec
    try:
        import statistics; rs=[math.log(c[i][4]/c[i+1][4]) for i in range(min(len(c)-1,96))]; sd15=statistics.pstdev(rs)
    except Exception: sd15=None
    usd=sorted(float(x['size'])*float(x['price']) for x in tr); p99=usd[int(0.99*(len(usd)-1))] if usd else 0
    ts_all=[iso(x['time']) for x in tr]; span_min=max((max(ts_all)-min(ts_all))/60,1/60) if ts_all else 1
    usd_per_min=sum(usd)/span_min
    # adaptive: quiet market -> floor, busy -> cap
    wc=WHALE_CFG[a]; thr=min(wc['cap'],max(wc['floor'],wc['p99mult']*p99,wc['volfrac']*usd_per_min)); strong=thr; now=time.time(); wh=[]
    for x in tr:
        u=float(x['size'])*float(x['price'])
        if u>=thr:
            ts=iso(x['time'])
            if now-ts<=WHALE_WIN: wh.append(dict(side='buy' if x['side']=='sell' else 'sell',usd=round(u),price=float(x['price']),ago=round(now-ts)))
    net=sum(w['usd'] if w['side']=='buy' else -w['usd'] for w in wh)
    wdir='YES' if net>=strong else 'NO' if net<=-strong else None
    return dict(sd15=sd15,spot=spot,momentum=round(mom,5),cvd=round(cvd,4),cvdRatio=round(cvd/vol,3) if vol else 0,
        whale=dict(thresholdUsd=round(thr),strongUsd=round(strong),usdPerMin=round(usd_per_min),floorUsd=wc['floor'],capUsd=wc['cap'],p99Usd=round(p99),count=len(wh),netFlowUsd=round(net),strongDir=wdir,alerts=wh[:10]))
def metal(a):
    sym=METALS[a]; c=get(f"{KF}/api/charts/v1/mark/{sym}/15m?from={int(time.time())-86400*2}")['candles'][::-1]  # newest first
    cl=[float(x['close']) for x in c]; spot=float(get(f"{KF}/derivatives/api/v3/tickers?symbol={sym}")['tickers'][0]['markPrice'])
    mom=(spot-float(c[1]['open']))/float(c[1]['open'])
    try:
        import statistics; sd15=statistics.pstdev([math.log(cl[i]/cl[i+1]) for i in range(min(len(cl)-1,96))])
    except Exception: sd15=None
    # tick-rule CVD over Kraken futures fills in the last 30 min (no Coinbase metals tape; whale filter not applied)
    h=[x for x in get(f"{KF}/derivatives/api/v3/history?symbol={sym}")['history'] if time.time()-iso(x['time'])<=1800][::-1]
    cvd=0.0; vol=0.0; last=None; sg=0
    for x in h:
        p=float(x['price']); sz=float(x['size'])
        if last is not None and p!=last: sg=1 if p>last else -1
        cvd+=sg*sz; vol+=sz; last=p
    return dict(sd15=sd15,spot=spot,momentum=round(mom,5),cvd=round(cvd,4),cvdRatio=round(cvd/vol,3) if vol else 0,
        whale=dict(thresholdUsd=None,count=0,netFlowUsd=0,strongDir=None,alerts=[],note='n/a for metals'))
def mk(m): return dict(ticker=m['ticker'],strike=m.get('floor_strike'),close=iso(m['close_time']),yesAsk=fnum(m,'yes_ask_dollars'),yesBid=fnum(m,'yes_bid_dollars'),noAsk=fnum(m,'no_ask_dollars'),noBid=fnum(m,'no_bid_dollars'),result=m.get('result') or '')
def kalshi(a):
    ms=[m for m in get(f"{KALSHI}/markets?series_ticker={SERIES[a]}&status=open&limit=5")['markets'] if iso(m['close_time'])>time.time()]
    return mk(sorted(ms,key=lambda m:m['close_time'])[0]) if ms else None
def eloedge_all():
    # ONE unscoped call per tick covers BTC/ETH/SOL (same interval as before; was one call per asset)
    if not KEY: return 'unavailable',{}
    d=get(ELO,auth=True); w={x['market']['ticker']:x for x in d.get('windows',[])}
    # commodities (GOLD/SILVER) are a separate EloEdge feed; one batched call cached ELO_COM_TTL s
    if metals_closed(): elo_com['wins']={}; elo_com['err']='skipped: metals session closed'
    elif time.time()-elo_com['at']>=ELO_COM_TTL:
        elo_com['at']=time.time()
        try: elo_com['wins']={x['market']['ticker']:x for x in get(ELO_COM,auth=True).get('windows',[])}; elo_com['err']=None
        except Exception as e: elo_com['err']=str(e); print('eloedge commodity err',e,flush=True)
    w.update(elo_com['wins']); return 'live',w
elo_com=dict(at=0,wins={},err=None)
def night(ts=None):
    h=datetime.fromtimestamp(ts or time.time(),ET).hour
    return h>=17 or h<9
WKND_MAX=float(os.environ.get('WKND_NIGHT_MAX_PRICE','0.69'))  # 0 disables
def wknd_night(ts=None):
    # Fri 5 PM ET -> Mon 9 AM ET (thin weekend volume)
    d=datetime.fromtimestamp(ts or time.time(),ET); wd,h=d.weekday(),d.hour
    return (wd==4 and h>=17) or wd in (5,6) or (wd==0 and h<9)
ALT=os.environ.get('ALT_CONSENSUS','1')!='0'; ALT_MAX=float(os.environ.get('ALT_MAX_PRICE','0.60')); _altc={}
def alt_votes(btc_ticker):
    # SOL/DOGE/XRP lean for the same 15m window (YES mid > 0.5); cached 20s. User live test Nov 7.
    suf=btc_ticker.split('-',1)[1]; c=_altc.get(suf)
    if c and time.time()-c[0]<20: return c[1]
    v={}
    for a in ('SOL','DOGE','XRP'):
        try:
            m=json.load(urllib.request.urlopen(f'https://api.elections.kalshi.com/trade-api/v2/markets/KX{a}15M-{suf}',timeout=5))['market']
            b,k=float(m.get('yes_bid_dollars') or 0),float(m.get('yes_ask_dollars') or 0)
            if b>0 and k>0: v[a]='YES' if (b+k)/2>0.5 else 'NO'
        except Exception: pass
    _altc[suf]=(time.time(),v); return v
_perp={}
def perp_premium(asset):
    # log-only (Nov 7): Kraken perp last vs spot index, as a fraction; cached 15s
    sym={'BTC':'PF_XBTUSD','ETH':'PF_ETHUSD','SOL':'PF_SOLUSD'}.get(asset)
    if not sym: return None
    c=_perp.get('t')
    if not c or time.time()-c[0]>15:
        try:
            d=json.load(urllib.request.urlopen('https://futures.kraken.com/derivatives/api/v3/tickers',timeout=4))
            c=(time.time(),{x['symbol']:x for x in d.get('tickers',[])}); _perp['t']=c
        except Exception: return None
    x=c[1].get(sym) or {}
    try: return round((float(x['last'])-float(x['indexPrice']))/float(x['indexPrice']),6)
    except Exception: return None
_reg={}
def regime(asset):
    # log-only (Nov 7): last 30 one-minute Coinbase candles -> trend_up/trend_down/choppy/quiet; cached 60s
    if asset not in ('BTC','ETH','SOL'): return None
    c=_reg.get(asset)
    if c and time.time()-c[0]<60: return c[1]
    r=None
    try:
        req=urllib.request.Request(f'https://api.exchange.coinbase.com/products/{asset}-USD/candles?granularity=60',headers={'User-Agent':'paper-desk'})
        k=sorted(json.load(urllib.request.urlopen(req,timeout=5))[:30])  # [t,low,high,open,close,vol]
        cl=[x[4] for x in k]; hi=max(x[2] for x in k); lo=min(x[1] for x in k)
        mv=(cl[-1]-k[0][3])/k[0][3]; rng=(hi-lo)/cl[-1]; path=sum(abs(cl[i]-cl[i-1]) for i in range(1,len(cl))) or 1e-9
        eff=abs(cl[-1]-k[0][3])/path
        r=dict(tag='quiet' if rng<0.001 else (('trend_up' if mv>0 else 'trend_down') if eff>=0.4 and abs(mv)>=0.0015 else 'choppy'),move=round(mv,5),range=round(rng,5),eff=round(eff,3))
    except Exception: r=c[1] if c else None
    _reg[asset]=(time.time(),r); return r
PAIR_TP=float(os.environ.get('PAIR_CASH_C','0.25'))
TREND_N=int(os.environ.get('TREND_N','3')); _trend={}
def trend_dir(series):
    # direction of the last TREND_N settled 15m windows if they all agree ('YES'/'NO'), else None; cached 60s
    c=_trend.get(series)
    if c and time.time()-c[0]<60: return c[1]
    d=None
    try:
        ms=json.load(urllib.request.urlopen(f'https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={series}&status=settled&limit={TREND_N}',timeout=6))['markets']
        rs=[m.get('result') for m in ms]
        if len(rs)==TREND_N and len(set(rs))==1 and rs[0] in ('yes','no'): d=rs[0].upper()
    except Exception: d=c[1] if c else None
    _trend[series]=(time.time(),d); return d
def need_edge(): return WKND_EDGE if wknd_night() else (NIGHT_EDGE if night() else MIN_EDGE)
def metals_closed(ts=None):
    # Kalshi GOLD/SILVER closed: daily 17:00-18:00 ET, and Fri 17:00 ET -> Sun 18:00 ET (DST-aware)
    d=datetime.fromtimestamp(ts if ts is not None else time.time(),ET); wd,h=d.weekday(),d.hour
    if h==17: return True
    if wd==4 and h>=17: return True
    if wd==5: return True
    if wd==6 and h<18: return True
    return False
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
    try: elo_est,elo_wins=eloedge_all()
    except Exception as e: elo_est,elo_wins='unavailable',{}; print('eloedge err',e,flush=True)
    for a in SERIES:
        sg={}
        try: cb=metal(a) if a in METALS else coinbase(a); sg.update(cb); spot[a]=cb['spot']; src['kraken-futures' if a in METALS else 'coinbase']='live'
        except Exception as e: src['kraken-futures' if a in METALS else 'coinbase']=f'error: {e}'; sigs[a]=dict(action='skip: coinbase down'); continue
        try:
            k=kalshi(a); src['kalshi']='live' if k else 'no open market'; sg['kalshi']=k
            if k and LV and LV.enabled(): LV.wsq(k['ticker'])
        except Exception as e: k=None; src['kalshi']=f'error: {e}'
        est,wins=elo_est,elo_wins
        src['eloedge']=est
        mom,cvd=cb['momentum'],cb['cvd']
        dirn='YES' if mom>0 and cvd>0 else 'NO' if mom<0 and cvd<0 else None
        sg['direction']=dirn or 'disagree'
        def fresh(tk):
            w=wins.get(tk)
            return w['model_p'] if w and w.get('model_p') is not None and (w.get('age_sec') or 99)<60 and w.get('state')=='open' else None
        # ---- monitor open positions for this asset ----
        for t in [t for t in pt.trades if t['status']=='open' and t['asset']==a and t.get('ticker') and time.time()<t['closesAt'] and not t.get('exiting')]:
            w=LV.wsq(t['ticker']) if (LV and t.get('mode')=='live') else None
            if w and w.get('yesBid') is not None and w.get('yesAsk') is not None: m=dict(yesBid=w['yesBid'],noBid=round(1-w['yesAsk'],4))  # live: pushed WS quote
            else:
              try: m=k if (k and k['ticker']==t['ticker'] and t.get('mode')!='live') else mk(get(f"{KALSHI}/markets/{t['ticker']}")['market'])  # live: always the fresh single-market quote
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
                t['peakBid']=round(max(t.get('peakBid') or 0,bid),4)
                tp=None
                if bid>=TP_BID:
                    sd=cb.get('sd15'); strike=t.get('strike'); z=None
                    if sd and strike and left_t>0:
                        z=(cb['spot']-strike)/(cb['spot']*sd*math.sqrt(max(left_t,1)/900))*(1 if t['side']=='YES' else -1)
                    hold=mp is not None and q is not None and q>=TP_HOLD_P and z is not None and z>=TP_HOLD_Z and left_t<=TP_HOLD_MAX_LEFT
                    zs=f'{z:.1f}' if z is not None else 'n/a'; qs=f'{q:.3f}' if q is not None else 'n/a'
                    why=f'bid {bid:.3f}>={TP_BID}; EloEdge p={qs} (need>={TP_HOLD_P}); dist={zs} sigma (need>={TP_HOLD_Z}); {left_t:.0f}s left (need<={TP_HOLD_MAX_LEFT})'
                    fee=kalshi_fee(t['contracts'],bid); sellpnl=round(t['contracts']*bid-fee-t['stake'],2)
                    if hold:
                        if not t.get('tpHeld'): print('TP-HOLD',t['id'],why,flush=True)
                        t.update(tpHeld=True,tpHoldReason=why,tpSellPnl=sellpnl if 'tpSellPnl' not in t else t['tpSellPnl'])
                    else: tp=(fee,why)
                if tp: pt.exit(t,bid,f'takeprofit: {tp[1]}; fee ${tp[0]:.2f}',fee=tp[0])
                elif PAIR_TP>0 and bid>=t['price']+PAIR_TP and any(o is not t and o['status']=='open' and not o.get('exiting') and o.get('closesAt')==t.get('closesAt') and o['side']==t['side'] for o in pt.trades):
                    fp=kalshi_fee(t['contracts'],bid); pt.exit(t,bid,f'paircash: up {bid-t["price"]:+.2f} with a same-side partner open; cash this one, partner rides the lock; fee ${fp:.2f}',fee=fp)
                elif lock_active() and t['peakBid']>=min(LOCK_PEAK_BY_ASSET.get(t['asset'],LOCK_PEAK), t['price']+lock_arm()) and bid<=t['peakBid']-(BIG_DROP if t['peakBid']-t['price']>=BIG_RUN else LOCK_DROP) and bid>t['price'] and not (q is not None and q>=LOCK_HOLD_P):
                    f2=kalshi_fee(t['contracts'],bid); _lp=min(LOCK_PEAK_BY_ASSET.get(t['asset'],LOCK_PEAK),t['price']+lock_arm()); pt.exit(t,bid,f'profitlock: {"night" if night() else "day"}, peak {t["peakBid"]:.3f} (armed at {_lp:.2f}, entry {t["price"]:.2f}) -> bid {bid:.3f}; EloEdge p={q if q is None else round(q,3)}; fee ${f2:.2f}',fee=f2)
                elif t['asset'] in GUARD_ASSETS and t['peakBid']>=t['price']+0.10 and bid>0 and bid<=t['price']-GUARD_GIVEBACK and not (q is not None and q>=LOCK_HOLD_P):
                    f3=kalshi_fee(t['contracts'],bid); pt.exit(t,bid,f'breakeven: {t["asset"]} peaked {t["peakBid"]:.3f} (>=entry+0.10), fell to bid {bid:.3f} vs entry {t["price"]:.3f}; EloEdge p={q if q is None else round(q,3)}; fee ${f3:.2f}',fee=f3)
                elif bid>0 and bid<=STOP*t['price']: pt.exit(t,bid,f'stop: mark {bid:.3f} <= {STOP:.0%} of entry {t["price"]}')
                elif not t.get('fade') and whale_against and bid>0 and not (left_t<90 and bid>=0.85) and (q is None or bid>=q-WHALE_MAX_GIVEUP):
                    pt.exit(t,bid,f'whale: net whale flow ${wnet:,.0f} against {t["side"]}, sold at bid {bid:.3f}')
                elif not t.get('fade') and sig_against and elo_against and q is not None and bid-q>=EXIT_EDGE:
                    pt.exit(t,bid,f'flip: momentum/CVD {dirn} + {"EloEdge" if est=="live" else "fallback"} p={q:.3f} vs bid {bid:.3f}')
        if not k: sg['action']='skip: no real Kalshi market'; sigs[a]=sg; continue
        if a=='SOL' and night(): sg['action']='skip: SOL paused at night (5 PM-9 AM ET, thin volume)'; sigs[a]=sg; continue
        if a in METALS and (metals_closed() or metals_closed(k['close']-1)): sg['action']='skip: metals session closed (Kalshi 5-6 PM ET daily, Fri 5 PM-Sun 6 PM ET)'; sigs[a]=sg; continue
        mp=fresh(k['ticker']); sg['modelP']=mp
        left=k['close']-time.time(); sg['secsToClose']=round(left)
        if REV and 600<=left<=840 and not any(t.get('ticker')==k['ticker'] for t in pt.trades):
            # reversal bet (user test, Nov 7): after 3+ same-way settles, buy the other side early in the window; backtest 59% wins
            td=trend_dir(k['ticker'].split('-')[0])
            if td:
                rd='NO' if td=='YES' else 'YES'; rask=k['yesAsk'] if rd=='YES' else k['noAsk']
                if 0<rask<=REV_MAX:
                    fq=min(rask+need_edge()+0.001,0.99); rmp=fq if rd=='YES' else 1-fq
                    rmeta=dict(priceSource='kalshi',signalSource='reversal-after-3',fade=True,reversal=True,streak=td)
                    t=None
                    if LV and LV.enabled():
                        if k['ticker'] not in LV.tried and k['ticker'] not in LV.resting:
                            sz=pt.size(rd,rask,rmp)
                            if sz:
                                _rs=lambda px,d=rd:None if px>REV_MAX else (pt.size(d,px,(lambda f:f if d=='YES' else 1-f)(min(px+need_edge()+0.001,0.99))) or [None])[0]  # keep min-edge size at the live price
                                fill=LV.buy(a,k['ticker'],rd,sz[0],rask,sizer=_rs)
                                if fill:
                                    with lock: t=pt.openLive(a,rd,fill,k['strike'],k['close'],k['ticker'],rmeta,sz[1],sz[2])
                    else:
                        with lock: t=pt.openTrade(a,rd,rask,rmp,k['strike'],k['close'],k['ticker'],rmeta)
                    if t: sg['action']=f'OPENED REVERSAL #{t["id"]} {rd} after 3x {td}'; sigs[a]=sg; continue
        if ALT and a=='BTC' and 540<=left<=780 and not any(t.get('ticker')==k['ticker'] for t in pt.trades):
            av=alt_votes(k['ticker'])
            if len(av)==3 and len(set(av.values()))==1:
                rd=list(av.values())[0]; rask=k['yesAsk'] if rd=='YES' else k['noAsk']
                if 0<rask<=ALT_MAX:
                    fq=min(rask+need_edge()+0.001,0.99); rmp=fq if rd=='YES' else 1-fq
                    rmeta=dict(priceSource='kalshi',signalSource='alt-consensus',fade=True,altConsensus=True,altVotes=av)
                    t=None
                    if LV and LV.enabled():
                        if k['ticker'] not in LV.tried and k['ticker'] not in LV.resting:
                            sz=pt.size(rd,rask,rmp)
                            if sz:
                                _as=lambda px,d=rd:None if px>ALT_MAX else (pt.size(d,px,(lambda f:f if d=='YES' else 1-f)(min(px+need_edge()+0.001,0.99))) or [None])[0]  # keep min-edge size at the live price
                                fill=LV.buy(a,k['ticker'],rd,sz[0],rask,sizer=_as)
                                if fill:
                                    with lock: t=pt.openLive(a,rd,fill,k['strike'],k['close'],k['ticker'],rmeta,sz[1],sz[2])
                    else:
                        with lock: t=pt.openTrade(a,rd,rask,rmp,k['strike'],k['close'],k['ticker'],rmeta)
                    if t: sg['action']=f'OPENED ALT-CONSENSUS #{t["id"]} {rd} (SOL/DOGE/XRP all {rd})'; sigs[a]=sg; continue
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
            meta=dict(priceSource='kalshi',signalSource=used,whaleFlowAtEntry=cb['whale']['netFlowUsd'],mom=mom,cvd=cvd,eloedge=est)
            q=mp if dirn=='YES' else 1-mp
            if FADE and WKND_MAX_EDGE>0 and wknd_night() and q-ask>WKND_MAX_EDGE:
                # fade test: weekend-night 20c+ edges went 0/4, so buy the other side, sized as a minimum-edge bet
                oe=round(q-ask,3); dirn='NO' if dirn=='YES' else 'YES'; ask=k['yesAsk'] if dirn=='YES' else k['noAsk']
                fq=min(ask+WKND_EDGE+0.001,0.99); mp=fq if dirn=='YES' else 1-fq; q=fq
                meta=dict(meta,fade=True,fadeOrigEdge=oe)
            td=trend_dir(k['ticker'].split('-')[0]) if TREND_N>0 else None
            if td and td!=dirn: meta=dict(meta,againstTrend=td)  # log-only: data showed 3-in-a-row reverses ~59%
            if any(t.get('status')=='open' and t.get('asset')!=a and t.get('closesAt')==k['close'] and t.get('side')!=dirn for t in pt.trades):
                sg['action']='skip: opposite side of another coin open this window (BTC/ETH settle together ~84%)'; sigs[a]=sg; continue
            if WKND_MAX>0 and wknd_night() and ask>WKND_MAX: sg['action']=f'skip: weekend-night price cap ({ask:.2f} > {WKND_MAX:.2f})'; sigs[a]=sg; continue
            if LV and LV.enabled():
                sz=pt.size(dirn,ask,mp); t=None
                if sz:
                    if k['ticker'] in LV.tried or k['ticker'] in LV.resting: sg['action']='live: order working / already tried this window'; sigs[a]=sg; continue
                    _sz=lambda px,d=dirn,m=mp:None if (WKND_MAX>0 and wknd_night() and px>WKND_MAX) else (pt.size(d,px,m) or [None])[0]
                    LV.lastBuy=None
                    fill=LV.buy(a,k['ticker'],dirn,sz[0],ask,sizer=_sz)  # real order; edge re-checked at the live ask
                    if fill:
                        with lock: t=pt.openLive(a,dirn,fill,k['strike'],k['close'],k['ticker'],meta,sz[1],sz[2])
                    elif LV.lastBuy in ('edge','nofill') and REST_SEC>0 and left>REST_SEC+60:
                        # missed at the live price: rest a limit at the signal price for REST_SEC, like XIII did
                        def _rest(a=a,tk=k['ticker'],d=dirn,ref=ask,szf=_sz,st=k['strike'],cl=k['close'],meta=meta,e=sz[1],q2=sz[2]):
                            f=LV.rest_buy(a,tk,d,ref,szf,wait=REST_SEC,close_at=cl-30)
                            if f:
                                with lock: pt.openLive(a,d,f,st,cl,tk,meta,e,q2)
                        threading.Thread(target=_rest,daemon=True).start(); sg['action']=f'live: resting limit @{ask:.2f} for {REST_SEC}s'; sigs[a]=sg; continue
                sg['action']=f'OPENED LIVE #{t["id"]}' if t else ('live: no fill / edge gone at live price (see live order log)' if sz else (f'skip: weekend-night edge too big ({q-ask:+.3f} > {WKND_MAX_EDGE})' if WKND_MAX_EDGE>0 and wknd_night() and q-ask>WKND_MAX_EDGE else f'skip: edge {q-ask:+.3f} < {need_edge()}'))
            else:
                with lock: t=pt.openTrade(a,dirn,ask,mp,k['strike'],k['close'],k['ticker'],meta)
                sg['action']=f'OPENED #{t["id"]}' if t else (f'skip: weekend-night edge too big ({q-ask:+.3f} > {WKND_MAX_EDGE})' if WKND_MAX_EDGE>0 and wknd_night() and q-ask>WKND_MAX_EDGE else f'skip: edge {q-ask:+.3f} < {need_edge()}')
        sigs[a]=sg
    with lock: pt.settle(spot)
    status['whaleAlerts']=sorted([dict(asset=a,**w) for a,g in sigs.items() for w in (g.get('whale') or {}).get('alerts',[])],key=lambda w:w['ago'])[:12]
    fb=src.get('eloedge')!='live'
    status.update(metalsClosed=metals_closed(),nightMode=night(),sources=src,signals=sigs,lastTick=time.time(),eloedge=src.get('eloedge','unknown'),fallbackInUse=fb,
        signalMode='FALLBACK: Coinbase momentum+CVD only (EloEdge unavailable)' if fb else 'Coinbase momentum+CVD + EloEdge model_p')
def loop():
    while True:
        try: tick_once()
        except Exception as e: print('tick err',e,flush=True)
        time.sleep(LIVE_TICK if any(t.get('mode')=='live' and t['status']=='open' for t in pt.trades) else TICK)  # faster checks while a real position is open
class H(SimpleHTTPRequestHandler):
    def __init__(s,*a,**k): super().__init__(*a,directory=APP,**k)
    def log_message(s,*a): pass
    def do_GET(s):
        p=s.path.split('?')[0]
        if p=='/api/status':
            try: lv=LV.status() if LV else dict(enabled=False,keysPresent=False,lastError=globals().get('LIVE_INIT_ERR','live module unavailable'))
            except Exception as e: lv=dict(enabled=False,error=str(e))
            with lock:
                d=pt.getStats(); d['signalStatus']=status; d['eloedge']=status['eloedge']; d['serverTime']=time.time(); d['persistence']=PERSIST; d['live']=lv
                live_on=bool(lv.get('enabled')); d['mode']='live' if live_on else 'paper'; d['liveCash']=lv.get('cash')
                if live_on and lv.get('cash') is not None: d['balance']=lv['cash']
                b=json.dumps(d).encode()
            s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.send_header('Access-Control-Allow-Origin','*'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/trades.csv':
            cols=['id','mode','opened ET','closed ET','asset','ticker','side','entry','stake','stakePct','edge','signalSource','status','exitPrice','exitReason','settle','pnl']
            et=lambda x:datetime.fromtimestamp(x,ET).strftime('%Y-%m-%d %H:%M:%S') if x else ''
            buf=io.StringIO(); w=csv.writer(buf); w.writerow(cols)
            with lock: tr=list(pt.trades)
            for t in tr: w.writerow([t.get('id'),t.get('mode','paper'),et(t.get('openedAt')),et(t.get('closedAt')),t.get('asset'),t.get('ticker',''),t.get('side'),t.get('price'),t.get('stake'),t.get('stakePct',''),t.get('edge'),t.get('signalSource',''),t.get('status'),t.get('exitPrice',''),t.get('exitReason',''),t.get('settle',''),t.get('pnl','')])
            b=buf.getvalue().encode(); s.send_response(200); s.send_header('Content-Type','text/csv; charset=utf-8')
            s.send_header('Content-Disposition',f'attachment; filename="paper-desk-trades-{datetime.now(ET).strftime("%Y%m%d-%H%M")}.csv"'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/healthz': b=json.dumps({'ok':True,'persistence':PERSIST}).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.end_headers(); s.wfile.write(b)
        elif p in('/','/index.html','/manifest.json'): super().do_GET()
        else: s.send_error(404)
    def do_POST(s):
        # merge-only restore; requires RESTORE_KEY env + matching X-Restore-Key header. Never deletes trades.
        p=s.path.split('?')[0]
        if p in('/api/live/reset','/api/live/toggle'):
            ks=[k for k in (os.environ.get('LIVE_ADMIN_KEY',''),os.environ.get('RESTORE_KEY','')) if k]
            if not ks or s.headers.get('X-Admin-Key') not in ks: return s.send_error(403)
            if not LV: return s.send_error(503)
            if p=='/api/live/reset': LV.reset()
            else:
                try: body=json.loads(s.rfile.read(int(s.headers.get('Content-Length','0'))) or b'{}')
                except Exception: body={}
                LV.set_paused(not body['on'] if 'on' in body else not LV.paused)
            b=json.dumps(dict(ok=True,enabled=LV.enabled(),paused=LV.paused,killSwitch=LV.killed)).encode()
            s.send_response(200); s.send_header('Content-Type','application/json'); s.end_headers(); return s.wfile.write(b)
        if p!='/api/restore-trades': return s.send_error(404)
        rk=os.environ.get('RESTORE_KEY','')
        if not rk or s.headers.get('X-Restore-Key')!=rk: return s.send_error(403)
        try:
            body=json.loads(s.rfile.read(int(s.headers.get('Content-Length','0'))))
            inc=body.get('trades',body) if isinstance(body,dict) else body
            with lock: before=len(pt.trades); n=pt.merge(inc)
            b=json.dumps({'before':before,'after':n,'balance':round(pt.balance,2)}).encode(); s.send_response(200)
        except Exception as e: b=json.dumps({'error':str(e)}).encode(); s.send_response(400)
        s.send_header('Content-Type','application/json'); s.end_headers(); s.wfile.write(b)
threading.Thread(target=loop,daemon=True).start()
print('Paper Desk (mode '+('LIVE' if LV and LV.enabled() else 'paper')+') on port',PORT,'data',TF,flush=True)
ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
