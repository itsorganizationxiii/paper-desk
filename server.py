# One strategy engine, two modes. PAPER (default): simulated trades/balance, exactly as before.
# LIVE (env LIVE_TRADING=1 + Kalshi keys + dashboard not paused): same engine trades the real Kalshi cash; trades are
# recorded only when contracts actually fill (mode='live'). Live never touches the paper balance (it stays frozen).
import lockhold as LH
import spec as SPEC
TRUTHFIX_AUTO=True  # one-shot Kalshi-record P&L job (fill parsing fixed Nov 8)
def pnl_bounds_ok(t,pnl,tol=0.02):
    st=float(t.get('stake') or 0); px=float(t.get('price') or 0); fees=float(t.get('entryFee') or 0)+float(t.get('exitFee') or 0)
    lo=-(st+fees); hi=(st*(1/px-1)-fees) if px>0 else float('inf')
    return (lo-tol<=pnl<=hi+tol+abs(float(t.get('partialPnl') or 0))),round(lo,4),round(hi,4)
import uuid,json, os, time, math, threading, urllib.request, shutil, csv, io
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
# Owner request Nov 8 9:12 AM ET: paper balance test at the real Kalshi balance. Pre-test paper balance was 2136.83. Set PAPER_TEST_AT=0 to restore (trades/logs kept).
PAPER_TEST_AT=1794147162
PAPER_TEST_START=15.12
# Paper parity with live (Nov 8): paper stops/locks react on websocket quotes like live; paper buys re-check the fresh ask and skip if it ran >1c away.
PAPER_WS_EXITS=True
PAPER_FRESH_FILL=True
# Owner request Nov 8 9:36 AM ET: when live is paused, paper may enter on EloEdge direction even if momentum/CVD disagree (more test trades). Live unaffected.
PAPER_LOOSE=True
KEY=os.environ.get('ELOEDGE_API_KEY','')
KALSHI='https://api.elections.kalshi.com/trade-api/v2'; CB='https://api.exchange.coinbase.com'
ELO='https://eloedge.com/api/v1/windows'; HERMES='https://hermes.pyth.network'
SERIES={'BTC':'KXBTC15M','ETH':'KXETH15M','SOL':'KXSOL15M','XRP':'KXXRP15M','GOLD':'KXGOLD15M','SILVER':'KXSILVER15M'}
METALS={'GOLD':'PF_XAUUSD','SILVER':'PF_XAGUSD'}  # spot/momentum/flow from Kraken public futures (Coinbase has no metals)
KF='https://futures.kraken.com'; ELO_COM='https://eloedge.com/api/v1/commodity15m/windows'; ELO_COM_TTL=int(os.environ.get('ELO_COM_TTL_SEC','30'))
PYTH={'GOLD':'765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2','SILVER':'f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e','OIL':'f2464139c45f26097e0173b03207c4a490dbd8bf1b72dfbf064eeb832c7c2933'}
WHALE_WIN=60; WHALE_MAX_GIVEUP=0.10
# per-asset whale cutoff, calibrated to 6h of Coinbase trades (Nov 2-3 2026): BTC max $200k, p99.99 $101k; ETH max $125k (outlier), p99.99 $38k
WHALE_CFG={'BTC':dict(floor=float(os.environ.get('WHALE_FLOOR_BTC','100000')),cap=float(os.environ.get('WHALE_CAP_BTC','150000')),p99mult=10,volfrac=0.5),
           'ETH':dict(floor=float(os.environ.get('WHALE_FLOOR_ETH','35000')),cap=float(os.environ.get('WHALE_CAP_ETH','60000')),p99mult=4,volfrac=0.5),
           'SOL':dict(floor=float(os.environ.get('WHALE_FLOOR_SOL','25000')),cap=float(os.environ.get('WHALE_CAP_SOL','45000')),p99mult=4,volfrac=0.5),
           'XRP':dict(floor=25000,cap=45000,p99mult=4,volfrac=0.5)}
