import time,lockhold as LH
fails=0
def ok(c,m):
    global fails
    print(('PASS ' if c else 'FAIL ')+m)
    if not c: fails+=1
logs=[]
def log(defer=True,**kw): logs.append(kw)
now=time.time()
t=dict(id=1,currentModelP=0.80,currentModelPAt=now)
ok(LH.should_hold(t,now,log),'holds at p=0.80')
ok(t.get('lockHeld') and t['lockHoldP']==0.8,'tagged lockHeld with first p')
LH.should_hold(t,now,log); ok(len(logs)==1,'deduped log')
t['currentModelP']=0.70
ok(not LH.should_hold(t,now,log),'releases below 0.73')
ok(len(logs)==2 and logs[1]['event']=='lock-hold-release','release logged')
ok(t['lockHoldP']==0.8,'first p kept')
ok(LH.should_hold(dict(currentModelP=0.73,currentModelPAt=now),now),'0.73 inclusive')
ok(not LH.should_hold(dict(currentModelP=0.9,currentModelPAt=now-120),now),'stale model no hold')
ok(not LH.should_hold(dict(currentModelP=None,currentModelPAt=now),now),'missing model no hold')
l2=[]; LH.should_hold(dict(shadow=True,currentModelP=0.9,currentModelPAt=now),now,lambda **k:l2.append(k)); ok(not l2,'shadow not logged')
LH.LOCK_HOLD_ENABLED=False; ok(not LH.should_hold(dict(currentModelP=0.9,currentModelPAt=now),now),'disabled'); LH.LOCK_HOLD_ENABLED=True
s=LH.summary([dict(lockHeld=True,status='settled',pnl=0.4),dict(lockHeld=True,status='open'),dict(status='settled',pnl=1)])
ok(s['count']==2 and s['wins']==1 and s['pnl']==0.4 and s['threshold']==0.73,'summary')
src=open('server.py').read()
ok(src.count('LH.should_hold(')==2,'wired into both profit-lock paths only')
print('FAILS',fails); raise SystemExit(1 if fails else 0)
