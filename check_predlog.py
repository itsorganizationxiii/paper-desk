# Prediction log tests: one record per coin per window, trade vs skip, no fabrication, failures can't block or raise.
import time, predlog as P
ok=True
def ck(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
class Col:
    def __init__(s,fail=0): s.d={}; s.fail=fail
    def update_one(s,q,u,upsert=False):
        if s.fail: s.fail-=1; raise Exception('db down')
        if '$setOnInsert' in u:
            if q['_id'] not in s.d: s.d[q['_id']]=dict(u['$setOnInsert'])
        else:
            r=s.d.get(q['_id'])
            if r and all(r.get(k)==v for k,v in q.items() if k!='_id'): r.update(u['$set'])
    def find(s,q,p=None):
        class L(list):
            def limit(self,n): return self[:n]
        return L([r for r in s.d.values() if r.get('settlement')==q['settlement'] and r['close']<q['close']['$lt']])
    def count_documents(s,q): return len([r for r in s.d.values() if all(r.get(k)==v for k,v in q.items())])
close=int(time.time())//900*900+900; P.BOOT=close-2000
k=dict(ticker='KXBTC15M-X',close=close,yesAsk=0.55,yesBid=0.53,noAsk=0.47,noBid=0.45,strike=75000)
P.observe({'BTC':dict(kalshi=k,modelP=0.62,direction='YES',momentum=0.001,cvd=5,sd15=0.002,action='skip: edge +0.020 < 0.07')},'paper',now=close-600)
P.observe({'BTC':dict(kalshi=k,modelP=0.66,direction='YES',action='OPENED #1090')},'paper',now=close-500)
P.observe({'BTC':dict(kalshi=k,modelP=0.30,direction='NO',action='holding/traded this window')},'paper',now=close-400)
k2=dict(k,ticker='KXETH15M-X'); P.observe({'ETH':dict(kalshi=k2,modelP=None,direction='disagree',action='skip: momentum/CVD disagree')},'paper',now=close-300)
ck('observe never raises on garbage',P.observe({'SOL':None,'X':dict(kalshi={'ticker':None})},'paper') is None and P.ST['obsErrors']==0)
ck('one record per coin per window',len(P._win)==2)
P._fn['regime']=lambda a:(_ for _ in ()).throw(Exception('coinbase down')); P._fn['result']=lambda tk:'yes'
c=Col(fail=1); P._col=c
P._io_pass(now=close+11)   # first write fails (db down) -> stays queued
ck('write failure keeps records queued',len(P._q)==2 and len(c.d)==0 and P.ST['writeErrors']==1)
P._io_pass(now=close+15); ck('retry writes both windows',len(c.d)==2 and len(P._q)==0)
r=c.d['KXBTC15M-%d'%close]
ck('trade decision = trade tick (later ticks ignored)',r['traded'] and r['tradeId']==1090 and r['decision']['modelP']==0.66 and r['modelDirection']=='YES' and r['confidence']==0.66)
ck('edge/spread from that tick',r['edge']==round(0.66-0.55,4) and r['spread']==0.02)
ck('regime failure recorded as missing, not guessed',r.get('regime') is None)
e=c.d['KXETH15M-%d'%close]
ck('no model p -> direction/confidence missing, skip reason kept',e['modelDirection'] is None and e['confidence'] is None and e['skipReason']=='skip: momentum/CVD disagree' and not e.get('traded'))
P._io_pass(now=close+15); P.observe({'BTC':dict(kalshi=k,modelP=0.9,action='skip: x')},'paper',now=close+1); P._io_pass(now=close+30)
ck('re-finalize same window cannot overwrite',c.d['KXBTC15M-%d'%close]['decision']['modelP']==0.66)
P._io_pass(now=close+61+(60-(close+61)%60))
ck('settlement backfilled separately',r['settlement']=='SETTLED' and r['result']=='yes')
# blocking regime call must not block observe (observe never calls I/O)
P._fn['regime']=lambda a:time.sleep(5); t0=time.time(); P.observe({'BTC':dict(kalshi=dict(k,close=close+900),modelP=0.6,action='skip: y')},'paper',now=close+100); ck('observe is non-blocking (<10ms)',time.time()-t0<0.01)
# queue bound: db down forever -> bounded, counted drops, never grows
P._col=Col(fail=10**9); P._q.clear(); P.MAXQ=3
for i in range(5): P._win['w%d'%i]=dict(_id='w%d'%i,close=0,asset='BTC',series='S',ticker='t',skipReasons=__import__('collections').Counter(),ticks=1)
P._io_pass(now=100); ck('bounded queue, drops counted',len(P._q)==3 and P.ST['dropped']==2)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