# take-profit: sell at bid (minus Kalshi taker fee) once our side's bid >= TP_BID, unless very confident it won't flip
# profit lock arms once the bid is LOCK_ARM_C above ENTRY (so mid-range peaks 55c->76c->12c get protected),
# or at the old fixed peaks, whichever comes first. LOCK_ARM_C=0 disables the entry-relative arm.
LOCK_PEAK=0.80; LOCK_DROP=0.10; BIG_RUN=float(os.environ.get('LOCK_BIG_RUN','0.20')); BIG_DROP=float(os.environ.get('LOCK_BIG_DROP','0.05')); LOCK_HOLD_P=1.01
LOCK_ARM_C=float(os.environ.get('LOCK_ARM_C','0.10'))
# Weekend lock thresholds are code constants, not deployment overrides.
WKND_LOCK_ARM_C=0.20
WKND_DROP=0.07
WKND_MODE=os.environ.get('WKND_MODE','volatile')  # 'volatile' (thin weekend: Up bets lock early) or 'normal'
WKND_UP_ARM=0.18; WKND_UP_DROP=0.08
def wknd_up(t): return t is not None and WKND_MODE=='volatile' and wknd_night() and t.get('side')=='YES'
def tp_bid(): return (WKND_TP if WKND_TP>0 else 9.0) if (WKND_MODE=='volatile' and wknd_night()) else TP_BID
def stop_pct(): return WKND_STOP if (WKND_STOP>0 and wknd_night()) else STOP
def lock_arm(t=None): return WKND_UP_ARM if wknd_up(t) else (WKND_LOCK_ARM_C if wknd_night() else LOCK_ARM_C)
def lock_drop(t): return WKND_UP_DROP if wknd_up(t) else (WKND_DROP if WKND_DROP>0 and wknd_night() else (BIG_DROP if t['peakBid']-t['price']>=BIG_RUN else LOCK_DROP))
# lock used to be night-only; replay of 153 peak-recorded trades showed arming entry+10c all day
# improves 64 exits by +$31.18 with no offsetting cost, so it now runs around the clock (LOCK_ALL_DAY=0 reverts)
LOCK_ALL_DAY=os.environ.get('LOCK_ALL_DAY','1')!='0'
def lock_active(ts=None): return LOCK_ALL_DAY or night(ts)
# metals flip harder off their peaks (thin book), so they lock in lower: 65c peak instead of 80c
LOCK_PEAK_BY_ASSET={'GOLD':float(os.environ.get('LOCK_PEAK_GOLD','0.65')),'SILVER':float(os.environ.get('LOCK_PEAK_SILVER','0.65'))}
TP_BID=0.91; WKND_TP=float(os.environ.get('WKND_TP','0')); TP_HOLD_P=0.97; TP_HOLD_Z=3.0; TP_HOLD_MAX_LEFT=180
# metals only: once the bid is 10c above entry, never give back past entry (thin books gap through the 40% stop)
GUARD_ASSETS={'GOLD','SILVER'}; GUARD_GIVEBACK=float(os.environ.get('GUARD_GIVEBACK','0.0'))
def kalshi_fee(c,p): return math.ceil(7*c*p*(1-p))/100  # Kalshi taker fee: ceil(0.07*C*P*(1-P)) in $
MIN_EDGE=0.03; NIGHT_EDGE=0.05; WKND_EDGE=float(os.environ.get('WKND_NIGHT_EDGE','0.07')); REV=os.environ.get('REVERSAL','0')!='0'; REV_MAX=float(os.environ.get('REVERSAL_MAX_PRICE','0.60')); FADE=os.environ.get('WKND_FADE','0')!='0'; WKND_MAX_EDGE=0.10; MIN_ENTRY=float(os.environ.get('MIN_ENTRY','0.40')); EXIT_EDGE=0.03; STOP=0.40; WKND_STOP=float(os.environ.get('WKND_STOP','0.60')); KELLY=0.25; CAP=0.08; TICK=int(os.environ.get('TICK_SEC','10')); REST_SEC=int(os.environ.get('LIVE_REST_SEC','30')); LIVE_TICK=int(os.environ.get('LIVE_TICK_SEC','5'))
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
        if LV is not None and LV.lease_ok is not None and not LV.owner(): continue  # stale instance: never overwrite the new one's state
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
        pp=[t for t in tr if t.get('mode')!='live' and not t.get('shadow')]
        st=s.start
        if PAPER_TEST_AT: pp=[t for t in pp if (t.get('openedAt') or 0)>=PAPER_TEST_AT]; st=PAPER_TEST_START
        s.balance=st+sum(t.get('pnl') or 0 for t in pp if t.get('status')!='open')-sum(t.get('stake') or 0 for t in pp if t.get('status')=='open')
        s.save(); return len(tr)
    def save(s):
        # P&L sanity check: -(stake+fees) <= pnl <= stake*(1/price-1)-fees; outside -> flagged, not stored
        for t in s.trades:
            if t.get('status') in('won','lost','exited') and 'pnlChecked' not in t and t.get('pnl') is not None:
                ok,lo,hi=pnl_bounds_ok(t,t['pnl']); t['pnlChecked']=True
                if not ok: t.update(pnlRaw=t['pnl'],pnl=None,pnlFlagged=dict(lo=lo,hi=hi,at=time.time())); print('PNL FLAG',t.get('id'),t['pnlRaw'],lo,hi,flush=True)
        tmp=TF+'.tmp'; json.dump({'balance':round(s.balance,4),'startBalance':s.start,'trades':s.trades},open(tmp,'w'),indent=1); os.replace(tmp,TF)
        if MCOL is not None: _dirty.set()
    @staticmethod
    def size(side,ask,p_yes,bid=None):
        # quarter Kelly fraction (cap, night halving); shared by paper and live
        # SPEC_EDGE/SPEC_GATES (default off) keep byte-for-byte current edge/floor behavior.
        q=p_yes if side=='YES' else 1-p_yes
        edge,bd=SPEC.decision_edge(q,ask,bid)
        nt=night()
        if edge is None or edge<need_edge() or not (0<ask<1): return None
        if SPEC.gates_block(ask,bid): return None
        if (not SPEC.SPEC_GATES) and MIN_ENTRY>0 and ask<MIN_ENTRY: return None  # entry floor: sub-40c entries lost in live + 7-weekend backtest (all signals, paper+live)
        # weekend max-edge / min-ask still use raw edge for flags-off parity; with SPEC_EDGE use decision edge
        edge_for_wknd=edge if SPEC.SPEC_EDGE else (q-ask)
        if WKND_MAX_EDGE>0 and wknd_night() and edge_for_wknd>WKND_MAX_EDGE: return None
        if WKND_MIN>0 and wknd_night() and ask<WKND_MIN: return None
        return min(KELLY*edge/(1-ask),CAP)*(0.5 if nt else 1),edge,q
    def openLive(s,asset,side,fill,strike,close,ticker,meta,edge,q):
        if meta.get('shadow') or meta.get('gap') or str(meta.get('signalSource','')).startswith('gap') or meta.get('mode','live')!='live' or 'XRP' in str(ticker).upper():
            raise ValueError('shadow/gap metadata forbidden for live fill')
        existing=next((t for t in s.trades if t.get('mode')=='live' and not t.get('shadow') and t.get('status')=='open' and t.get('ticker')==ticker and t.get('side')==side and (t.get('liveOrderId')==fill.get('orderId') or t.get('adopted'))),None)
        if existing:
            if LV: LV.pending_fills.pop(fill.get('orderId'),None)
            return existing
        meta={k:v for k,v in meta.items() if k!='mode'}
        stake=round(fill['cost']+fill['fee'],4)
        t=dict(id=max([x.get('id',0) for x in s.trades]+[0])+1,mode='live',asset=asset,side=side,price=fill['price'],stake=stake,stakePct=round(100*fill['frac'],2),
               contracts=fill['contracts'],entryFee=fill['fee'],liveOrderId=fill['orderId'],liveCashAtEntry=fill['cash'],
               edge=round(edge,3),modelP=round(q,4),strike=strike,ticker=ticker,openedAt=time.time(),closesAt=close,status='open',**meta)
        t['perpPremium']=perp_premium(asset); t['regime']=regime(asset); t['flowAtEntry']=flow(asset)
        s.trades.append(t); s.save()
        if LV: LV.pending_fills.pop(fill.get('orderId'),None)
        print('OPEN LIVE',t['id'],ticker,side,fill['price'],fill['contracts'],flush=True); return t
    def adopt(s,asset,side,contracts,price,fee,ticker,strike,close,order_ids,fee_known):
        t=dict(id=max([x.get('id',0) for x in s.trades]+[0])+1,mode='live',shadow=False,adopted=True,
               asset=asset,side=side,contracts=contracts,price=round(price,6),stake=round(contracts*price+fee,6),
               stakePct=0,entryFee=round(fee,6),adoptionFeesKnown=fee_known,adoptedOrderIds=order_ids,
               ticker=ticker,strike=strike,closesAt=close,openedAt=time.time(),status='open',
               signalSource='portfolio-reconcile',priceSource='kalshi-position',edge=0,modelP=None)
        s.trades.append(t); s.save()
        print('ADOPT LIVE',t['id'],ticker,side,contracts,price,flush=True)
        return t
    def openTrade(s,asset,side,ask,p_yes,strike,close,ticker,meta):
        if PAPER_FRESH_FILL and not meta.get('shadow') and LV is not None:
            try: fq=LV.wsq(ticker)
            except Exception: fq=None
            fa=None if not fq or fq.get('yesAsk') is None or fq.get('yesBid') is None else (fq['yesAsk'] if side=='YES' else round(1-fq['yesBid'],4))
            if fa is not None:
                if fa>ask+0.01: print('PAPER NOFILL',ticker,side,ask,fa,flush=True); return None
                ask=fa
        bid=meta.get('_bid') if isinstance(meta,dict) else None
        sz=s.size(side,ask,p_yes,bid)
        if not sz: return None
        f,edge,q=sz; bal=s.balance; sh=bool(meta.get('shadow'))
        stake=SHADOW_USD if (sh or PAPER_FLAT) else round(bal*f,2)
        if stake<0.05: return None
        if not sh: s.balance-=stake
        t=dict(id=max([x.get('id',0) for x in s.trades]+[0])+1,asset=asset,side=side,price=round(ask,4),stake=stake,stakePct=0 if sh else round(100*stake/bal,2),contracts=round(max(stake-kalshi_fee(stake/ask,ask),0.01)/ask,3),entryFee=kalshi_fee(stake/ask,ask),
               edge=round(edge,3),modelP=round(q,4),strike=strike,ticker=ticker,openedAt=time.time(),closesAt=close,status='open',**meta)
        t['perpPremium']=perp_premium(asset); t['regime']=regime(asset); t['flowAtEntry']=flow(asset)
        s.trades.append(t); s.save(); print('OPEN',t['id'],ticker,side,ask,stake,flush=True)
        return t
    def close(s,t,win,how,val=None):
        payout=t['contracts'] if win else 0
        if t.get('mode')=='live' and not t.get('shadow'):
            pnl=round(payout-t['stake'],2); t.update(status='won' if win else 'lost',settle=val,settledBy=how,pnl=round(pnl+(t.get('partialPnl') or 0),2),closedAt=time.time())
            if LV: LV.book(pnl); LV.log(action='settle',ticker=t['ticker'],side=t['side'],count=t['contracts'],result=t['status'],pnl=pnl)
        else:
            t.update(status='won' if win else 'lost',settle=val,settledBy=how,pnl=round(payout-t['stake'],2),closedAt=time.time())
            if not t.get('shadow'): s.balance+=payout
        for k in('markPrice','unrealizedPnl','flipWarning','signalsAgree','whaleAgainst'): t.pop(k,None)
    def exit(s,t,bid,reason,fee=0.0):
        if t.get('mode')=='live' and not t.get('shadow'): return s.exitLive(t,bid,reason)
        proceeds=t['contracts']*bid-fee
        t.update(status='exited',exitPrice=round(bid,4),exitReason=reason,exitFee=round(fee,2),settledBy='early-exit@bid',pnl=round(proceeds-t['stake'],2),closedAt=time.time())
        for k in('markPrice','unrealizedPnl','flipWarning','signalsAgree','whaleAgainst'): t.pop(k,None)
        if not t.get('shadow'): s.balance+=proceeds
        s.save(); print('EXIT',t['id'],reason,bid,flush=True)
    def exitLive(s,t,bid,reason):
        # real sell runs off the lock; trade stays 'open' (flagged exiting) until the fill is known
        if t.get('shadow') or t.get('mode')!='live' or 'XRP' in str(t.get('ticker')).upper(): return
        if not (LV and LV.allowed() and LV.owner()): t['liveSellError']='live disabled (env/keys); holding to settlement'; return
        if t.get('exiting'): return
        t['exiting']=reason; t['exitTrigAt']=time.time(); t['exitTrigBid']=round(bid,4)
        threading.Thread(target=s._sellLive,args=(t,bid,reason),daemon=True).start()
    def _sellLive(s,t,bid,reason):
        if t.get('shadow') or t.get('mode')!='live' or 'XRP' in str(t.get('ticker')).upper(): return
        floor=round(t['price']+0.01,2) if str(reason).lower().startswith(('profitlock','take','tp','breakeven')) is True and not str(reason).lower().startswith('breakeven') else None  # profit exits never sell below entry
        isstop=str(reason).lower().startswith('stop')
        stl=round(stop_pct()*t['price'],4)
        try:
            if isstop: fc,fpx,fee,oid,err=LV.stop_sell(t['ticker'],t['side'],t['contracts'],stl,bid,t.get('exitTrigAt'))
            else: fc,fpx,fee,oid,err=LV.sell(t['ticker'],t['side'],t['contracts'],bid,floor=floor)
        except Exception as e: fc,fpx,fee,oid,err=0,None,0.0,None,f'{type(e).__name__}: {e}'
        with lock:
            t.pop('exiting',None)
            if isstop and fc>0: t.update(stopLevel=stl,stopBidAtTrigger=t.get('exitTrigBid'),stopTriggerAt=t.get('exitTrigAt'),stopFillPrice=fpx,stopFillAt=time.time(),stopSlipC=round((stl-fpx)*100,1) if fpx is not None else None)
            if fc<=0:
                t.update(liveSellError=err or 'no fill',liveSellAt=time.time())
                ext=False
                try:
                    p=LV.position(t['ticker'])
                    ext=p is not None and ((p if t['side']=='YES' else -p)<=0.001)
                except Exception: ext=False
                rec=None
                try: rec=LV.closing_fills(t['ticker'],t['side'],t.get('liveOrderId'),t.get('openedAt') or 0)
                except Exception as e: print('fill recover err',e,flush=True)
                if rec and rec[0]>=t['contracts']-0.01:
                    # The sale DID fill on Kalshi (possibly by an earlier order/instance): book the real fills, never "no fill"
                    q,fpx2,fee2,oids=rec; t.pop('liveSellError',None)
                    t.update(status='exited',exitPrice=fpx2,exitFee=fee2,exitReason=reason,settledBy='live-sell (recovered from Kalshi fills)',liveSellOrderId=oids[-1] if oids else None,
                             pnl=round(q*fpx2-fee2-t['stake']+(t.get('partialPnl') or 0),4),closedAt=time.time(),fillRecovered=True)
                    LV.log(action='sell-fill-recovered',ticker=t['ticker'],side=t['side'],filled=q,fillPrice=fpx2,fee=fee2,orderIds=oids)
                    s.save(); 
                    if isstop: LV.persist()
                    return
                if ext:
                    # Owner sold manually (or position otherwise gone): stop retrying, book at the trigger bid as an estimate
                    epx=round(t.get('exitTrigBid') or bid or 0,4)
                    t.update(status='exited',exitPrice=epx,exitReason=f'closed outside bot (no Kalshi position) during: {reason}',settledBy='external',pnlEstimated=True,
                             pnl=round(t['contracts']*epx-t['stake']+(t.get('partialPnl') or 0),2),closedAt=time.time())
                    LV.log(action='external-close',ticker=t['ticker'],side=t['side'],estPrice=epx)
                    print('EXIT EXTERNAL',t['id'],flush=True)
                s.save()
                if isstop: LV.persist()
                return
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
                        try:
                            py=None
                            if t['asset'] in('GOLD','SILVER'):
                                py=(status.get('commodities') or {}).get(t['asset'],{}).get('price')
                            SPEC.append_research(SPEC.settlement_row(asset=t['asset'],ticker=t.get('ticker'),side=t.get('side'),
                                coinbase_spot=spot.get(t['asset']) if isinstance(spot,dict) else None,
                                kalshi_result=m.get('result'),kalshi_expiration_value=m.get('expiration_value'),
                                pyth_price=py,settled_by='kalshi',trade_id=t.get('id'),
                                fill_ts=t.get('openedAt'),opened_at=t.get('openedAt'),closed_at=t.get('closedAt')),DATA)
                        except Exception: pass
                        continue
                except Exception as e: print('kalshi settle err',e,flush=True)
                if now<t['closesAt']+600: continue
            if t['asset'] in spot:
                s.close(t,(spot[t['asset']]>=t['strike'])==(t['side']=='YES'),'coinbase-spot',spot[t['asset']]); ch=True
                try:
                    SPEC.append_research(SPEC.settlement_row(asset=t['asset'],ticker=t.get('ticker'),side=t.get('side'),
                        coinbase_spot=spot[t['asset']],kalshi_result=None,kalshi_expiration_value=None,
                        settled_by='coinbase-spot',trade_id=t.get('id'),
                        fill_ts=t.get('openedAt'),opened_at=t.get('openedAt'),closed_at=t.get('closedAt')),DATA)
                except Exception: pass
            # counterfactual for early exits: what holding to settlement would have paid
        for t in s.trades:
            if t['status']=='exited' and t.get('ticker') and 'wouldHaveWon' not in t and now>=t['closesAt']:
                try:
                    m=get(f"{KALSHI}/markets/{t['ticker']}")['market']
                    if m.get('result') in('yes','no'):
                        win=(m['result']=='yes')==(t['side']=='YES'); hold=round((t['contracts'] if win else 0)-t['stake'],2)
                        t.update(finalResult=m['result'],wouldHaveWon=win,counterfactualPnl=hold,exitSavedPnl=round((t.get('pnl') or 0)-hold,2)); ch=True
                except Exception as e: print('cf err',e,flush=True)
        # hold_pnl: after-fee P&L if held to settlement from the same entry (backfills any trade with a known result)
        for t in s.trades:
            if 'hold_pnl' in t: continue
            if t['status'] in('won','lost'): w=t['status']=='won'
            elif t['status']=='exited' and 'wouldHaveWon' in t: w=t['wouldHaveWon']
            else: continue
            t['hold_pnl']=(t.get('pnl') or 0) if t['status'] in('won','lost') else round((t.get('contracts') or 0)*(1 if w else 0)-(t.get('stake') or 0)-(t.get('entryFee') or 0),4); ch=True
        if ch: s.save()
    def exitSummary(s):
        ex=[t for t in s.trades if t['status']=='exited']; done=[t for t in ex if 'wouldHaveWon' in t]
        def agg(ts):
            return dict(count=len(ts),resolved=len([t for t in ts if 'wouldHaveWon' in t]),wouldHaveWon=len([t for t in ts if t.get('wouldHaveWon')]),
                exitPnl=round(sum((t.get('pnl') or 0) for t in ts),2),holdPnl=round(sum(t.get('counterfactualPnl',0) for t in ts if 'wouldHaveWon' in t),2),
                netSaved=round(sum(t.get('exitSavedPnl',0) for t in ts),2),
                savedOnLosers=round(sum(t['exitSavedPnl'] for t in ts if 'wouldHaveWon' in t and not t['wouldHaveWon']),2),
                lostOnWinners=round(-sum(t['exitSavedPnl'] for t in ts if t.get('wouldHaveWon')),2))
        by={}
        for t in ex: by.setdefault((t.get('exitReason') or 'other').split(':')[0],[]).append(t)
        hd=[t for t in s.trades if t.get('tpHeld') and t['status'] in('won','lost')]
        held=dict(count=len([t for t in s.trades if t.get('tpHeld')]),resolved=len(hd),won=len([t for t in hd if t['status']=='won']),
            lost=len([t for t in hd if t['status']=='lost']),holdPnl=round(sum((t.get('pnl') or 0) for t in hd),2),
            tpPnlIfSold=round(sum(t.get('tpSellPnl',0) for t in hd),2),holdGain=round(sum((t.get('pnl') or 0)-t.get('tpSellPnl',0) for t in hd),2))
        return dict(all=agg(ex),byReason={k:agg(v) for k,v in by.items()},pending=len(ex)-len(done),takeProfitHolds=held)
    def getStats(s):
        def st(ts):
            c=[t for t in ts if t['status']!='open']; w=[t for t in c if t.get('pnl',0)>0]; o=[t for t in ts if t['status']=='open']
            return dict(trades=len(ts),closed=len(c),open=len(o),wins=len(w),winRate=round(len(w)/len(c),3) if c else 0,pnl=round(sum((t.get('pnl') or 0) for t in c),2),
                expectancy=round(sum((t.get('pnl') or 0) for t in c)/len(c),3) if c else 0,unrealizedPnl=round(sum(t.get('unrealizedPnl',0) for t in o),2))
        pp=[t for t in s.trades if t.get('mode')!='live' and not t.get('shadow')]; lt=[t for t in s.trades if t.get('mode')=='live' and not t.get('shadow')]
        P=st(pp); Lv=st(lt); A=st(pp+lt); o=[t for t in pp if t['status']=='open']
        sh={f'{x} shadow':st([t for t in s.trades if is_shadow(t) and t.get('asset')==x]) for x in ('BTC','ETH','SOL','XRP')}
        return dict(mode='paper',balance=round(s.balance,2),paperBalance=round(s.balance,2),startBalance=s.start,
            equity=round(s.balance+sum(t.get('markPrice',t['price'])*t['contracts'] for t in o),2),
            unrealizedPnl=P['unrealizedPnl'],winRate=P['winRate'],expectancy=P['expectancy'],paperStats=P,liveStats=Lv,shadowStats=sh,
            closed=A['closed'],open=A['open'],earlyExits=s.exitSummary(),trades=s.trades[::-1])
