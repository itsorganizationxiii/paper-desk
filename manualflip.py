"""LOGGING ONLY: tag and record positions the live engine manages but did not open (owner manual flips).
Never places, sizes, blocks, or exits a trade. Never touches the live test #3 cap or its stats.
Records persist like other books (Mongo collection manual_flips, TTL on openedAtDt)."""
import json, os, re, threading, time
E=os.environ.get
MAX_KEEP=int(E('MANUALFLIP_MAX_KEEP','2000'))
TTL_DAYS=int(E('GAP_TRADE_TTL_DAYS','120'))
SINK=[None]
_lk=threading.Lock()
ITEMS=[]; _by={}
_path=[None]
STAT=dict(seen=0,sells=0,settled=0,backfilled=0,errors=0)
def is_manual(t):
    """True for a live position the bot did not open (no bot buy / liveOrderId)."""
    if not t or t.get('shadow') or t.get('gapLive'): return False
    if t.get('manualFlip') or t.get('tag')=='manual-flip': return True
    if t.get('adopted') and not t.get('liveOrderId'): return True
    return False
def _coin(ticker,asset=None):
    if asset: return str(asset).upper()
    m=re.match(r'KX([A-Z]+)\d',str(ticker or ''))
    return m.group(1) if m else None
def _close_ts(ticker,closes=None):
    if closes: 
        try: return float(closes)
        except Exception: pass
    # KXBTC15M-26NOV100315-15 -> 2026-11-10 03:15 America/New_York
    m=re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{4})',str(ticker or ''))
    if not m: return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        mon={'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
        yy,monm,dd,hhmm=int(m.group(1)),mon.get(m.group(2)),int(m.group(3)),m.group(4)
        if not monm: return None
        return datetime(2000+yy,monm,dd,int(hhmm[:2]),int(hhmm[2:]),tzinfo=ZoneInfo('America/New_York')).timestamp()
    except Exception: return None
def context(coin,ticker,side,lv=None,now=None):
    """Read-only first-seen snapshot from in-memory caches (MoogleEdge, gap-regime, volume, WS quote). Never fetches."""
    now=time.time() if now is None else now
    out=dict(moogleP=None,moogleAgeSec=None,regime=None,volume=None,ask=None,bid=None)
    try:
        import moogle as M
        r=M.CUR.get(coin) if coin else None
        if r and r.get('p') is not None and (not ticker or r.get('ticker') in (None,ticker) or str(r.get('ticker') or '')==str(ticker)):
            p=float(r['p']); out['moogleP']=round(p if str(side).upper()=='YES' else 1-p,4)
            if r.get('t'): out['moogleAgeSec']=round(now-float(r['t']),1)
        elif r and r.get('p') is not None:
            p=float(r['p']); out['moogleP']=round(p if str(side).upper()=='YES' else 1-p,4)
            if r.get('t'): out['moogleAgeSec']=round(now-float(r['t']),1)
    except Exception: pass
    try:
        import gapregime as G
        cur=((G.current() or {}).get('coins') or {}).get(coin) or {}
        out['regime']=cur.get('regime')
        vol=cur.get('volume')
        if not vol:
            try: vol=G.volume_ctx(coin) if coin else None
            except Exception: vol=None
        out['volume']=vol
    except Exception: pass
    try:
        q=None
        if lv is not None and ticker and hasattr(lv,'wsq'): q=lv.wsq(ticker)
        if q:
            ya,yb=q.get('yesAsk'),q.get('yesBid')
            if str(side).upper()=='YES': out['ask']=ya; out['bid']=yb
            else:
                out['ask']=None if yb is None else round(1-float(yb),4)
                out['bid']=None if ya is None else round(1-float(ya),4)
    except Exception: pass
    return out
def _rid(ticker,side,trade_id=None):
    if trade_id is not None: return f'mf|{trade_id}'
    return f'mf|{ticker}|{side}'
def _find(ticker,side,trade_id=None):
    if trade_id is not None:
        x=_by.get(_rid(ticker,side,trade_id))
        if x: return x
    return _by.get(_rid(ticker,side))
def _pnl(rec):
    entry=rec.get('entry')
    sells=rec.get('sells') or []
    sold=sum(float(s.get('qty') or 0) for s in sells)
    proceeds=sum(float(s.get('qty') or 0)*float(s.get('price') or 0)-float(s.get('fee') or 0) for s in sells if s.get('price') is not None)
    qty=float(rec.get('contracts') or 0)
    fee=float(rec.get('entryFee') or 0)
    settle=rec.get('settle')
    if entry is None and rec.get('pnl') is not None: return rec.get('pnl')
    if entry is None: return None
    if rec.get('status') in ('won','lost') and rec.get('pnl') is not None: return rec.get('pnl')
    if sold>=qty-0.005 and sells:
        return round(proceeds-qty*float(entry)-fee,4)
    if settle in ('yes','no') and rec.get('side'):
        win=(settle=='yes')==(str(rec['side']).upper()=='YES')
        held=max(0.0,qty-sold)
        return round(proceeds+(held if win else 0)-qty*float(entry)-fee,4)
    return rec.get('pnl')
def _touch(rec,now=None):
    rec['updatedAt']=time.time() if now is None else now
    p=_pnl(rec)
    if p is not None: rec['pnl']=round(p,4)
    rec['win']=bool(rec.get('pnl') is not None and rec['pnl']>0) if rec.get('pnl') is not None and rec.get('status') in ('exited','won','lost','settled') else rec.get('win')
    if SINK[0] is not None:
        try: SINK[0](json.loads(json.dumps(rec,default=str)))
        except Exception as e: STAT['errors']+=1; print('manualflip sink err',e,flush=True)
def _new(ticker,side,contracts,first,secs,entry,entry_fee,ctx,trade_id=None,source=None,closes=None):
    rid=_rid(ticker,side,trade_id)
    rec=dict(id=rid,tag='manual-flip',ticker=ticker,coin=_coin(ticker),side=side,contracts=contracts,
             firstSeenAt=first,secondsLeft=secs,entry=entry,entryFee=entry_fee,sells=[],
             settle=None,status='open',pnl=None,win=None,closesAt=closes,
             moogleP=(ctx or {}).get('moogleP'),moogleAgeSec=(ctx or {}).get('moogleAgeSec'),
             regime=(ctx or {}).get('regime'),volume=(ctx or {}).get('volume'),
             ask=(ctx or {}).get('ask'),bid=(ctx or {}).get('bid'),
             openedAt=first,source=source or 'adopt',tradeId=trade_id)
    ITEMS.append(rec); _by[rid]=rec
    if len(ITEMS)>MAX_KEEP:
        for old in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(old['id'],None)
        del ITEMS[:len(ITEMS)-MAX_KEEP]
    return rec
def seen(t,lv=None,now=None):
    """First sight of a managed-but-not-opened live position (adopt path). Logging only."""
    if not t: return None
    try:
        now=time.time() if now is None else now
        ticker=t.get('ticker'); side=t.get('side')
        if not ticker or not side: return None
        first=float(t.get('openedAt') or now)
        close=_close_ts(ticker,t.get('closesAt'))
        secs=None if close is None else round(close-first,1)
        ctx=context(_coin(ticker,t.get('asset')),ticker,side,lv=lv,now=now)
        entry=t.get('price'); fee=t.get('entryFee')
        # entry from Kalshi fills already computed by reconciler (cost basis); else null
        with _lk:
            rec=_find(ticker,side,t.get('id'))
            if rec is None:
                rec=_new(ticker,side,t.get('contracts'),first,secs,entry,fee,ctx,trade_id=t.get('id'),source='adopt',closes=close)
                # also index ticker|side so a later order-backfill merges
                _by.setdefault(_rid(ticker,side),rec)
                STAT['seen']+=1
            else:
                rec['tradeId']=rec.get('tradeId') or t.get('id')
                if entry is not None and rec.get('entry') is None: rec['entry']=entry
                if fee is not None and rec.get('entryFee') is None: rec['entryFee']=fee
                if rec.get('contracts') is None: rec['contracts']=t.get('contracts')
                if rec.get('secondsLeft') is None: rec['secondsLeft']=secs
                if rec.get('regime') is None and ctx: rec.update({k:ctx[k] for k in ('moogleP','moogleAgeSec','regime','volume','ask','bid') if rec.get(k) is None and ctx.get(k) is not None})
            t['tag']='manual-flip'; t['manualFlip']=True
            _touch(rec,now)
            return rec
    except Exception as e:
        STAT['errors']+=1; print('manualflip seen err',e,flush=True); return None
def on_sell(t,qty,price,fee=0,at=None,order_id=None):
    """Append one live sell of a manual-flip position. Logging only; does not change the sale."""
    if not t or not (is_manual(t) or t.get('adopted') or t.get('tag')=='manual-flip' or t.get('manualFlip')): return None
    try:
        at=time.time() if at is None else at
        ticker,side=t.get('ticker'),t.get('side')
        with _lk:
            rec=_find(ticker,side,t.get('id'))
            if rec is None:
                rec=seen(t,now=at)
            if rec is None: return None
            sell=dict(at=at,price=price,qty=qty,fee=fee)
            if order_id: sell['orderId']=order_id
            rec.setdefault('sells',[])
            # de-dup identical sells (restart / double hook)
            if not any(abs(float(s.get('at') or 0)-float(at))<0.05 and abs(float(s.get('qty') or 0)-float(qty or 0))<0.005 for s in rec['sells']):
                rec['sells'].append(sell); STAT['sells']+=1
            rec['status']='exited' if t.get('status')=='exited' or (qty and rec.get('contracts') and float(qty)>=float(rec['contracts'])-0.005) else rec.get('status') or 'open'
            if t.get('pnl') is not None: rec['pnl']=t['pnl']
            if t.get('exitReason'): rec['exitReason']=t.get('exitReason')
            _touch(rec,at)
            return rec
    except Exception as e:
        STAT['errors']+=1; print('manualflip on_sell err',e,flush=True); return None
def on_settle(t):
    """Record Kalshi settlement (or would-have-won after an early cashout). Logging only."""
    if not t or not is_manual(t): return None
    try:
        ticker,side=t.get('ticker'),t.get('side')
        with _lk:
            rec=_find(ticker,side,t.get('id'))
            if rec is None: rec=seen(t)
            if rec is None: return None
            res=t.get('finalResult') or t.get('settle')
            if res in ('yes','no'): rec['settle']=res
            if t.get('wouldHaveWon') is not None: rec['wouldHaveWon']=t.get('wouldHaveWon')
            if t.get('status') in ('won','lost'): rec['status']=t['status']
            elif rec.get('status')=='open' and t.get('status') in ('won','lost','exited'): rec['status']=t['status']
            if t.get('pnl') is not None: rec['pnl']=t['pnl']
            STAT['settled']+=1; _touch(rec)
            return rec
    except Exception as e:
        STAT['errors']+=1; print('manualflip on_settle err',e,flush=True); return None
def _bot_tickers(orders,trades):
    bot=set()
    for o in orders or []:
        if (o or {}).get('action')=='buy' and o.get('ticker') and (o.get('filled') or 0)>0:
            bot.add((o.get('ticker'),o.get('side')))
    for t in trades or []:
        if t and t.get('liveOrderId') and not is_manual(t) and t.get('ticker'):
            bot.add((t.get('ticker'),t.get('side')))
        if t and t.get('gapLive') and t.get('ticker'):
            bot.add((t.get('ticker'),t.get('side')))
    return bot
def backfill(orders=None,trades=None):
    """Startup: adopt any already-tracked manual trades, plus unmatched sells in the live order log (e.g. 3:14 AM KXBTC15M-26NOV100315-15)."""
    n=0
    try:
        for t in trades or []:
            if is_manual(t) or (t and t.get('adopted') and not t.get('liveOrderId')):
                rec=seen(t)
                if rec is not None:
                    if t.get('status')=='exited' or t.get('exitPrice') is not None:
                        on_sell(t,t.get('contracts'),t.get('exitPrice'),t.get('exitFee') or 0,at=t.get('closedAt') or t.get('liveSellAt'),order_id=t.get('liveSellOrderId'))
                    if t.get('settle') or t.get('finalResult') or t.get('status') in ('won','lost'):
                        on_settle(t)
                    n+=1
        bot=_bot_tickers(orders,trades)
        # group sells by ticker+side
        sells={}
        for o in orders or []:
            if (o or {}).get('action') not in ('sell','sell-fill-recovered'): continue
            if not o.get('ticker') or (o.get('filled') or 0)<=0 or o.get('fillPrice') is None: continue
            key=(o.get('ticker'),o.get('side'))
            if key in bot: continue
            sells.setdefault(key,[]).append(o)
        with _lk:
            for (ticker,side),ss in sells.items():
                if _find(ticker,side) is not None: continue
                ss=sorted(ss,key=lambda o:float(o.get('at') or 0))
                first=float(ss[0].get('at') or time.time())
                # earlier order-submitted on the same ticker, if any, is a closer first-seen
                for o in orders or []:
                    if o.get('ticker')==ticker and o.get('side')==side and o.get('action')=='order-submitted' and (o.get('at') or first)<=first:
                        first=float(o['at'])
                close=_close_ts(ticker)
                secs=None if close is None else round(close-first,1)
                qty=sum(float(s.get('filled') or 0) for s in ss)
                rec=_new(ticker,side,qty,first,secs,None,None,{},source='recentOrders',closes=close)
                _by.setdefault(_rid(ticker,side),rec)
                for s in ss:
                    rec['sells'].append(dict(at=s.get('at'),price=s.get('fillPrice'),qty=s.get('filled'),fee=s.get('fee') or 0))
                rec['status']='exited'; STAT['backfilled']+=1; STAT['sells']+=len(ss)
                _touch(rec,ss[-1].get('at'))
                n+=1
        return n
    except Exception as e:
        STAT['errors']+=1; print('manualflip backfill err',e,flush=True); return n
def merge_in(items):
    with _lk:
        for o in items or []:
            try:
                if not isinstance(o,dict) or not o.get('id'): continue
                o={k:v for k,v in o.items() if k!='_id'}
                x=_by.get(o['id'])
                if x is None:
                    ITEMS.append(o); _by[o['id']]=o
                    if o.get('ticker') and o.get('side'): _by.setdefault(_rid(o['ticker'],o['side']),o)
                else:
                    # prefer the record with more completion
                    def rank(r): return (r.get('settle') is not None, len(r.get('sells') or []), r.get('pnl') is not None, r.get('entry') is not None)
                    if rank(o)>=rank(x): x.clear(); x.update(o)
            except Exception: pass
        ITEMS.sort(key=lambda o:o.get('openedAt') or o.get('firstSeenAt') or 0)
        if len(ITEMS)>MAX_KEEP:
            for old in ITEMS[:len(ITEMS)-MAX_KEEP]: _by.pop(old['id'],None)
            del ITEMS[:len(ITEMS)-MAX_KEEP]
def load(data_dir):
    _path[0]=os.path.join(data_dir,'manual_flips.json')
    try:
        with open(_path[0]) as f: d=json.load(f)
        merge_in(d.get('items'))
    except Exception: pass
def snapshot():
    with _lk: return [dict(o) for o in ITEMS]
def reset():
    """Test helper."""
    with _lk:
        ITEMS.clear(); _by.clear()
        for k in list(STAT): STAT[k]=0
def summary():
    its=snapshot()
    fin=[o for o in its if o.get('status') in ('exited','won','lost','settled') and o.get('pnl') is not None]
    n=len(its); wins=sum(1 for o in fin if (o.get('pnl') or 0)>0)
    pnl=round(sum(o.get('pnl') or 0 for o in fin),2)
    return dict(n=n,open=sum(1 for o in its if o.get('status')=='open'),
                closed=len(fin),wins=wins,losses=sum(1 for o in fin if (o.get('pnl') or 0)<=0),
                pnl=pnl,stats=dict(STAT),
                persist=dict(collection='manual_flips',ttlDays=TTL_DAYS,note=f'Mongo TTL {TTL_DAYS}d on openedAtDt; in-memory cap {MAX_KEEP}.'),
                note='LOGGING ONLY: the live engine still manages owner-placed positions; this book just tags and records them. They never count toward live test #3.')
def api(limit=300,items=True):
    its=snapshot()[-limit:] if items else []
    return dict(summary=summary(),items=its[::-1])
def brief():
    s=summary()
    return f"{s['n']}, {s['wins']} win{'s' if s['wins']!=1 else ''}, {s['pnl']:+.2f}"
