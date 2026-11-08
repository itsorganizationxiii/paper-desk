# Read-only prediction log (Nov 8, 2026). Separate evaluation dataset; never feeds trading or the 300-window pricing experiment.
# One record per coin per 15-min window (_id = windowId = '<SERIES>-<close unix>', same scheme as idxcmp), written once after close.
# Records only what the existing model already computed on real ticks. No tick observed -> no record. Nothing is backfilled or simulated.
# observe() is O(1) dict work in the trading thread, wrapped so it can never raise; all I/O (regime, trend, Mongo, settlement) runs in its own thread.
import time, threading, collections
ST=dict(observed=0,obsErrors=0,written=0,writeErrors=0,dropped=0,queued=0,settled=0,lastErr=None)
_win={}; _q=collections.deque(); _col=None; BOOT=int(time.time()); MAXQ=500
_fn=dict(regime=lambda a:None,trend=lambda s:None,result=lambda tk:None,owner=lambda:True)
def _f(x):
    try: return None if x is None else float(x)
    except Exception: return None
def observe(sigs,mode,now=None):
    try:
        now=now or time.time()
        for a,sg in (sigs or {}).items():
            k=sg.get('kalshi') if isinstance(sg,dict) else None
            if not k or not k.get('ticker') or not k.get('close'): continue
            ser=k['ticker'].split('-')[0]; close=int(round(k['close'])); wid=f'{ser}-{close}'
            r=_win.get(wid)
            if r is None:
                r=_win[wid]=dict(_id=wid,windowId=wid,asset=a,series=ser,ticker=k['ticker'],close=close,firstObsAt=int(now),ticks=0,
                                 skipReasons=collections.Counter(),partialObservation=BOOT>close-900,mode=mode)
            act=str(sg.get('action') or ''); left=close-now
            snap=dict(at=int(now),secsToClose=round(left),action=act,modelP=_f(sg.get('modelP')),signalDirection=sg.get('direction'),
                      momentum=_f(sg.get('momentum')),cvd=_f(sg.get('cvd')),vol15=_f(sg.get('sd15')),spot=_f(sg.get('spot')),
                      yesAsk=_f(k.get('yesAsk')),yesBid=_f(k.get('yesBid')),noAsk=_f(k.get('noAsk')),noBid=_f(k.get('noBid')),strike=_f(k.get('strike')))
            r['ticks']+=1; r['lastObsAt']=int(now)
            if act.startswith('skip:'): r['skipReasons'][act.split('(')[0].strip()[:80]]+=1
            if 'OPENED' in act and not r.get('traded'):
                r['traded']=True; r['decision']=snap; r['decisionKind']='trade'
                try: r['tradeId']=int(act.split('#')[1].split()[0])
                except Exception: r['tradeId']=None
            elif not r.get('traded') and left>=60: r['decision']=snap; r['decisionKind']='skip'
            r['_need']=True
        ST['observed']+=1
    except Exception as e: ST['obsErrors']+=1; ST['lastErr']=f'observe {type(e).__name__}: {e}'[:120]
def build(r):
    d=r.get('decision') or {}; mp=d.get('modelP')
    out={k:v for k,v in r.items() if k not in ('_need','_regAt')}; out['skipReasons']=dict(r['skipReasons'])
    if mp is not None:
        side='YES' if mp>0.5 else 'NO' if mp<0.5 else None
        out['modelDirection']=side; out['confidence']=round(max(mp,1-mp),4)
        ask=d.get('yesAsk') if side=='YES' else d.get('noAsk') if side=='NO' else None
        bid=d.get('yesBid') if side=='YES' else d.get('noBid') if side=='NO' else None
        out['edge']=round(out['confidence']-ask,4) if ask else None
        out['spread']=round(ask-bid,4) if ask is not None and bid is not None else None
    else: out.update(modelDirection=None,confidence=None,edge=None,spread=None)   # model had no fresh p: recorded as missing, not guessed
    out['decision']=d or None; out['skipReason']=None if r.get('traded') else (d.get('action') if d else None)
    out.setdefault('settlement','PENDING'); out.setdefault('result',None)
    return out
def _io_pass(now=None):
    now=now or time.time()
    for wid,r in list(_win.items()):
        if r.get('_need') and r.get('decision') and r.get('_regAt',0)<now-60:
            r['_regAt']=now
            try: r['regime']=_fn['regime'](r['asset']); r['priorTrend']=_fn['trend'](r['series']); r['regimeAt']=int(now)
            except Exception as e: ST['lastErr']=f'regime {e}'[:120]
            r['_need']=False
        if now>r['close']+10:
            del _win[wid]
            if len(_q)>=MAXQ: ST['dropped']+=1
            else: _q.append(build(r))
    ST['queued']=len(_q)
    while _q and _col is not None:
        row=_q[0]
        try: _col.update_one({'_id':row['_id']},{'$setOnInsert':row},upsert=True); _q.popleft(); ST['written']+=1
        except Exception as e: ST['writeErrors']+=1; ST['lastErr']=f'write {type(e).__name__}: {e}'[:120]; break
    if _col is not None and int(now)%60<3:
        try:
            for p in _col.find({'settlement':'PENDING','close':{'$lt':now-60}},{'ticker':1,'close':1}).limit(50):
                res=_fn['result'](p['ticker'])
                if res in ('yes','no'): _col.update_one({'_id':p['_id'],'settlement':'PENDING'},{'$set':dict(result=res,settlement='SETTLED')}); ST['settled']+=1
                elif now-p['close']>1800: _col.update_one({'_id':p['_id'],'settlement':'PENDING'},{'$set':dict(settlement='MISSING')})
        except Exception as e: ST['lastErr']=f'settle {e}'[:120]
def _loop():
    while True:
        time.sleep(2)
        try:
            if _fn['owner'](): _io_pass()
        except Exception as e: ST['lastErr']=f'loop {e}'[:120]
def stats():
    out=dict(ST,openWindows=len(_win))
    if _col is not None:
        try: out.update(records=_col.count_documents({}),traded=_col.count_documents({'traded':True}))
        except Exception: pass
    return out
def start(db,regime=None,trend=None,result=None,owner=None):
    global _col
    if db is not None: _col=db['predictions']
    for k,v in (('regime',regime),('trend',trend),('result',result),('owner',owner)):
        if v: _fn[k]=v
    threading.Thread(target=_loop,daemon=True).start()