SHADOW_USD=1.00
# Owner request Nov 8: paper trades bet flat $1 like live, not Kelly of the paper balance. Set False to restore.
PAPER_FLAT=True
LV=None
lock=threading.Lock(); pt=PaperTrader()
for _t in pt.trades:
    if _t.get('status')=='open' and _t.pop('exiting',None): _t['liveSellError']='sell thread lost in restart; exit/settle re-armed'  # a dead sell thread must not block settlement forever
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
    from trader_lease import TraderLease
    INSTANCE=f'{os.getpid()}-{uuid.uuid4().hex[:8]}-{int(time.time())}'
    LEASE=TraderLease(MCOL,INSTANCE)
    LV.lease_ok=LEASE.check
    LV.entry_block=lambda asset,ticker:shadow_asset(str(asset).upper()) or 'XRP' in str(ticker).upper()
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
    try: ret15=round((c[1][4]-c[1][3])/c[1][3],6) if len(c)>1 and c[1][3] else None
    except Exception: ret15=None
    return dict(sd15=sd15,spot=spot,momentum=round(mom,5),cvd=round(cvd,4),cvdRatio=round(cvd/vol,3) if vol else 0,ret15=ret15,
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
    try: ret15=round((cl[1]-float(c[1]['open']))/float(c[1]['open']),6) if len(cl)>1 and float(c[1]['open']) else None
    except Exception: ret15=None
    return dict(sd15=sd15,spot=spot,momentum=round(mom,5),cvd=round(cvd,4),cvdRatio=round(cvd/vol,3) if vol else 0,ret15=ret15,
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
# Weekend-night entry floor (Nov 8 5 AM ET): live since Fri 5 PM, entries under 60c went 8-16 (-$3.46), 60-69c went 8-3 (+$1.81). Set 0 to disable.
WKND_MIN=0.60
WKND_MAX=float(os.environ.get('WKND_NIGHT_MAX_PRICE','0.69'))  # 0 disables
def wknd_night(ts=None):
    # Fri 5 PM ET -> Mon 9 AM ET (thin weekend volume)
    d=datetime.fromtimestamp(ts or time.time(),ET); wd,h=d.weekday(),d.hour
    return (wd==4 and h>=17) or wd in (5,6) or (wd==0 and h<9)
COOLDOWNS_ENABLED=False
COOL_LOSSES=3; COOL_SEC=3600; SHARED_COOL_LOSSES=2; SHARED_COOL_SEC=1800; _cool={'at':0,'map':{}}; _cool_logged=set()
def cooldowns(now=None):
    # per-coin loss-streak cooldown, derived from closed live trades so restarts keep it
    now=now or time.time()
    if now-_cool['at']<5 and _cool['map'] is not None: return _cool['map']
    tr=sorted([t for t in pt.trades if t.get('mode')=='live' and not t.get('shadow') and t.get('status') in ('won','lost','exited') and t.get('closedAt')],key=lambda t:t['closedAt'])
    st={}; out={}; shared_streak=0; shared_until=0; loss_streaks={}
    for t in tr:
        if (t.get('pnl') or 0)<0:
            shared_streak+=1
            if shared_streak>=SHARED_COOL_LOSSES: shared_until=t['closedAt']+SHARED_COOL_SEC
        else: shared_streak=0
        a=t.get('asset'); loss_streaks[a]=loss_streaks.get(a,0)+1 if (t.get('pnl') or 0)<0 else 0
        s=st.setdefault(a,{'streak':0,'until':0})
        if (t.get('pnl') or 0)<0:
            s['streak']+=1
            if s['streak']>=COOL_LOSSES: s['until']=t['closedAt']+COOL_SEC; s['streak']=0
        else: s['streak']=0
    for a,s in st.items():
        if s['until']>now:
            out[a]=dict(until=round(s['until']),minsLeft=round((s['until']-now)/60,1))
            if COOLDOWNS_ENABLED and (a,round(s['until'])) not in _cool_logged: _cool_logged.add((a,round(s['until']))); print('COOLDOWN',a,'shadow-only until',datetime.fromtimestamp(s['until'],ET).strftime('%H:%M ET'),flush=True)
    if shared_until>now:
        out['ALL']=dict(until=round(shared_until),minsLeft=round((shared_until-now)/60,1),reason='2 consecutive closed live losses across coins; all new live entries cooling down')
    _cool.update(at=now,map=out if COOLDOWNS_ENABLED else {},streaks=dict(ALL=shared_streak,**loss_streaks))
    return _cool['map']
def shadow_asset(a,ts=None):
    # paper-only shadow: XRP always; any coin in a loss-streak cooldown. Never a live entry.
    cd=cooldowns()
    return a=='XRP' or a in cd or 'ALL' in cd
def is_shadow(t): return bool(t.get('shadow'))
ALT=os.environ.get('ALT_CONSENSUS','1')!='0'; ALT_MAX=float(os.environ.get('ALT_MAX_PRICE','0.60')); _altc={}
from altcap import state as alt_cap_state
_alt_cap_skips=[]; _alt_cap_seen=set()
ALT_CAP_ENABLED=False
def alt_cap_allow(ticker,now=None):
    if not ALT_CAP_ENABLED: return True
    cap=alt_cap_state(pt.trades,now)
    if not cap['blocked']: return True
    key=(ticker,cap['blockedUntil'],cap['reason'])
    if key not in _alt_cap_seen:
        _alt_cap_seen.add(key)
        if len(_alt_cap_seen)>200: _alt_cap_seen.clear(); _alt_cap_seen.add(key)
        event=dict(at=time.time() if now is None else now,ticker=ticker,reason=cap['reason'],blockedUntil=cap['blockedUntil'])
        _alt_cap_skips.append(event)
        del _alt_cap_skips[:-20]
        print('ALT CAP SKIP',ticker,cap['reason'],flush=True)
        if LV: LV.log(action='alt-cap-skip',ticker=ticker,reason=cap['reason'],blockedUntil=cap['blockedUntil'])
    return False
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
_FSYM={'BTC':'PF_XBTUSD','ETH':'PF_ETHUSD','SOL':'PF_SOLUSD','GOLD':'PF_XAUUSD','SILVER':'PF_XAGUSD','XAU':'PF_XAUUSD','XAG':'PF_XAGUSD'}
_flow={}; _fseen=set()
def _flow_poller():
    # log-only: Kraken futures taker flow + liquidations per minute (user Nov 7)
    while True:
        for a,sym in set(_FSYM.items()):
            try:
                h=json.load(urllib.request.urlopen(f'https://futures.kraken.com/derivatives/api/v3/history?symbol={sym}',timeout=8)).get('history',[])
                for x in h:
                    u=x.get('uid')
                    if not u or u in _fseen: continue
                    _fseen.add(u); mn=x['time'][:16]; usd=float(x['price'])*float(x['size'])
                    b=_flow.setdefault(sym,{}).setdefault(mn,{'buy':0.0,'sell':0.0,'liqLong':0.0,'liqShort':0.0})
                    b['buy' if x.get('side')=='buy' else 'sell']+=usd
                    if x.get('type')=='liquidation': b['liqLong' if x.get('side')=='sell' else 'liqShort']+=usd
                for k in sorted(_flow.get(sym,{}))[:-40]: _flow[sym].pop(k,None)
            except Exception as e: print('flow err',sym,e,flush=True)
        if len(_fseen)>200000: _fseen.clear()
        time.sleep(20)
def flow(asset,mins=2):
    sym=_FSYM.get(asset.upper())
    if not sym or sym not in _flow: return None
    ks=sorted(_flow[sym])[-mins:]; b=sum(_flow[sym][k]['buy'] for k in ks); se=sum(_flow[sym][k]['sell'] for k in ks)
    return {'buy':round(b),'sell':round(se),'imb':round((b-se)/(b+se),3) if b+se else 0,'liqLong':round(sum(_flow[sym][k]['liqLong'] for k in ks)),'liqShort':round(sum(_flow[sym][k]['liqShort'] for k in ks))}
threading.Thread(target=_flow_poller,daemon=True).start()
# Shared live entry flow gate (Nov 8): Kraken taker flow against our side lost (28% win vs 55% with flow). Set FLOW_GATE_MIN=None to disable.
FLOW_GATE_MIN=0.0
FLOW_GATE_MIN_USD=1000
def flow_blocked(asset,side):
    if FLOW_GATE_MIN is None: return False
    fl=flow(asset)
    if not fl or fl.get('imb') is None or (fl.get('buy',0)+fl.get('sell',0))<FLOW_GATE_MIN_USD: return False
    s_imb=fl['imb'] if side=='YES' else -fl['imb']
    return s_imb<FLOW_GATE_MIN
def _gbuy(asset,tk,side,*a,**kw):
    if not kw.get('shadow') and kw.get('mode','live')=='live' and flow_blocked(asset,side):
        LV.lastBuy='flow'; print('FLOW GATE skip',asset,tk,side,flush=True); return None
    return LV.buy(asset,tk,side,*a,**kw)
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
import uptilt
def up_tilt_skip(a,ticker,side,edge,path):
    if not uptilt.blocked(side,edge,need_edge(),wknd_night()): return False
    print(f'up-tilt skip {path} {a} {ticker} edge={edge}',flush=True)
    if LV: LV.log(defer=True,action='up-tilt-skip',ticker=ticker,side=side,path=path,reason='up-tilt',edge=None if edge is None else round(edge,3),need=round(need_edge()+uptilt.UP_TILT,3))
    return True
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
from leadlag import LeadLag, entry_skip, MIN_LEFT as LL_MIN_LEFT, MAX_SIGNAL_AGE as LL_MAX_AGE, FRESH as LL_FRESH, MOVE as LL_MOVE, MAX_RETRACE as LL_RETRACE
import leadlag
LL=LeadLag()
def leadlag_entry(k,model_p=None):
    # Only the normal strategy thread submits entries; websocket callbacks never wait for orders.
    for ev in LL.drain():
        try:
            now=time.time(); tk=k['ticker']; side=ev['side']
            with lock:
                trades=list(pt.trades)
            paper=not (LV and LV.enabled())
            used=tk in LL.attempted or any(t.get('ticker')==tk and t.get('leadlag') for t in trades)
            held=any(t.get('ticker')==tk and t.get('mode')=='live' and t.get('status')=='open' for t in trades)
            cd=cooldowns(now)
            reason=entry_skip(ev,tk,k['close']-now,used,held,'BTC' in cd or 'ALL' in cd)
            if reason=='BTC loss cooldown' and 'ALL' in cd: reason='ALL shared loss cooldown'
            if reason is None and now-ev['at']>LL_MAX_AGE: reason='stale trigger'
            if reason is None and leadlag.LEADLAG_PAUSED and not paper: reason='paused by user (live only)'
            if reason is None and up_tilt_skip('BTC',tk,side,None,'leadlag'): reason='up-tilt'
            if reason is None and (tk in LV.tried or tk in LV.resting): reason='BTC order already working/tried'
            if reason is None and any(t.get('status')=='open' and not is_shadow(t) and t.get('asset')!='BTC' and t.get('closesAt')==k['close'] and t.get('side')!=side for t in trades):
                reason='opposite live coin position this window'
            quote=LV.wsq(tk) if reason is None else None
            if reason is None and (not quote or quote.get('yesBid') is None or quote.get('yesAsk') is None): reason='no fresh BTC websocket quote'
            if reason is None and LL.caught_up(ev,(quote['yesBid']+quote['yesAsk'])/2): reason='BTC caught up before entry'
            if reason is None:
                with LL.lock:
                    leader=LL.history.get(ev['triggerCoin'])
                    latest=leader[1][-1] if leader and leader[0]==ev['triggerTicker'] else None
                sign=1 if side=='YES' else -1
                if not latest or now-latest[0]>LL_FRESH or sign*(latest[1]-ev['leaderBase'])+1e-9<max(LL_MOVE,abs(ev['move'])-LL_RETRACE):
                    reason='leader stale or move retraced >2c'
            if reason is None:
                _qs=None if model_p is None else (model_p if side=='YES' else 1-model_p)
                ev['eloP']=None if _qs is None else round(_qs,4)
                if _qs is None: reason='no fresh EloEdge model p'
                elif _qs+1e-9<leadlag.MIN_MODEL_P: reason='EloEdge model p below 60%'
            if reason:
                LL.finish(ev,reason); continue
            ask=quote['yesAsk'] if side=='YES' else round(1-quote['yesBid'],4)
            if _qs is not None and _qs-ask<need_edge():
                LL.finish(ev,'real EloEdge edge below minimum'); continue
            # This is a directional hypothesis, not an EloEdge probability estimate.
            # Use the existing minimum-edge sizing adapter, exactly as alt-consensus does.
            q=min(ask+need_edge()+0.001,0.99); mp=q if side=='YES' else 1-q
            def size_at(px):
                if k['close']-time.time()<LL_MIN_LEFT or not (paper or LV.enabled()) or shadow_asset('BTC'): return None
                if WKND_MAX>0 and wknd_night() and px>WKND_MAX: return None
                return (pt.size(side,px,mp) or [None])[0]
            sz=pt.size(side,ask,mp)
            if not sz or size_at(ask) is None:
                LL.finish(ev,'normal entry price/edge filter'); continue
            LL.attempted={x for x in LL.attempted if x==tk}
            LL.attempted.add(tk)
            LL.last_error=None
            if paper or SPEC.EXPERIMENTAL_SHADOW_ONLY:
                pmeta=dict(priceSource='kalshi',signalSource='leadlag',leadlag=True,leadlagTriggerCoin=ev['triggerCoin'],leadlagMove=ev['move'],leadlagBtcMove=ev['btcMove'],leadlagTriggerAt=ev['at'],leadlagEloP=ev.get('eloP'))
                if SPEC.EXPERIMENTAL_SHADOW_ONLY: pmeta=SPEC.experimental_force_shadow(pmeta)
                if flow_blocked('BTC',side): LL.finish(ev,'flow gate'); continue
                with lock: trade=pt.openTrade('BTC',side,ask,mp,k['strike'],k['close'],tk,pmeta)
                LL.finish(ev,trade=trade) if trade else LL.finish(ev,'paper size filter'); continue
            fill=_gbuy('BTC',tk,side,sz[0],ask,sizer=size_at,shadow=False,mode='live')
            if fill:
                meta=dict(priceSource='kalshi',signalSource='leadlag',leadlag=True,
                          leadlagTriggerCoin=ev['triggerCoin'],leadlagMove=ev['move'],
                          leadlagBtcMove=ev['btcMove'],leadlagTriggerAt=ev['at'],leadlagEloP=ev.get('eloP'))
                with lock: trade=pt.openLive('BTC',side,fill,k['strike'],k['close'],tk,meta,sz[1],sz[2])
                LL.finish(ev,trade=trade)
            else: LL.finish(ev,'no fill: '+str(LV.lastBuy or 'execution guard'))
        except Exception as e:
            LL.last_error=type(e).__name__
            LL.finish(ev,'entry error: '+type(e).__name__)
            print('leadlag entry error',type(e).__name__,flush=True)

from ethlead import EthLead, evaluate as el_eval, ETHLEAD_MIN_LEFT as EL_MIN_LEFT
import ethlead
EL=EthLead()
def ethlead_entry(k,model_p=None):
    # ETH-leads-BTC: buy BTC on ETH's >50c side when BTC is 7-10c cheaper; one per window.
    try:
        now=time.time(); tk=k['ticker']; left=k['close']-now
        if not LV: return
        paper=not LV.enabled()
        if now<ethlead.ETHLEAD_RESUME_AT: EL.last_reason='paused by user until 8 AM ET'; return
        eq=LV.wsq('KXETH15M-'+tk.split('15M-',1)[1]); bq=LV.wsq(tk)
        side,ea,ba,gap,reason=el_eval(eq,bq,left,now)
        EL.last_reason=reason
        if reason: return
        ev=dict(at=now,ticker=tk,side=side,ethAsk=ea,btcAsk=ba,gapC=round(gap*100,2),secsLeft=round(left),status='pending',skipReason=None,
                eloP=None if model_p is None else round(model_p if side=='YES' else 1-model_p,4))
        with lock: trades=list(pt.trades)
        if tk in EL.attempted or any(t.get('ticker')==tk and t.get('signalSource')=='ethlead' for t in trades): reason='already traded ethlead this window'
        elif any(t.get('ticker')==tk and t.get('mode')=='live' and t.get('status')=='open' for t in trades): reason='BTC position already open'
        elif shadow_asset('BTC'): reason='BTC shadow/cooldown'
        elif tk in LV.tried or tk in LV.resting: reason='BTC order already working/tried'
        elif any(t.get('status')=='open' and not is_shadow(t) and t.get('asset')!='BTC' and t.get('closesAt')==k['close'] and t.get('side')!=side for t in trades): reason='opposite live coin position this window'
        elif WKND_MAX>0 and wknd_night() and ba>WKND_MAX: reason='weekend price cap'
        elif ethlead.flow_against(side,flow('BTC')): reason='flow against'
        elif ev['eloP'] is None: reason='no fresh EloEdge model p'
        elif ev['eloP']-ba<need_edge(): reason='real EloEdge edge below minimum'
        if reason is None:
            q=min(ba+need_edge()+0.001,0.99); mp=q if side=='YES' else 1-q
            def size_at(px):
                if k['close']-time.time()<EL_MIN_LEFT or not (paper or LV.enabled()) or shadow_asset('BTC'): return None
                if WKND_MAX>0 and wknd_night() and px>WKND_MAX: return None
                if not (0.40<=px<=0.69): return None
                return (pt.size(side,px,mp) or [None])[0]
            sz=pt.size(side,ba,mp)
            if not sz or size_at(ba) is None: reason='normal entry price/edge filter'
        if reason:
            if not EL.events or EL.events[-1].get('ticker')!=tk or EL.events[-1].get('skipReason')!=reason:
                ev.update(status='skipped',skipReason=reason); EL.log(ev)
            return
        EL.attempted={x for x in EL.attempted if x==tk}; EL.attempted.add(tk)
        if paper or SPEC.EXPERIMENTAL_SHADOW_ONLY:
            pmeta=dict(priceSource='kalshi',signalSource='ethlead',ethlead=True,ethleadEthAsk=ea,ethleadBtcAsk=ba,ethleadGap=gap,ethleadEloP=ev.get('eloP'),ethleadSecsIn=round(900-left))
            if SPEC.EXPERIMENTAL_SHADOW_ONLY: pmeta=SPEC.experimental_force_shadow(pmeta)
            if flow_blocked('BTC',side): ev.update(status='skipped',skipReason='flow gate'); EL.log(ev); return
            with lock: trade=pt.openTrade('BTC',side,ba,mp,k['strike'],k['close'],tk,pmeta)
            ev.update(status='filled (paper)',tradeId=trade and trade['id']); EL.log(ev); return
        fill=_gbuy('BTC',tk,side,sz[0],ba,sizer=size_at,shadow=False,mode='live')
        if fill:
            meta=dict(priceSource='kalshi',signalSource='ethlead',ethlead=True,ethleadEthAsk=ea,ethleadBtcAsk=ba,ethleadGap=gap,ethleadEloP=ev.get('eloP'),ethleadSecsIn=round(900-left))
            with lock: trade=pt.openLive('BTC',side,fill,k['strike'],k['close'],tk,meta,sz[1],sz[2])
            ev.update(status='filled',tradeId=trade['id'])
        else: ev.update(status='skipped',skipReason='no fill: '+str(LV.lastBuy or 'execution guard'))
        EL.log(ev)
    except Exception as e:
        EL.last_error=type(e).__name__; print('ethlead entry error',type(e).__name__,flush=True)

last_pyth=[0]
def tick_once():
    src={}; spot={}; sigs={}; _pair_rets={}
    if time.time()-last_pyth[0]>60:
        last_pyth[0]=time.time()
        try: status['commodities']=pyth(); src['pyth']='live'
        except Exception as e: src['pyth']=f'error: {e}'
    else: src['pyth']=status['sources'].get('pyth','unknown')
    try: elo_est,elo_wins=eloedge_all()
    except Exception as e: elo_est,elo_wins='unavailable',{}; print('eloedge err',e,flush=True)
    for a in SERIES:
        sg={}
        try:
            cb=metal(a) if a in METALS else coinbase(a); sg.update(cb); spot[a]=cb['spot']; src['kraken-futures' if a in METALS else 'coinbase']='live'
            if cb.get('ret15') is not None: _pair_rets[a]=cb['ret15']
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
            try:  # log-only per-minute flow while open
                f=flow(t['asset'],1); mk_=time.strftime('%H:%M')
                if f and (not t.get('flowLog') or t['flowLog'][-1].get('m')!=mk_): t.setdefault('flowLog',[]).append(dict(f,m=mk_,bid=round(bid,3))); t['flowLog']=t['flowLog'][-16:]
            except Exception as e: print('flowlog err',e,flush=True)
            try:  # log-only: one other coin >=90% against us late, no lock armed, bid near entry (user Nov 7)
                lt=t['closesAt']-time.time()
                if not t.get('oneAgainstFlag') and 0<lt<=300 and (t.get('peakBid') or 0)<t['price']+0.10 and abs(bid-t['price'])<=0.10:
                    suf=t['ticker'].split('-',1)[1]
                    for a in ('BTC','ETH','SOL','DOGE','XRP'):
                        if a==t['asset']: continue
                        mm=json.load(urllib.request.urlopen(f'https://api.elections.kalshi.com/trade-api/v2/markets/KX{a}15M-{suf}',timeout=5))['market']
                        md=(float(mm.get('yes_bid_dollars') or 0)+float(mm.get('yes_ask_dollars') or 0))/2
                        ours=md if t['side']=='YES' else 1-md
                        if 0<ours<=0.10:
                            t.update(oneAgainstFlag=True,oneAgainstCoin=a,oneAgainstBid=round(bid,4),oneAgainstLeft=round(lt)); print('ONE-AGAINST flag',t['id'],a,bid,flush=True); break
            except Exception as e: print('oneagainst err',e,flush=True)
            with lock:
                t.update(markPrice=round(bid,4),unrealizedPnl=round(t['contracts']*bid-t['stake'],2),signalsAgree=bool(agree),
                         flipWarning=bool(sig_against or (mp is not None and (mp>0.5)!=(t['side']=='YES'))),currentModelP=q)
                if q is not None: t['currentModelPAt']=time.time()
                wnet=cb['whale']['netFlowUsd']*(1 if t['side']=='YES' else -1); left_t=t['closesAt']-time.time()
                whale_against=cb['whale']['strongDir'] not in (None,t['side'])
                t.update(whaleFlowUsd=wnet,whaleAgainst=whale_against,whaleCount=cb['whale']['count'])
                t['peakBid']=round(max(t.get('peakBid') or 0,bid),4)
                tp=None
                if bid>=tp_bid():
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
                elif PAIR_TP>0 and bid>=t['price']+PAIR_TP and any(o is not t and o['status']=='open' and not o.get('exiting') and o.get('closesAt')==t.get('closesAt') and o['side']==t['side'] and is_shadow(o)==is_shadow(t) for o in pt.trades):
                    fp=kalshi_fee(t['contracts'],bid); pt.exit(t,bid,f'paircash: up {bid-t["price"]:+.2f} with a same-side partner open; cash this one, partner rides the lock; fee ${fp:.2f}',fee=fp)
                elif lock_active() and t['peakBid']>=min(LOCK_PEAK_BY_ASSET.get(t['asset'],LOCK_PEAK), t['price']+lock_arm(t)) and bid<=t['peakBid']-lock_drop(t) and bid>t['price'] and not LH.should_hold(t,log=_lh_log) and not (q is not None and q>=LOCK_HOLD_P):
                    f2=kalshi_fee(t['contracts'],bid); _lp=min(LOCK_PEAK_BY_ASSET.get(t['asset'],LOCK_PEAK),t['price']+lock_arm(t)); pt.exit(t,bid,f'profitlock: {"night" if night() else "day"}, peak {t["peakBid"]:.3f} (armed at {_lp:.2f}, entry {t["price"]:.2f}) -> bid {bid:.3f}; EloEdge p={q if q is None else round(q,3)}; fee ${f2:.2f}',fee=f2)
                elif t['asset'] in GUARD_ASSETS and t['peakBid']>=t['price']+0.10 and bid>0 and bid<=t['price']-GUARD_GIVEBACK and not (q is not None and q>=LOCK_HOLD_P):
                    f3=kalshi_fee(t['contracts'],bid); pt.exit(t,bid,f'breakeven: {t["asset"]} peaked {t["peakBid"]:.3f} (>=entry+0.10), fell to bid {bid:.3f} vs entry {t["price"]:.3f}; EloEdge p={q if q is None else round(q,3)}; fee ${f3:.2f}',fee=f3)
                elif SPEC.is_paperish(t):
                    (do_stop,stop_why),cf=SPEC.paper_stop_action(t,bid,stop_pct(),model_q=q,ask=(m.get('yesAsk') if t['side']=='YES' else m.get('noAsk')) if isinstance(m,dict) else None,left_sec=left_t)
                    try: t['stopCounterfactual']=cf
                    except Exception: pass
                    if do_stop: pt.exit(t,bid,stop_why,fee=kalshi_fee(t['contracts'],bid))
                elif bid>0 and bid<=stop_pct()*t['price']: pt.exit(t,bid,f'stop: mark {bid:.3f} <= {stop_pct():.0%} of entry {t["price"]}')
                elif not t.get('fade') and whale_against and bid>0 and not (left_t<90 and bid>=0.85) and (q is None or bid>=q-WHALE_MAX_GIVEUP):
                    pt.exit(t,bid,f'whale: net whale flow ${wnet:,.0f} against {t["side"]}, sold at bid {bid:.3f}')
                elif not t.get('fade') and sig_against and elo_against and q is not None and bid-q>=EXIT_EDGE:
                    pt.exit(t,bid,f'flip: momentum/CVD {dirn} + {"EloEdge" if est=="live" else "fallback"} p={q:.3f} vs bid {bid:.3f}')
        if not k: sg['action']='skip: no real Kalshi market'; sigs[a]=sg; continue
        if a in METALS and (metals_closed() or metals_closed(k['close']-1)): sg['action']='skip: metals session closed (Kalshi 5-6 PM ET daily, Fri 5 PM-Sun 6 PM ET)'; sigs[a]=sg; continue
        mp=fresh(k['ticker']); sg['modelP']=mp
        left=k['close']-time.time(); sg['secsToClose']=round(left)
        if a=='BTC': leadlag_entry(k,mp)
        if a=='BTC': ethlead_entry(k,mp)
        if REV and 600<=left<=840 and not any(t.get('ticker')==k['ticker'] for t in pt.trades):
            # reversal bet (user test, Nov 7): after 3+ same-way settles, buy the other side early in the window; backtest 59% wins
            td=trend_dir(k['ticker'].split('-')[0])
            if td:
                rd='NO' if td=='YES' else 'YES'; rask=k['yesAsk'] if rd=='YES' else k['noAsk']
                if 0<rask<=REV_MAX and not any(t.get('status')=='open' and not is_shadow(t) and t.get('asset')!=a and t.get('closesAt')==k['close'] and t.get('side')!=rd for t in pt.trades):
                    fq=min(rask+need_edge()+0.001,0.99); rmp=fq if rd=='YES' else 1-fq
                    rmeta=dict(priceSource='kalshi',signalSource='reversal-after-3',fade=True,reversal=True,streak=td)
                    if shadow_asset(a): rmeta=dict(rmeta,shadow=True,mode='shadow')
                    if SPEC.EXPERIMENTAL_SHADOW_ONLY: rmeta=SPEC.experimental_force_shadow(rmeta)
                    t=None
                    if LV and LV.enabled() and not shadow_asset(a) and not SPEC.EXPERIMENTAL_SHADOW_ONLY:
                        if k['ticker'] not in LV.tried and k['ticker'] not in LV.resting and not up_tilt_skip(a,k['ticker'],rd,None,'reversal'):
                            sz=pt.size(rd,rask,rmp)
                            if sz:
                                _rs=lambda px,d=rd:None if px>REV_MAX else (pt.size(d,px,(lambda f:f if d=='YES' else 1-f)(min(px+need_edge()+0.001,0.99))) or [None])[0]  # keep min-edge size at the live price
                                fill=_gbuy(a,k['ticker'],rd,sz[0],rask,sizer=_rs,shadow=bool(rmeta.get('shadow')),mode=rmeta.get('mode','live'))
                                if fill:
                                    with lock: t=pt.openLive(a,rd,fill,k['strike'],k['close'],k['ticker'],rmeta,sz[1],sz[2])
                    else:
                        with lock: t=pt.openTrade(a,rd,rask,rmp,k['strike'],k['close'],k['ticker'],rmeta)
                    if t: sg['action']=f'OPENED REVERSAL #{t["id"]} {rd} after 3x {td}'; sigs[a]=sg; continue
        if ALT and a=='BTC' and 540<=left<=780 and not any(t.get('ticker')==k['ticker'] for t in pt.trades):
            av=alt_votes(k['ticker'])
            if len(av)==3 and len(set(av.values()))==1:
                rd=list(av.values())[0]; rask=k['yesAsk'] if rd=='YES' else k['noAsk']
                if 0<rask<=ALT_MAX and not any(t.get('status')=='open' and not is_shadow(t) and t.get('asset')!=a and t.get('closesAt')==k['close'] and t.get('side')!=rd for t in pt.trades):
                    fq=min(rask+need_edge()+0.001,0.99); rmp=fq if rd=='YES' else 1-fq
                    rmeta=dict(priceSource='kalshi',signalSource='alt-consensus',fade=True,altConsensus=True,altVotes=av)
                    if SPEC.EXPERIMENTAL_SHADOW_ONLY: rmeta=SPEC.experimental_force_shadow(rmeta)
                    t=None
                    if LV and LV.enabled() and not SPEC.EXPERIMENTAL_SHADOW_ONLY:
                        if alt_cap_allow(k['ticker']) and k['ticker'] not in LV.tried and k['ticker'] not in LV.resting and not up_tilt_skip(a,k['ticker'],rd,None,'alt-consensus'):
                            sz=pt.size(rd,rask,rmp)
                            if sz:
                                _as=lambda px,d=rd:None if px>ALT_MAX else (pt.size(d,px,(lambda f:f if d=='YES' else 1-f)(min(px+need_edge()+0.001,0.99))) or [None])[0]  # keep min-edge size at the live price
                                fill=_gbuy(a,k['ticker'],rd,sz[0],rask,sizer=_as,shadow=bool(rmeta.get('shadow')),mode=rmeta.get('mode','live'))
                                if fill:
                                    with lock: t=pt.openLive(a,rd,fill,k['strike'],k['close'],k['ticker'],rmeta,sz[1],sz[2])
                    else:
                        with lock: t=pt.openTrade(a,rd,rask,rmp,k['strike'],k['close'],k['ticker'],rmeta)
                    if t: sg['action']=f'OPENED ALT-CONSENSUS #{t["id"]} {rd} (SOL/DOGE/XRP all {rd})'; sigs[a]=sg; continue
        reok=False
        try: reok=(bool(LV and LV.enabled()) and any(t.get('ticker')==k['ticker'] for t in pt.trades) and reentry_ok(a,k,dirn,cb,left)) or False
        except Exception as e: print('reentry err',e,flush=True)
        if PAPER_LOOSE and not dirn and not (LV and LV.enabled()) and mp is not None and abs(mp-0.5)>=0.05: dirn='YES' if mp>0.5 else 'NO'
        if not dirn: sg['action']='skip: momentum/CVD disagree'
        elif est=='live' and mp is None and a!='XRP': sg['action']='skip: no fresh EloEdge model_p'
        elif mp is not None and (mp>0.5)!=(dirn=='YES'): sg['action']=f'skip: EloEdge disagrees (model_p {mp})'
        elif cb['whale']['strongDir'] not in (None,dirn): sg['action']=f"skip: strong whale flow against (${cb['whale']['netFlowUsd']:,.0f})"
        elif left<60: sg['action']='skip: <60s to close'
        elif any(t.get('ticker')==k['ticker'] for t in pt.trades) and not reok: sg['action']='holding/traded this window'
        else:
            used='coinbase+eloedge'
            if mp is None: mp=heur(cb); used='coinbase-proxy (XRP shadow, no EloEdge)' if a=='XRP' else 'coinbase-fallback (EloEdge unavailable)'
            ask=k['yesAsk'] if dirn=='YES' else k['noAsk']
            meta=dict(priceSource='kalshi',signalSource=used,whaleFlowAtEntry=cb['whale']['netFlowUsd'],mom=mom,cvd=cvd,eloedge=est,_bid=(k['yesBid'] if dirn=='YES' else k['noBid']))
            q=mp if dirn=='YES' else 1-mp
            shd=shadow_asset(a)
            if reok and not shd:
                meta=dict(meta,reentry=True,reentryAfter=reok if reok in ('lock','stop') else 'lock',signalSource=used+'+reentry'+('-stop' if reok=='stop' else '')); LV.tried.discard(k['ticker'])
            if shd: meta=dict(meta,shadow=True,mode='shadow',shadowTag=f'{a} shadow')
            if FADE and WKND_MAX_EDGE>0 and wknd_night() and q-ask>WKND_MAX_EDGE:
                # fade test: weekend-night 20c+ edges went 0/4, so buy the other side, sized as a minimum-edge bet
                oe=round(q-ask,3); dirn='NO' if dirn=='YES' else 'YES'; ask=k['yesAsk'] if dirn=='YES' else k['noAsk']
                fq=min(ask+WKND_EDGE+0.001,0.99); mp=fq if dirn=='YES' else 1-fq; q=fq
                meta=dict(meta,fade=True,fadeOrigEdge=oe)
            td=trend_dir(k['ticker'].split('-')[0]) if TREND_N>0 else None
            if td and td!=dirn: meta=dict(meta,againstTrend=td)  # log-only: data showed 3-in-a-row reverses ~59%
            if any(t.get('status')=='open' and is_shadow(t)==shd and t.get('asset')!=a and t.get('closesAt')==k['close'] and t.get('side')!=dirn for t in pt.trades):
                sg['action']='skip: opposite side of another coin open this window (BTC/ETH settle together ~84%)'; sigs[a]=sg; continue
            if WKND_MAX>0 and wknd_night() and ask>WKND_MAX: sg['action']=f'skip: weekend-night price cap ({ask:.2f} > {WKND_MAX:.2f})'; sigs[a]=sg; continue
            if LV and LV.enabled() and not shd:
                if up_tilt_skip(a,k['ticker'],dirn,q-ask,'reentry' if meta.get('reentry') else 'eloedge'):
                    sg['action']=f'skip: up-tilt (weekend-night Up edge {q-ask:+.3f} < {need_edge()+uptilt.UP_TILT:.2f})'; sigs[a]=sg; continue
                _bid=k['yesBid'] if dirn=='YES' else k['noBid']
                sz=pt.size(dirn,ask,mp,_bid); t=None
                if sz:
                    if k['ticker'] in LV.tried or k['ticker'] in LV.resting: sg['action']='live: order working / already tried this window'; sigs[a]=sg; continue
                    _sz=lambda px,d=dirn,m=mp,b=_bid:None if (WKND_MAX>0 and wknd_night() and px>WKND_MAX) or uptilt.blocked(d,(m if d=='YES' else 1-m)-px,need_edge(),wknd_night()) else (pt.size(d,px,m,b) or [None])[0]
                    LV.lastBuy=None
                    fill=_gbuy(a,k['ticker'],dirn,sz[0],ask,sizer=_sz,shadow=bool(meta.get('shadow')),mode=meta.get('mode','live'))
                    if fill:
                        with lock: t=pt.openLive(a,dirn,fill,k['strike'],k['close'],k['ticker'],meta,sz[1],sz[2])
                    elif LV.lastBuy in ('edge','nofill') and REST_SEC>0 and left>REST_SEC+60:
                        # missed at the live price: rest a limit at the signal price for REST_SEC, like XIII did
                        def _rest(a=a,tk=k['ticker'],d=dirn,ref=ask,szf=_sz,st=k['strike'],cl=k['close'],meta=meta,e=sz[1],q2=sz[2]):
                            f=LV.rest_buy(a,tk,d,ref,szf,wait=REST_SEC,close_at=cl-30,shadow=bool(meta.get('shadow')),mode=meta.get('mode','live'))
                            if f:
                                with lock: pt.openLive(a,d,f,st,cl,tk,meta,e,q2)
                        threading.Thread(target=_rest,daemon=True).start(); sg['action']=f'live: resting limit @{ask:.2f} for {REST_SEC}s'; sigs[a]=sg; continue
                sg['action']=f'OPENED LIVE #{t["id"]}' if t else ('live: no fill / edge gone at live price (see live order log)' if sz else (f'skip: weekend-night edge too big ({q-ask:+.3f} > {WKND_MAX_EDGE})' if WKND_MAX_EDGE>0 and wknd_night() and q-ask>WKND_MAX_EDGE else f'skip: edge {q-ask:+.3f} < {need_edge()}'))
            else:
                with lock: t=pt.openTrade(a,dirn,ask,mp,k['strike'],k['close'],k['ticker'],meta)
                sg['action']=(f'OPENED {a} SHADOW #{t["id"]} (paper only)' if shd else f'OPENED #{t["id"]}') if t else (f'skip: weekend-night edge too big ({q-ask:+.3f} > {WKND_MAX_EDGE})' if WKND_MAX_EDGE>0 and wknd_night() and q-ask>WKND_MAX_EDGE else f'skip: edge {q-ask:+.3f} < {need_edge()}')
        sigs[a]=sg
    try: SPEC.log_metal_gap((sigs.get('GOLD') or {}).get('kalshi'),(sigs.get('SILVER') or {}).get('kalshi'))  # research only
    except Exception: pass
    _gm={x:(sigs.get(x) or {}).get('kalshi') for x in GAPS_ASSETS}
    try: _gm['DOGE']=_doge_mk()  # research/shadow confirmation only; DOGE is NOT in SERIES, never traded by Spec/live
    except Exception: _gm['DOGE']=None
    try: SPEC.log_crypto_gap({x:_gm.get(x) for x in ('BTC','ETH','SOL','XRP','DOGE')})  # research only
    except Exception: pass
    try:  # pairing-gap SHADOW only (separate book, never live, never pt.trades)
        GAPS.on_tick(_gm); GAPS.settle(_pl_result)
    except Exception as e: print('gapshadow tick err',e,flush=True)
    try:
        if 'DOGE' not in _pair_rets:
            try:
                dc=get(f'{CB}/products/DOGE-USD/candles?granularity=900')
                if dc and len(dc)>1 and dc[1][3]: _pair_rets['DOGE']=round((dc[1][4]-dc[1][3])/dc[1][3],6)
            except Exception: pass
        SPEC.update_pair_returns({a:[[0,0,0,1,1+(_pair_rets[a] or 0),0],[0,0,0,1,1+(_pair_rets[a] or 0),0]] for a in _pair_rets})
        # update_pair_returns expects candle lists; bypass via direct cache write for snapshotted rets
        SPEC._ret_cache.update(at=time.time(),rets=dict(_pair_rets),detail=SPEC.corr_regime(_pair_rets))
        SPEC._ret_cache['tag']=SPEC._ret_cache['detail'].get('corrRegime'); SPEC._ret_cache['corr']=SPEC._ret_cache['detail'].get('signAgree')
    except Exception: pass
    try:
        for a,sg in list(sigs.items()):
            k=sg.get('kalshi') or {}
            side=sg.get('direction') if sg.get('direction') in('YES','NO') else None
            mp=sg.get('modelP'); ask=None; bid=None; q=None
            if side and k:
                ask=k.get('yesAsk') if side=='YES' else k.get('noAsk')
                bid=k.get('yesBid') if side=='YES' else k.get('noBid')
                if mp is not None: q=mp if side=='YES' else 1-mp
            SPEC.append_research(SPEC.decision_row(asset=a,ticker=k.get('ticker') if isinstance(k,dict) else None,side=side,
                model_p=mp,source=sg.get('signalSource') or sg.get('action'),yes_bid=(k or {}).get('yesBid'),yes_ask=(k or {}).get('yesAsk'),
                no_bid=(k or {}).get('noBid'),no_ask=(k or {}).get('noAsk'),q=q if q is not None else mp,ask=ask,
                bid=bid,action=sg.get('action'),skip_reason=None if (sg.get('action') or '').startswith('OPENED') else sg.get('action'),
                cb=sg,extra=dict(secsToClose=sg.get('secsToClose'))),DATA)
    except Exception: pass
    try: PL.observe(sigs,'live' if (LV and LV.enabled()) else 'paper')  # read-only prediction log; cannot raise
    except Exception: pass
    with lock: pt.settle(spot)
    status['whaleAlerts']=sorted([dict(asset=a,**w) for a,g in sigs.items() for w in (g.get('whale') or {}).get('alerts',[])],key=lambda w:w['ago'])[:12]
    fb=src.get('eloedge')!='live'
    status.update(metalsClosed=metals_closed(),nightMode=night(),sources=src,signals=sigs,lastTick=time.time(),eloedge=src.get('eloedge','unknown'),fallbackInUse=fb,
        signalMode='FALLBACK: Coinbase momentum+CVD only (EloEdge unavailable)' if fb else 'Coinbase momentum+CVD + EloEdge model_p')
TICK_TTL_DAYS=5; TICK_MAX_MB=350; TICK_FLUSH_SEC=2; TICK_BATCH=500; TICK_MAXQ=20000
import collections
_tq=collections.deque(maxlen=TICK_MAXQ); _tlast={}
TICKS=dict(enabled=False,recorded=0,dropped=0,deduped=0,flushErrors=0,lastFlush=None,lastError=None,count=None,sizeMB=None,storageMB=None,paused=False,ttlDays=TICK_TTL_DAYS,maxMB=TICK_MAX_MB,ratePerMin=None)
TCOL=MCOL.database['ticks'] if MCOL is not None else None
import windowlog as WL
import truthfix as TFX
TFX.SANE=pnl_bounds_ok
# Owner #3 fix: permanent append-only order log (every order/cancel/skip event) and one-shot Kalshi-record P&L truth fix.
if LV is not None and MCOL is not None:
    try: LV.orderlog=MCOL.database['orderlog']
    except Exception as _e: print('orderlog err',_e,flush=True)
if LV is not None:
    if TRUTHFIX_AUTO:
     try: TFX.start(pt,lock,LV,_live,MCOL.database['truthfix'] if MCOL is not None else None)
     except Exception as _e: print('truthfix start err',_e,flush=True)
try: WL.start(MCOL.database if MCOL is not None else None,owner=lambda:(LV is None or LV.lease_ok is None or LV.owner()))
except Exception as _e: print('windowlog start err',_e,flush=True)
import predlog as PL
import gapshadow as GAPS
GAPS_ASSETS=('GOLD','SILVER','BTC','ETH','SOL','XRP')
try: GAPS.load(DATA)
except Exception as e: print('gapshadow load err',e,flush=True)
# ---- Nov 9: persist in-memory-only state to the existing Mongo (async/batched, owner-gated, bounded) + reload on boot ----
import gappersist as GP
SEED_DIR=os.path.join(APP,'gap_seed')
def _gp_owner(): return LV is None or LV.lease_ok is None or LV.owner()
def _seed(name):
    try:
        with open(os.path.join(SEED_DIR,name)) as f: return json.load(f)
    except Exception: return []
def _log_id(r):
    k=r.get('kind')
    if k=='metal_gap': return 'metalgap','m|%d'%int(r['ts_ms'])
    if k=='crypto_gap': return 'cryptogap','c|%d'%int(r['ts_ms'])
    return 'spec_research','s|%d|%s|%s'%(int(r.get('ts_ms') or 0),r.get('ticker'),r.get('tradeId'))
def _log_sink(r):
    col,_id=_log_id(r); GP.insert(col,_id,r)
def _persist_boot():
    GP.start(MCOL.database if MCOL is not None else None,owner=_gp_owner)
    # gap shadow + gap paper trades: file (already loaded) + Mongo + committed seed (one-time import, idempotent)
    mdocs=GP.load_all('gap_trades'); mk_={GAPS.doc_id(d):d for d in mdocs}
    GAPS.merge_in(mdocs); GAPS.merge_in(_seed('gapshadow.json'))
    n=0
    for t in GAPS.snapshot()+GAPS.paper_snapshot():
        o=mk_.get(GAPS.doc_id(t))
        if o is None or GAPS._rank(t)>GAPS._rank(o): GP.upsert('gap_trades',GAPS.doc_id(t),t); n+=1
    GP.ST['seeded']['gap_trades']=n if GP.enabled() else 0
    GAPS.SINK[0]=lambda t:GP.upsert('gap_trades',GAPS.doc_id(t),t)
    # tick logs: newest ring from Mongo + seed rows (within TTL); seed rows are re-inserted idempotently (_id = kind|ts_ms)
    cut=(time.time()-GP.LOG_TTL_DAYS*86400)*1000
    try:
        import hashlib; _sha=hashlib.sha256(b''.join(open(os.path.join(SEED_DIR,f),'rb').read() for f in ('metalgap.json','cryptogap.json'))).hexdigest()[:16]
    except Exception: _sha=None
    _mk=GP.load_doc('research_state','seed_import') if GP.enabled() else None
    _need=bool(_sha) and GP.enabled() and not (_mk and _mk.get('sha')==_sha)
    for dq,col,fn in ((SPEC.METAL_GAP,'metalgap','metalgap.json'),(SPEC.CRYPTO_GAP,'cryptogap','cryptogap.json')):
        GP.merge_rows(dq,GP.load_recent(col,dq.maxlen or 3000))
        sd=[r for r in _seed(fn) if isinstance(r,dict) and (r.get('ts_ms') or 0)>=cut]
        add=GP.merge_rows(dq,sd)
        if _need:
            for r in sd: _log_sink(r)   # one-time import; _id=kind|ts_ms so a repeat is a harmless duplicate-key no-op
        GP.ST['seeded'][col]=len(sd) if _need else 0
    if _need: GP.upsert('research_state','seed_import',dict(sha=_sha,at=time.time()))
    SPEC.LOG_SINK[0]=_log_sink
    print('persist boot: gap trades',len(GAPS.TRADES),'shadow /',len(GAPS.PAPER),'paper; metalgap',len(SPEC.METAL_GAP),'cryptogap',len(SPEC.CRYPTO_GAP),'mongo',GP.enabled(),flush=True)
try: _persist_boot()
except Exception as e: print('persist boot err',e,flush=True)
# gap PAPER fills re-check the fresh websocket quote (same as Spec paper PAPER_FRESH_FILL); read-only quote cache
GAPS.QUOTE[0]=lambda tk:(LV.wsq(tk) if LV is not None else None)
# research trigger logs (leadlag / ethlead): capped snapshot doc each, reloaded on boot (display/research only)
def _rs_reload(name,dq):
    try:
        d=GP.load_doc('research_state',name)
        if d and isinstance(d.get('items'),list) and not dq: dq.extend(d['items'][-(dq.maxlen or 200):])
    except Exception as e: print('research_state reload err',name,e,flush=True)
try:
    _rs_reload('leadlag_events',LL.events); _rs_reload('ethlead_events',EL.events)
    GP.register_snapshot('leadlag_events',lambda:[dict(e) for e in list(LL.events)])
    GP.register_snapshot('ethlead_events',lambda:[dict(e) for e in list(EL.events)])
except Exception as e: print('research_state err',e,flush=True)
# open paper positions' lock/stop state (peakBid etc.) is mutated on websocket quotes without a save; flush it to the
# existing paper_desk_state doc at most once a minute when it changed (background, never in the trading path)
def _peak_sig():
    return tuple((t.get('id'),t.get('peakBid'),t.get('stopCounterfactual') is not None) for t in list(pt.trades) if t.get('status')=='open')
def _peak_flusher():
    last=None
    while True:
        time.sleep(60)
        try:
            sg=_peak_sig()
            if sg and sg!=last: _dirty.set()
            last=sg
        except Exception as e: print('peak flush err',e,flush=True)
if MCOL is not None: threading.Thread(target=_peak_flusher,daemon=True).start()
GAP_DOGE=os.environ.get('GAP_DOGE','1')!='0'
DOGE_SERIES='KXDOGE15M'
def _doge_mk():
    """Public Kalshi KXDOGE15M market for gap-shadow confirmation/logging only (not in SERIES)."""
    if not GAP_DOGE: return None
    ms=[m for m in get(f"{KALSHI}/markets?series_ticker={DOGE_SERIES}&status=open&limit=5")['markets'] if iso(m['close_time'])>time.time()]
    return mk(sorted(ms,key=lambda m:m['close_time'])[0]) if ms else None
def _pl_result(tk):
    try: return (get(f"{KALSHI}/markets/{tk}")['market'].get('result') or None)
    except Exception: return None
try: PL.start(MCOL.database if MCOL is not None else None,regime=lambda a:regime(a),trend=lambda s:trend_dir(s),result=_pl_result,owner=lambda:(LV is None or LV.lease_ok is None or LV.owner()))
except Exception as _e: print('predlog start err',_e,flush=True)
def tick_rec(tk,q):
    if TCOL is None or TICKS['paused']: return
    row=(q.get('yesBid'),q.get('yesAsk'),q.get('noBid'),q.get('noAsk'),q.get('last'))
    if _tlast.get(tk)==row: TICKS['deduped']+=1; return
    _tlast[tk]=row
    if len(_tq)>=TICK_MAXQ: TICKS['dropped']+=1
    ts=q.get('ts') or q.get('rx') or time.time()
    _tq.append(dict(t=tk,ms=int(ts*1000),yb=row[0],ya=row[1],nb=row[2],na=row[3],lp=row[4]))
def _tick_writer():
    from datetime import timezone as _tz
    try:
        TCOL.create_index([('t',1),('ms',1)]); TCOL.create_index('ms')
        try: TCOL.create_index('at',expireAfterSeconds=TICK_TTL_DAYS*86400)
        except Exception: MCOL.database.command('collMod','ticks',index={'keyPattern':{'at':1},'expireAfterSeconds':TICK_TTL_DAYS*86400})
        TICKS['ttlActual']=[i.get('expireAfterSeconds') for i in TCOL.list_indexes() if i.get('key',{}).get('at')]
        TICKS['enabled']=True
    except Exception as e: TICKS['lastError']=f'index: {e}'
    last_stat=0; win=collections.deque()
    while True:
        time.sleep(TICK_FLUSH_SEC if len(_tq)<TICK_BATCH else 0.2)
        if LV is not None and LV.lease_ok is not None and not LV.owner(): _tq.clear(); continue
        rows=[]
        while _tq and len(rows)<TICK_BATCH*4: rows.append(_tq.popleft())
        if rows:
            now=datetime.now(_tz.utc)
            for x in rows: x['at']=now
            try:
                TCOL.with_options(write_concern=__import__('pymongo').WriteConcern(w=1,wtimeout=3000)).insert_many(rows,ordered=False)
                TICKS['recorded']+=len(rows); TICKS['lastFlush']=time.time(); win.append((time.time(),len(rows)))
            except Exception as e: TICKS['flushErrors']+=1; TICKS['dropped']+=len(rows); TICKS['lastError']=f'{type(e).__name__}: {str(e)[:160]}'
        while win and time.time()-win[0][0]>600: win.popleft()
        TICKS['ratePerMin']=round(sum(n for _,n in win)/10,1) if win else 0
        if time.time()-last_stat>120:
            last_stat=time.time()
            try:
                st=MCOL.database.command('collStats','ticks'); TICKS.update(count=st.get('count'),sizeMB=round(st.get('size',0)/1e6,2),storageMB=round((st.get('storageSize',0)+st.get('totalIndexSize',0))/1e6,2))
                TICKS['paused']=TICKS['storageMB']>=TICK_MAX_MB
            except Exception as e: TICKS['lastError']=f'stats: {e}'
if TCOL is not None: threading.Thread(target=_tick_writer,daemon=True).start()
def _lh_log(defer=True,**kw):
    if LV is not None and not kw.get('shadow'): LV.log(defer=defer,**kw)
def _ws_quote(tk,q):
    try: LL.observe(tk,q)
    except Exception: pass
    try: tick_rec(tk,q)
    except Exception: pass
    if not (LV and LV.allowed()): return
    ts=[t for t in list(pt.trades) if not t.get('shadow') and (t.get('mode')=='live' or PAPER_WS_EXITS) and t.get('status')=='open' and t.get('ticker')==tk and not t.get('exiting')]
    if not ts or not LV.owner(): return
    if not lock.acquire(timeout=0.2): return
    try:
        now=time.time()
        for t in ts:
            if t['status']!='open' or t.get('exiting') or now>=t['closesAt'] or now-(t.get('liveSellAt') or 0)<3: continue
            yb,ya=q.get('yesBid'),q.get('yesAsk')
            bid=yb if t['side']=='YES' else (None if ya is None else round(1-ya,4))
            if bid is None: continue
            t['peakBid']=round(max(t.get('peakBid') or 0,bid),4); mp=t.get('currentModelP')
            if lock_active() and t['peakBid']>=min(LOCK_PEAK_BY_ASSET.get(t['asset'],LOCK_PEAK),t['price']+lock_arm(t)) and bid<=t['peakBid']-lock_drop(t) and bid>t['price'] and not LH.should_hold(t,log=_lh_log) and not (mp is not None and mp>=LOCK_HOLD_P):
                f2=kalshi_fee(t['contracts'],bid); pt.exit(t,bid,f'profitlock: ws quote, peak {t["peakBid"]:.3f} -> bid {bid:.3f}; fee ${f2:.2f}',fee=f2)
            elif SPEC.is_paperish(t):
                (do_stop,stop_why),cf=SPEC.paper_stop_action(t,bid,stop_pct(),model_q=mp,ask=None,left_sec=t['closesAt']-now)
                try: t['stopCounterfactual']=cf
                except Exception: pass
                if do_stop: pt.exit(t,bid,stop_why,fee=kalshi_fee(t['contracts'],bid))
            elif bid>0 and bid<=stop_pct()*t['price']:
                pt.exit(t,bid,f'stop: ws mark {bid:.3f} <= {stop_pct():.0%} of entry {t["price"]}',fee=0 if t.get('mode')=='live' else kalshi_fee(t['contracts'],bid))
    finally: lock.release()
if LV is not None and getattr(LV,'ws',None) is not None: LV.ws.on_quote=_ws_quote
REENTRY_WAIT=60; REENTRY_MIN_LEFT=120
STOP_REENTRY_ENABLED=True
STOP_REENTRY_WAIT=60
STOP_REENTRY_MIN_LEFT=120
STOP_REENTRY_BAND=(0.40,0.69)
STOP_REENTRY_RECOVER=0.05
STOP_REENTRY_FLOW_SEC=60
def _side_trade_prices(tk,side,since,feed=None):
    # our-side prices and taker counts from the kws trade feed (in-memory only)
    if feed is None:
        lv=globals().get('LV'); feed=getattr(lv,'ws',None) if lv is not None else None
    out=[]
    if feed is None: return out
    try: tr=list(feed.trades)
    except Exception: return out
    for x in tr:
        if x.get('market_ticker')!=tk or (x.get('ts_ms') or 0)<since*1000: continue
        px=x.get('yes_price_dollars') if side=='YES' else x.get('no_price_dollars')
        out.append(dict(px=px,taker=x.get('taker_side'),cnt=x.get('count_fp') or 0,ts=x.get('ts_ms')))
    return out
def stop_reentry_confirm(t,k,dirn,cb,now,feed=None):
    # live confirmation the move flipped back our way after a stop
    up=dirn=='YES'; ask=k.get('yesAsk') if up else k.get('noAsk'); bid=k.get('yesBid') if up else k.get('noBid')
    if ask is None or not (STOP_REENTRY_BAND[0]<=ask<=STOP_REENTRY_BAND[1]): return False
    mom=cb.get('momentum') or 0
    if not (mom>0 if up else mom<0): return False
    tr=_side_trade_prices(k.get('ticker'),dirn,t.get('closedAt') or now,feed)
    lows=[x['px'] for x in tr if x['px'] is not None]
    if t.get('exitPrice') is not None: lows.append(t['exitPrice'])
    if not lows: return False
    low=min(lows); cur=bid if bid is not None else ask-0.01
    if cur-low<STOP_REENTRY_RECOVER-1e-9: return False
    rec=[x for x in tr if (x['ts'] or 0)>=(now-STOP_REENTRY_FLOW_SEC)*1000]
    ours=sum(x['cnt'] for x in rec if x['taker']==dirn.lower()); theirs=sum(x['cnt'] for x in rec if x['taker'] in ('yes','no') and x['taker']!=dirn.lower())
    if rec and ours<theirs: return False
    return True
def reentry_ok(a,k,dirn,cb,left,now=None,feed=None):
    # one live re-buy per window, same side, after a profit-lock exit ('lock') or a stop with live confirmation ('stop')
    now=now or time.time()
    if not dirn or left<REENTRY_MIN_LEFT or shadow_asset(a) or 'XRP' in str(k.get('ticker')): return False
    ts=[t for t in pt.trades if t.get('ticker')==k['ticker'] and not t.get('shadow')]
    if not ts or any(t.get('status')=='open' or t.get('reentry') or t.get('exiting') for t in ts): return False
    lv=[t for t in ts if t.get('mode')=='live']
    if len(lv)!=1 or len(ts)!=1: return False
    t=lv[0]
    if t.get('status')!='exited' or t.get('side')!=dirn: return False
    why=str(t.get('exitReason','')).lower()
    if why.startswith('stop'):
        if not STOP_REENTRY_ENABLED or left<STOP_REENTRY_MIN_LEFT or now-(t.get('closedAt') or now)<STOP_REENTRY_WAIT: return False
        return 'stop' if stop_reentry_confirm(t,k,dirn,cb,now,feed) else False
    if not why.startswith('profitlock'): return False
    if now-(t.get('closedAt') or now)<REENTRY_WAIT: return False
    up=dirn=='YES'; st=k.get('strike'); sp=cb.get('spot'); mom=cb.get('momentum') or 0
    side_ok=(st is not None and sp is not None and (sp>st if up else sp<st)) or (mom>0 if up else mom<0)
    return 'lock' if side_ok else False
def loop():
    while True:
        if LV is not None and LV.lease_ok is not None and not LV.owner(): time.sleep(5); continue  # newer instance owns trading; stale one idles
        try: tick_once()
        except Exception as e: print('tick err',e,flush=True)
        time.sleep(LIVE_TICK if any(t.get('mode')=='live' and t['status']=='open' for t in pt.trades) else TICK)  # faster checks while a real position is open
STATUS_TRADES=50
STATUS_ORDERS=50
STATUS_TRIGGERS=20
STATUS_DROP_KEYS=('flowLog',)
def _newest_first(v):
    a,b=v[0],v[-1]
    ta=(a.get('at') or a.get('openedAt') or a.get('ts') or 0) if isinstance(a,dict) else 0
    tb=(b.get('at') or b.get('openedAt') or b.get('ts') or 0) if isinstance(b,dict) else 0
    return ta>tb
def slim_status(d):
    tr=d.get('trades') or []
    d['tradesTotal']=len(tr)
    if len(tr)>STATUS_TRADES:
        keep=[t for t in tr if t.get('status')=='open']
        keep+= [t for t in tr if t.get('status')!='open'][:STATUS_TRADES]
        d['trades']=sorted(keep,key=lambda t:-(t.get('openedAt') or 0))
        d['tradesTrimmed']=True
    d['trades']=[{k:v for k,v in t.items() if k not in STATUS_DROP_KEYS} for t in d.get('trades') or []]
    lv=d.get('live') or {}
    if isinstance(lv.get('recentOrders'),list): lv['recentOrders']=lv['recentOrders'][:STATUS_ORDERS]
    for sec in ('leadlag','ethlead','reentries','altCap','tradeFlow','reconcile'):
        x=d.get(sec)
        if not isinstance(x,dict): continue
        for k,v in list(x.items()):
            if isinstance(v,list) and len(v)>STATUS_TRIGGERS:
                x[k+'Total']=len(v); x[k]=v[:STATUS_TRIGGERS] if _newest_first(v) else v[-STATUS_TRIGGERS:]
    return d
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
                _lt=[t for t in pt.trades if t.get('mode')=='live' and not t.get('shadow') and t.get('status')!='open']; d['livePnlSinceStart']=round(sum(t.get('pnl') or 0 for t in _lt),2); d['liveStartAt']=min((t['openedAt'] for t in _lt),default=None)
                d['netDeposits']=round(d['liveCash']-d['livePnlSinceStart'],2) if d.get('liveCash') is not None else None
                if live_on and lv.get('cash') is not None: d['balance']=lv['cash']
                d['ticks']=dict(TICKS,queued=len(_tq))
                d['leadlag']=LL.snapshot(pt.trades)
                d['ethlead']=EL.snapshot(pt.trades)
                d['lockHold']=LH.summary(pt.trades)
                try: d['tradeFlow']=LV.ws.trade_flow() if (LV is not None and getattr(LV,'ws',None) is not None) else dict(enabled=False)
                except Exception as e: d['tradeFlow']=dict(error=str(e)[:200])
                d['reconcile']=dict(RECON.state)
                d['deployment']='looser-lock-fast-stop-alt-cap-v3-tradefeed'
                d['profitLock']=dict(weekend=wknd_night(),mode=WKND_MODE,upArmC=round(WKND_UP_ARM*100),upDropC=round(WKND_UP_DROP*100),downArmC=round(WKND_LOCK_ARM_C*100),downDropC=round(WKND_DROP*100),fixedPeakC=round(LOCK_PEAK*100))
                d['altCap']=dict(alt_cap_state(pt.trades),enabled=ALT,capEnabled=ALT_CAP_ENABLED,recentSkips=list(_alt_cap_skips))
                if not ALT_CAP_ENABLED: d['altCap']['blocked']=False; d['altCap']['reason']=None; d['altCap']['blockedUntil']=None
                d['stopExecution']=dict(limitAttempts=1,confirmation='single immediate order-status poll',confirmationWaitSec=0,aggressiveLimitC=1,preflightPosition=False)
                _re=[t for t in pt.trades if t.get('reentry')]; _rc=[t for t in _re if t.get('status')!='open']
                d['reentries']=dict(count=len(_re),open=len(_re)-len(_rc),closed=len(_rc),wins=len([t for t in _rc if (t.get('pnl') or 0)>0]),pnl=round(sum(t.get('pnl') or 0 for t in _rc),2))
                for _ty in ('lock','stop'):
                    _r2=[t for t in _re if (t.get('reentryAfter') or 'lock')==_ty]; _c2=[t for t in _r2 if t.get('status')!='open']
                    d['reentries'][_ty]=dict(count=len(_r2),open=len(_r2)-len(_c2),closed=len(_c2),wins=len([t for t in _c2 if (t.get('pnl') or 0)>0]),pnl=round(sum(t.get('pnl') or 0 for t in _c2),2))
                d['reentries']['stopReentry']=dict(enabled=STOP_REENTRY_ENABLED,waitSec=STOP_REENTRY_WAIT,minLeftSec=STOP_REENTRY_MIN_LEFT,band=list(STOP_REENTRY_BAND),recoverC=round(STOP_REENTRY_RECOVER*100),flowSec=STOP_REENTRY_FLOW_SEC)
                try:
                    d['live']['cooldowns']=cooldowns()
                    d['live']['cooldownsEnabled']=COOLDOWNS_ENABLED
                    d['live']['lossStreaks']=dict(_cool.get('streaks',{}))
                except Exception as e: d['live']['cooldowns']={'error':str(e)}
                try:  # gap PAPER book shown in the paper ledger (display copies only; Spec stats/balance above exclude it)
                    d['gapPaper']=GAPS.paper_summary(); d['persistence_gap']=GP.stats()
                    d['trades']=sorted(list(d.get('trades') or [])+[dict(g,exitReason=f"gap {g.get('rule')} {g.get('pair')}") for g in GAPS.paper_snapshot()],key=lambda t:-(t.get('openedAt') or 0))
                except Exception as e: d['gapPaper']=dict(error=str(e)[:200])
                if 'full=1' not in s.path: slim_status(d)
                b=json.dumps(d).encode()
            s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.send_header('Access-Control-Allow-Origin','*'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/trades.csv':
            cols=['id','mode','opened ET','closed ET','asset','ticker','side','entry','stake','stakePct','edge','signalSource','status','exitPrice','exitReason','settle','pnl','hold_pnl','kalshi_pnl','pnl_verified','tag']
            et=lambda x:datetime.fromtimestamp(x,ET).strftime('%Y-%m-%d %H:%M:%S') if x else ''
            buf=io.StringIO(); w=csv.writer(buf); w.writerow(cols)
            with lock: tr=list(pt.trades)
            for t in tr: w.writerow([t.get('id'),t.get('mode','paper'),et(t.get('openedAt')),et(t.get('closedAt')),t.get('asset'),t.get('ticker',''),t.get('side'),t.get('price'),t.get('stake'),t.get('stakePct',''),t.get('edge'),t.get('signalSource',''),t.get('status'),t.get('exitPrice',''),t.get('exitReason',''),t.get('settle',''),t.get('pnl',''),t.get('hold_pnl',''),t.get('kalshiPnl',''),('kalshi' if 'kalshiPnl' in t else ''),'bot'])
            try:
                for g in GAPS.snapshot(): w.writerow([g.get('id'),'shadow',et(g.get('openedAt')),et(g.get('closedAt')),g.get('laggard'),g.get('ticker',''),g.get('side'),g.get('entry'),g.get('stake'),0,g.get('gapDown'),'gapshadow:'+str(g.get('rule','gap20'))+':'+str(g.get('pair','')),g.get('status'),g.get('exit') if g.get('exit') is not None else '','',g.get('settle') or '',g.get('pnl') if g.get('pnl') is not None else '',g.get('hold_pnl') if g.get('hold_pnl') is not None else '','','','gapshadow'])
            except Exception: pass
            try:  # gap PAPER book (Rule A BTC-ETH / SOL-XRP): real paper trades, never live, separate from Spec balance
                for g in GAPS.paper_snapshot(): w.writerow([g.get('id'),'paper',et(g.get('openedAt')),et(g.get('closedAt')),g.get('asset'),g.get('ticker',''),g.get('side'),g.get('price'),g.get('stake'),0,g.get('gapDown'),'gap:'+str(g.get('rule'))+':'+str(g.get('pair','')),g.get('status'),g.get('exit') if g.get('exit') is not None else '','',g.get('settle') or '',g.get('pnl') if g.get('pnl') is not None else '',g.get('hold_pnl') if g.get('hold_pnl') is not None else '','','','gap-paper'])
            except Exception: pass
            for x in (TFX.ST.get('manual') or {}).get('tickers',[]): w.writerow(['','live','','',x['ticker'].split('15M')[0].removeprefix('KX'),x['ticker'],'','','','','','manual (not bot)','settled','','','',x['pnl'],'',x['pnl'],'kalshi','manual'])
            b=buf.getvalue().encode(); s.send_response(200); s.send_header('Content-Type','text/csv; charset=utf-8')
            s.send_header('Content-Disposition',f'attachment; filename="paper-desk-trades-{datetime.now(ET).strftime("%Y%m%d-%H%M")}.csv"'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/kxtrades':
            from urllib.parse import parse_qs
            q=parse_qs(s.path.split('?',1)[1] if '?' in s.path else '')
            try: after=int(float((q.get('after') or ['0'])[0]))
            except Exception: after=0
            try: lim=max(1,min(20000,int((q.get('limit') or ['5000'])[0])))
            except Exception: lim=5000
            ws=getattr(LV,'ws',None) if LV is not None else None
            rows=ws.trades_after(after,lim) if ws is not None else []
            b=json.dumps(dict(trades=rows,count=len(rows),next=rows[-1]['ts_ms'] if rows else after,bufferSize=len(ws.trades) if ws is not None else 0)).encode()
            s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/ticks':
            from urllib.parse import parse_qs
            qs={k:v[0] for k,v in parse_qs(s.path.split('?',1)[1] if '?' in s.path else '').items()}
            if TCOL is None: b=json.dumps({'error':'tick store unavailable (no mongo)','rows':[]}).encode()
            else:
                try:
                    flt={}
                    if qs.get('ticker'): flt['t']=qs['ticker']
                    rng={}
                    if qs.get('from'): rng['$gte']=int(float(qs['from']))
                    if qs.get('to'): rng['$lte']=int(float(qs['to']))
                    if qs.get('after'): rng['$gt']=int(float(qs['after']))
                    if rng: flt['ms']=rng
                    lim=max(1,min(int(qs.get('limit') or 5000),20000))
                    rows=list(TCOL.find(flt,{'_id':0,'at':0}).sort([('t',1),('ms',1)] if qs.get('ticker') else [('ms',1),('t',1)]).limit(lim))
                    b=json.dumps({'count':len(rows),'limit':lim,'rows':rows}).encode()
                except Exception as e: b=json.dumps({'error':str(e)[:200],'rows':[]}).encode()
            s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.send_header('Access-Control-Allow-Origin','*'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/kxraw':
            # read-only GET of the user's own Kalshi portfolio records, for P&L debugging; never mutates
            from urllib.parse import parse_qs
            q=parse_qs(s.path.split('?',1)[1] if '?' in s.path else ''); pth=(q.get('path') or [''])[0]
            if not pth.startswith('/portfolio/') or LV is None: r=dict(err='bad path or live off')
            else:
                import live as _lv
                try: c,dd=_lv.api('GET',pth); r=dict(code=c,data=dd)
                except Exception as e: r=dict(err=str(e)[:200])
            b=json.dumps(r,default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/truthfix': b=json.dumps(TFX.ST,default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/cryptogap': b=json.dumps(list(SPEC.CRYPTO_GAP),default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/gappaper': b=json.dumps(dict(summary=GAPS.paper_summary(),trades=GAPS.paper_snapshot()),default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/persist': b=json.dumps(GP.stats(),default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/gapshadow': b=json.dumps(GAPS.snapshot(),default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/metalgap': b=json.dumps(list(SPEC.METAL_GAP),default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/predstats': b=json.dumps(PL.stats(),default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.end_headers(); s.wfile.write(b)
        elif p=='/api/idxstats': b=json.dumps(WL.stats(),default=str).encode(); s.send_response(200); s.send_header('Content-Type','application/json'); s.end_headers(); s.wfile.write(b)
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
from reconcile import Reconciler
RECON=Reconciler(LV,pt,lock)
def _reconcile_loop():
    while True:
        RECON.run()
        if LV and LV.allowed() and LV.owner():
            # Reuse the exact websocket stop/lock checks immediately after adoption.
            for t in list(pt.trades):
                if t.get('mode')!='live' or t.get('shadow') or t.get('status')!='open': continue
                try:
                    q=LV.wsq(t['ticker'])
                    if not q:
                        m=LV.market(t['ticker'])
                        if m.get('yes_bid_dollars') is None or m.get('yes_ask_dollars') is None: continue
                        q=dict(yesBid=fnum(m,'yes_bid_dollars'),yesAsk=fnum(m,'yes_ask_dollars'))
                    _ws_quote(t['ticker'],q)
                except Exception as e: RECON.state['lastError']='adoption monitor: '+type(e).__name__
        time.sleep(15)
# Reconcile before the first strategy tick, not after it submits a new entry.
RECON.run()
threading.Thread(target=_reconcile_loop,daemon=True).start()
threading.Thread(target=loop,daemon=True).start()
print('Paper Desk (mode '+('LIVE' if LV and LV.enabled() else 'paper')+') on port',PORT,'data',TF,flush=True)
ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
