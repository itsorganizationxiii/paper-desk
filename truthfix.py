# Owner checklist #3 fix (Nov 8, 2026). Record-keeping only: never places, cancels or sells orders, never changes sizing or entry logic.
# (1) Replace estimated P&L with Kalshi's own fills, fees and settlements (GET /portfolio/fills?ticker=, GET /portfolio/settlements?ticker=).
# (2) Backfill missing finalResult from GET /markets/{ticker}, tagged finalResultBackfilled=True.
# (4) Reconcile total bot after-fee P&L against Kalshi's per-ticker P&L and the account balance change; log the difference.
import time, threading, traceback
from urllib.parse import urlencode
SANE=None
ST=dict(ran=False,at=None,err=None,fixed=[],backfilled=[],recon=None)
def _n(f,k): v=f.get(k+'_fp') if f.get(k+'_fp') not in (None,'') else f.get(k); return float(v or 0)
def _px(f,side):
    v=f.get(side.lower()+'_price_dollars')
    if v is None:
        o=f.get(('no' if side=='YES' else 'yes')+'_price_dollars')
        if o is not None: v=1-float(o)
    return float(v)
def ticker_pnl(L,live,tk):
    """Kalshi's own realized P&L for one ticker: sells + settlement revenue - buy cost - all fees."""
    fills=L.pages('/portfolio/fills?'+urlencode(dict(ticker=tk,exchange_index=live.SHARD,subaccount=0,limit=1000)),'fills')
    sets=L.pages('/portfolio/settlements?'+urlencode(dict(ticker=tk,limit=200)),'settlements')
    cash=0.0; fees=0.0; buys=sells=0.0; bcost=scash=0.0
    for f in fills:
        side=str(f.get('side') or '').upper(); q=_n(f,'count'); p=_px(f,side); fee=float(f.get('fee_cost') or 0)
        fees+=fee
        # Kalshi reports a sell of YES as action=sell side=no at the NO price (and vice versa): proceeds are the OTHER side's price
        if f.get('action')=='sell': sp=1-p; cash+=q*sp; sells+=q; scash+=q*sp
        else: cash-=q*p; buys+=q; bcost+=q*p
    rev=0.0; sfee=0.0; res=None
    for x in sets:
        rev+=float(x.get('revenue') or 0)/100; res=x.get('market_result') or res  # settlement fee_cost repeats the fill fees; not added again
    # Kalshi's fill action/side are YES-book relative (a NO buy shows as action=sell side=no), so use the settlement record's
    # aggregates: revenue + paired YES/NO contracts ($1 each) - total YES cost - total NO cost - fees.
    if sets:
        x=sets[0]; yc=float(x.get('yes_count_fp') or 0); nc=float(x.get('no_count_fp') or 0)
        sp=float(x.get('revenue') or 0)/100+min(yc,nc)-float(x.get('yes_total_cost_dollars') or 0)-float(x.get('no_total_cost_dollars') or 0)-float(x.get('fee_cost') or 0)
        return dict(pnl=round(sp,4),fees=round(float(x.get('fee_cost') or 0),4),buys=buys,sells=sells,avgBuy=None,avgSell=None,revenue=rev,result=res,nFills=len(fills),nSettlements=len(sets),method='settlement-aggregate')
    return dict(pnl=round(cash+rev-fees-sfee,4),fees=round(fees+sfee,4),buys=buys,sells=sells,avgBuy=(bcost/buys if buys else None),avgSell=(scash/sells if sells else None),revenue=rev,result=res,nFills=len(fills),nSettlements=len(sets))
def verify_fees(L,t,k,fills=None):
    """Set the bot's logged fees to Kalshi's: entry order's fill fees + all other fills on the ticker."""
    t['kalshiPnl']=k['pnl']
    try:
        fills=fills if fills is not None else L.pages('/portfolio/fills?'+urlencode(dict(ticker=t['ticker'],limit=200)),'fills')
        ef=sum(float(f.get('fee_cost') or 0) for f in fills if f.get('order_id')==t.get('liveOrderId'))
        xf=sum(float(f.get('fee_cost') or 0) for f in fills if f.get('order_id')!=t.get('liveOrderId'))
        if fills:
            if abs(ef-(t.get('entryFee') or 0))>0.0005 or abs(xf-(t.get('exitFee') or 0))>0.0005: t['feesBot']=dict(entry=t.get('entryFee'),exit=t.get('exitFee'))
            t['entryFee']=round(ef,4); t['exitFee']=round(xf,4); t['feesVerified']='kalshi-fills'
    except Exception as e: t['feesVerifyErr']=str(e)[:80]
def verifier(pt,lock,L,live,every=600):
    """Ongoing: each newly closed live trade (after its market settles) gets Kalshi P&L + Kalshi fees. Plain code, no model calls."""
    while True:
        time.sleep(every)
        try:
            if L.lease_ok is not None and not L.owner(): continue
            now=time.time()
            todo=[t for t in pt.trades if t.get('mode')=='live' and not t.get('shadow') and t.get('status')!='open' and 'kalshiPnl' not in t and now>(t.get('closesAt') or now)+120][:20]
            for t in todo:
                k=ticker_pnl(L,live,t['ticker'])
                if k.get('nSettlements'):
                    with lock: verify_fees(L,t,k)
                time.sleep(1)
            if todo:
                with lock: pt.save()
        except Exception as e: print('verifier err',e,flush=True)
