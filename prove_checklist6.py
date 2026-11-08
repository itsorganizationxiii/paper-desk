import sys,types;sys.path.insert(0,'/workspace/paper-desk-deploy')
import live as L
# Daily cap: simulate with cap enabled at $3
L.HARD_DAILY_LOSS=3.0; L.DAILY_LOSS=0
o=L.Live.__new__(L.Live); import threading
o.lock=threading.Lock(); o.killed=False; o.killReason=None; o.dayPnl=0.0; o.day=L.et_day(); o.persist=lambda:None
src=open('/workspace/paper-desk-deploy/live.py').read()
line=[l for l in src.splitlines() if '_daily_cap() and not s.killed' in l][0].strip()
s=o; s.dayPnl=-3.10; exec(line,vars(L),{"s":o})
print('DAILY_CAP enabled=3.0 dayPnl=-3.10 -> killed',o.killed,'|',o.killReason)
L.HARD_DAILY_LOSS=0.0; o.killed=False; o.killReason=None; exec(line,vars(L),{"s":o})
print('DAILY_CAP off(0) dayPnl=-3.10 -> killed',o.killed)
# Stale feed: ws returns no fresh quote 3 times
o2=L.Live.__new__(L.Live); o2.lock=threading.Lock(); o2.killed=False; o2.killReason=None; o2.persist=lambda:None; o2.tried=set(); o2.ws=object(); o2.logs=[]
o2.wsq=lambda t:None; o2.entry_allowed=lambda *a:True; o2._rollday=lambda:None; o2.log=lambda **k:o2.logs.append(k); o2.cash=15; o2.cashAt=9e18
for i in range(3): r=o2.buy('BTC',f'T{i}','YES',0.05,0.5)
print('STALE 3 attempts -> killed',o2.killed,'|',o2.killReason,'| logs',[x.get('error') for x in o2.logs])
r=o2.buy('BTC','T9','YES',0.05,0.5); print('next buy after kill ->',r,o2.logs[-1].get('error'))
