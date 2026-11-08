import time
LOCK_HOLD_ENABLED=True
LOCK_HOLD_MIN_P=0.73
LOCK_HOLD_MAX_AGE=60
def model_p(t,now=None):
    now=time.time() if now is None else now
    p=t.get('currentModelP'); at=t.get('currentModelPAt')
    if p is None or at is None or now-at>LOCK_HOLD_MAX_AGE: return None
    return p
def should_hold(t,now=None,log=None):
    # called only when a profit-lock exit would fire; True means skip the sale this tick
    if not LOCK_HOLD_ENABLED: return False
    p=model_p(t,now)
    if t.get('shadow'): log=None
    hold=p is not None and p>=LOCK_HOLD_MIN_P
    if hold:
        if not t.get('lockHeld'): t.update(lockHeld=True,lockHoldP=round(p,4),lockHoldAt=time.time())
        if not t.get('lockHolding'):
            t['lockHolding']=True
            if log: _safe(log,event='lock-hold',tradeId=t.get('id'),ticker=t.get('ticker'),side=t.get('side'),modelP=round(p,4),bid=t.get('markPrice'))
    elif t.get('lockHolding'):
        t['lockHolding']=False
        if log: _safe(log,event='lock-hold-release',tradeId=t.get('id'),ticker=t.get('ticker'),side=t.get('side'),modelP=None if p is None else round(p,4))
    return hold
def _safe(log,**kw):
    try: log(defer=True,**kw)
    except Exception: pass
def summary(trades):
    h=[t for t in trades if t.get('lockHeld')]; c=[t for t in h if t.get('status')!='open']
    return dict(enabled=LOCK_HOLD_ENABLED,threshold=LOCK_HOLD_MIN_P,maxModelAgeSec=LOCK_HOLD_MAX_AGE,count=len(h),open=len(h)-len(c),closed=len(c),
        wins=len([t for t in c if (t.get('pnl') or 0)>0]),pnl=round(sum(t.get('pnl') or 0 for t in c),2))
