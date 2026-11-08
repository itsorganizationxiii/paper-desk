# Window collection tests: restart mid-window, ws drop, deploy during finalize, duplicate finalize, missing data.
import windowlog as W
class Res:
    def __init__(s,u): s.upserted_id=u
class Col:
    def __init__(s): s.d={}
    def update_one(s,q,u,upsert=False):
        k=q['boundary']
        if k in s.d: return Res(None)
        s.d[k]=dict(u['$setOnInsert']); return Res(k)
    def find_one(s,q,p=None): return s.d.get(q['boundary'])
    def find(s,q=None,p=None): return list(s.d.values())
    def insert_many(s,rows,ordered=False):
        for r in rows:
            if r['_id'] in s.d: raise Exception('E11000 duplicate')
            s.d[r['_id']]=r
    def insert_one(s,r): pass
b=1794160800
def mk(ts): return [(t,75000.0,5,75000.0) for t in ts]
mkt=lambda ct: dict(ticker='T%d'%ct,expiration_value='75001',floor_strike=75000,result='yes')
nomkt=lambda ct: dict(ticker='T%d'%ct)
full=range(b-960,b)
ok=True
def ck(name,cond):
    global ok; print(('PASS ' if cond else 'FAIL ')+name); ok&=bool(cond)
r,f=W.finalize(b,b+60,mk(full),mkt); ck('clean window COMPLETE',f and r['status']=='COMPLETE')
# 1. restart halfway: old instance persisted samples, new instance restores them; Render overlap leaves ~20s gap
idx=Col(); W._col['idxsec']=idx
W._sec.clear(); W._pend[:]=mk([t for t in full if t<b-480]); W._flush()
W._sec.clear(); W._sec.extend(mk([t for t in full if t>=b-460]))
n=W._restore(now=b); r,f=W.finalize(b,b+60,list(W._sec),mkt,boot=b-470)
ck('restart mid-window restored and RECOVERED (%d restored)'%n,r['status']=='RECOVERED' and r['complete'] and r['maxGapSec']==21)
# restart with no persisted data: long gap -> INCOMPLETE, not counted
r,f=W.finalize(b,b+60,mk([t for t in full if t>=b-460]),mkt,boot=b-470); ck('restart w/o persisted data INCOMPLETE',r['status']=='INCOMPLETE' and not r['complete'])
# 2. ws disconnect: 8s gap mid-window -> RECOVERED? no restart; gap>5 -> RECOVERED requires <=60 & 95%
r,f=W.finalize(b,b+60,mk([t for t in full if not b-500<=t<b-492]),mkt); ck('ws drop 8s gap counted as RECOVERED, not COMPLETE',r['status']=='RECOVERED')
r,f=W.finalize(b,b+60,mk([t for t in full if not b-40<=t<b-30]),mkt); ck('gap in closing 60s INCOMPLETE',r['status']=='INCOMPLETE')
# 3+4. deploy during finalization / duplicate finalize: two instances save same window
c=Col(); r,f=W.finalize(b,b+60,mk(full),mkt); a1=W.save_window(c,r); a2=W.save_window(c,dict(r)); a3=W.save_window(c,dict(r,status='INCOMPLETE'))
ck('duplicate finalize: one record, first wins',a1 and not a2 and not a3 and len(c.d)==1 and c.d[b]['status']=='COMPLETE')
# 5. missing market data
r,f=W.finalize(b,b+60,[],mkt); ck('no data INVALID, not counted',r['status']=='INVALID' and not r['complete'])
r,f=W.finalize(b,b+60,mk(range(b-300,b)),mkt); ck('partial data INCOMPLETE',r['status']=='INCOMPLETE' and not r['complete'])
r,f=W.finalize(b,b+300,mk(full),nomkt); ck('settlement late: data still COMPLETE, settlement PENDING',r['status']=='COMPLETE' and r['complete'] and r['settlement']=='PENDING')
class C2(Col):
    def update_one(s,q,u,upsert=False):
        if '$set' in u:
            k=q['boundary']
            if s.d.get(k,{}).get('settlement')==q.get('settlement'): s.d[k].update(u['$set'])
            return Res(None)
        return Col.update_one(s,q,u,upsert)
c2=C2(); W.save_window(c2,r); W.backfill_settlement(c2,c2.d[b],b+900,mkt)
ck('late settlement backfilled without touching validity',c2.d[b]['settlement']=='SETTLED' and c2.d[b]['expirationValue']==75001 and c2.d[b]['status']=='COMPLETE')
r,f=W.finalize(b,b+1900,mk(full),nomkt); ck('settlement never posted MISSING, data still valid',r['settlement']=='MISSING' and r['complete'])
# durable writes: failed insert keeps samples queued, retry succeeds, duplicates are acked
class Flaky(Col):
    fail=True
    def insert_many(s,rows,ordered=False):
        if s.fail: s.fail=False; raise Exception('network down')
        Col.insert_many(s,rows)
fl=Flaky(); W._col['idxsec']=fl; W._pend[:]=mk(range(b,b+10))
ck('write failure keeps samples queued',W._flush() is False and len(W._pend)==10)
ck('retry writes them and clears queue',W._flush() and len(W._pend)==0 and len(fl.d)==10)
W._pend[:]=mk(range(b,b+3)); ck('duplicate samples treated as acked',W._flush() and len(W._pend)==0)
# counter counts unique valid only
W._col['idxcmp']=c; c.d[b-900]=dict(boundary=b-900,complete=False,status='INCOMPLETE'); s=W.stats(); ck('counter = unique valid (1)',s['n']==1 and s['excluded']==1)
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