def run(pt,lock,L,live,col=None):
    try:
        trades=[t for t in pt.trades if t.get('mode')=='live' and not t.get('shadow') and t.get('status')!='open']
        by={}
        for t in trades: by.setdefault(t['ticker'],[]).append(t)
        botTot=0.0; kalTot=0.0; per=[]
        ST['total']=len(by); ST['done']=0; ST['errs']=[]
        for tk,ts in by.items():
            ST['done']+=1; ST['cur']=tk
            try: k=ticker_pnl(L,live,tk)
            except Exception as e: per.append(dict(ticker=tk,err=str(e)[:100])); ST['errs'].append(tk+': '+str(e)[:80]); continue
            bp=round(sum(t.get('pnl') or 0 for t in ts),4); botTot+=bp; kalTot+=k['pnl']
            per.append(dict(ticker=tk,ids=[t['id'] for t in ts],bot=bp,kalshi=k['pnl'],diff=round(bp-k['pnl'],4)))
            with lock:
                for t in ts: verify_fees(L,t,k)
            with lock:
                for t in ts:
                    if not t.get('finalResult') and k['result'] in ('yes','no'):
                        t['finalResult']=k['result']; t['finalResultBackfilled']=True; ST['backfilled'].append(t['id'])
                    if t.get('pnlSource')=='kalshi-fills' and len(ts)==1:   # redo earlier overwrite made with the buggy parser
                        t['pnl']=t.get('pnlBotEstimate'); t['pnlEstimated']=True; t.pop('pnlSource',None)
                    if len(ts)==1 and (t.get('pnlEstimated') or (t.get('status')=='exited' and t.get('exitFee') in (None,''))):
                        ok,lo,hi=SANE(t,k['pnl']) if SANE else (True,None,None)
                        if not ok: t['pnlFlagged']=dict(kalshi=k['pnl'],lo=lo,hi=hi); ST.setdefault('flagged',[]).append(t['id']); continue
                        old=t.get('pnl'); t['pnlBotEstimate']=old; t['pnl']=k['pnl']; t['pnlSource']='kalshi-fills'; t.pop('pnlEstimated',None)
                        t['exitFee']=round(k['fees']-(t.get('entryFee') or 0),4)
                        if k['avgSell'] is not None: t['exitPrice']=round(k['avgSell'],4)
                        t['kalshiRecord']=k; ST['fixed'].append(dict(id=t['id'],old=old,new=k['pnl']))
            time.sleep(1.0)
        with lock: pt.save()
        bal=None
        try: bal=L.balance()
        except Exception: pass
        first=min(trades,key=lambda t:t['openedAt']) if trades else None
        start=first.get('liveCashAtEntry') if first else None
        man=[]; manTot=0.0
        try:
            t0=int(first['openedAt']) if first else 0; bt=set(by)
            for x in L.pages('/portfolio/settlements?'+urlencode(dict(min_ts=t0,limit=200)),'settlements'):
                if x.get('ticker') in bt: continue
                yc=float(x.get('yes_count_fp') or 0); nc=float(x.get('no_count_fp') or 0)
                v=float(x.get('revenue') or 0)/100+min(yc,nc)-float(x.get('yes_total_cost_dollars') or 0)-float(x.get('no_total_cost_dollars') or 0)-float(x.get('fee_cost') or 0)
                man.append(dict(ticker=x.get('ticker'),tag='manual',pnl=round(v,4))); manTot+=v
        except Exception as e: ST['errs'].append('manual: '+str(e)[:80])
        ST['manual']=dict(tickers=man,pnl=round(manTot,4))
        ST['recon']=dict(trades=len(trades),tickers=len(by),botPnl=round(botTot,4),kalshiPnl=round(kalTot,4),diffBotMinusKalshi=round(botTot-kalTot,4),
            startCash=start,startTradeId=first['id'] if first else None,cashNow=bal,balanceChange=(round(bal-start,4) if bal is not None and start is not None else None),
            diffKalshiPnlVsBalance=(round(kalTot-(bal-start),4) if bal is not None and start is not None else None),
            note='balance change includes any deposits/withdrawals since the first live trade; Kalshi per-ticker P&L does not',
            worst=sorted([p for p in per if 'diff' in p],key=lambda p:-abs(p['diff']))[:15],errors=[p for p in per if 'err' in p])
        ST.update(ran=True,at=time.time())
        if col is not None: col.insert_one({**{k:v for k,v in ST.items() if k!='_id'},'at':time.time()})
    except Exception as e: ST['err']=traceback.format_exc()[-400:]
def start(pt,lock,L,live,col=None,delay=60):
    def go():
        time.sleep(delay)
        while L.lease_ok is not None and not L.owner(): time.sleep(10)
        run(pt,lock,L,live,col)
    threading.Thread(target=go,daemon=True).start()
    threading.Thread(target=verifier,args=(pt,lock,L,live),daemon=True).start()
