"""Offline: /api/cpuprof shape (read-only profiler) + display-only gap-live mark."""
import ast, json, threading, time, types
import cpuprof as CPU
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
def busy():
    end=time.time()+3
    while time.time()<end: sum(i*i for i in range(5000))
threading.Thread(target=busy,name='busy-worker',daemon=True).start()
with CPU.timed('demo.save',lambda:4096): time.sleep(0.02)
r=CPU.profile(1.0)
keys={'windowSec','samples','processCpuPct','cgroup','topThreads','threadHotspots','topOwnFunctions','topLeafFunctions','stages','throttledPct'}
chk('profile keys',keys<=set(r))
chk('window capped/short and sampled',0.9<=r['windowSec']<=1.6 and r['samples']>=30)
chk('busy thread is top by CPU',r['topThreads'][0]['name']=='busy-worker' and r['topThreads'][0]['cpuPct']>30)
chk('top functions are [name,count,pct]',r['topLeafFunctions'] and len(r['topLeafFunctions'][0])==3)
chk('stage timer recorded with bytes',r['stages']['demo.save']['bytes']==4096 and r['stages']['demo.save']['n']==1)
chk('cgroup block has cpuCount/rss',r['cgroup'].get('cpuCount') and 'rssMB' in r['cgroup'])
chk('sec capped at 5',CPU.profile.__defaults__[0]==3.0)
t=threading.Thread(target=lambda:CPU.profile(1.0)); t.start(); time.sleep(0.1)
chk('one profile at a time',CPU.profile(0.5).get('error')=='profile already running'); t.join()
txt=json.dumps(r,default=str)
chk('no secret-ish fields',not any(w in txt.lower() for w in ('kalshi-access','private','signature','balance_dollars')))
chk('json serialisable',isinstance(txt,str))
# server wiring + display-only mark
src=open('server.py').read(); tree=ast.parse(src)
chk('/api/cpuprof route present (GET, read-only)',"elif p=='/api/cpuprof':" in src and 'CPU.profile(' in src)
imp=src.index('import cpuprof as CPU'); first_use=src.index('CPU.'); inst=src.index('pt=PaperTrader()') if 'pt=PaperTrader()' in src else len(src)
chk('cpuprof imported before first use / PaperTrader init (boot regression)',imp<first_use and imp<inst)
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_gap_live_marks')
g={'time':time,'LV':None,'pt':types.SimpleNamespace(trades=[dict(id=1190,gapLive=True,status='open',asset='ETH',side='NO',ticker='KXETH15M-X',price=0.47,stake=0.9164,contracts=1.88),
                                                           dict(id=5,gapLive=False,status='open',asset='ETH',side='NO',ticker='KXETH15M-X',price=0.5,stake=1,contracts=2)])}
exec(compile(ast.Module(body=[fn],type_ignores=[]),'server.py','exec'),g)
g['_gap_live_marks']({'ETH':dict(ticker='KXETH15M-X',yesBid=0.67,noBid=0.32)})
t0,t1=g['pt'].trades
chk('gap-live card gets mark from REST market (NO bid)',t0['markPrice']==0.32 and t0['markSource']=='kalshi-rest' and t0['unrealizedPnl']==round(1.88*0.32-0.9164,2))
chk('costPerContract incl fee shown',t0['costPerContract']==round(0.9164/1.88,4))
chk('entry/stake/contracts untouched',t0['price']==0.47 and t0['stake']==0.9164 and t0['contracts']==1.88 and t0['status']=='open')
chk('non-gap-live trades untouched',('markPrice' not in t1))
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
