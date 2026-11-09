"""Offline persistence checks with a mocked Mongo (no network): gappersist + gapshadow reload + server boot/seed wiring."""
import importlib, json, os, re, tempfile, time, collections
from pymongo.errors import BulkWriteError
import gappersist as P, gapshadow as G, spec as SP
ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)
class Cur:
    def __init__(s,rows): s.rows=rows
    def sort(s,k,d=1):
        if isinstance(k,list): k,d=k[0]
        s.rows=sorted(s.rows,key=lambda r:r.get(k) or 0,reverse=d<0); return s
    def skip(s,n): s.rows=s.rows[n:]; return s
    def limit(s,n): s.rows=s.rows[:n] if n else s.rows; return s
    def __iter__(s): return iter([dict(r) for r in s.rows])
class Col:
    def __init__(s,db,name): s.db=db; s.name=name; s.docs={}; s.indexes=[]
    def _chk(s):
        if s.db.fail: raise RuntimeError('mongo down')
        s.db.calls+=1
    def bulk_write(s,ops,ordered=True):
        s._chk()
        for op in ops: d=dict(op._doc); s.docs[d['_id']]=d
    def insert_many(s,rows,ordered=True):
        s._chk(); errs=[]
        for i,r in enumerate(rows):
            if r['_id'] in s.docs: errs.append(dict(index=i,code=11000,errmsg='dup'))
            else: s.docs[r['_id']]=dict(r)
        if errs: raise BulkWriteError(dict(writeErrors=errs,nInserted=len(rows)-len(errs)))
    def _proj(s,r,proj):
        if not proj: return dict(r)
        if all(v==0 for v in proj.values()): return {k:v for k,v in r.items() if k not in proj}
        return {k:v for k,v in r.items() if k in proj or k=='_id'}
    def find(s,flt=None,proj=None):
        s._chk(); rows=list(s.docs.values())
        for k,v in (flt or {}).items():
            if isinstance(v,dict) and '$lte' in v: rows=[r for r in rows if (r.get(k) or 0)<=v['$lte']]
            else: rows=[r for r in rows if r.get(k)==v]
        return Cur([s._proj(r,proj) for r in rows])
    def find_one(s,flt):
        s._chk(); return next(iter(s.find(flt)),None)
    def create_index(s,k,**kw): s._chk(); s.indexes.append((k,kw))
    def estimated_document_count(s): s._chk(); return len(s.docs)
    def delete_many(s,flt):
        s._chk(); ids=[r['_id'] for r in s.find(flt)]
        for i in ids: s.docs.pop(i,None)
        return type('R',(),{'deleted_count':len(ids)})()
class DB:
    def __init__(s): s.cols={}; s.fail=False; s.calls=0; s.mb=10.0
    def __getitem__(s,k): return s.cols.setdefault(k,Col(s,k))
    def command(s,*a,**k):
        if s.fail: raise RuntimeError('mongo down')
        if a[0]=='dbStats': return dict(storageSize=s.mb*1e6,indexSize=0)
        return {}
db=DB()
P.start(db,owner=lambda:True,thread=False); P.ensure_indexes()
ttl={(c.name,k):kw.get('expireAfterSeconds') for c in db.cols.values() for k,kw in c.indexes if kw}
chk('TTL 14d on metalgap/cryptogap/spec_research',all(ttl.get((c,'at'))==14*86400 for c in ('metalgap','cryptogap','spec_research')))
chk('TTL on gap_trades',ttl.get(('gap_trades','openedAtDt'))==P.TRADE_TTL_DAYS*86400)
# producers are pure in-memory (no db calls on the caller thread)
c0=db.calls; t0=time.perf_counter()
for i in range(2000): P.insert('metalgap','m|%d'%i,dict(kind='metal_gap',ts_ms=1700000000000+i,gapDown=0.1))
for i in range(50): P.upsert('gap_trades','k%d'%(i%5),dict(rule='A',pair='BTC-ETH',closesAt=1.0+i%5,status='open',openedAt=1.0,v=i))
dt=time.perf_counter()-t0
chk('enqueue does no Mongo I/O',db.calls==c0)
chk('enqueue fast (<50ms for 2050 ops)',dt<0.05)
P.flush(); P.flush()
chk('upserts coalesced: 5 docs, last write wins',len(db['gap_trades'].docs)==5 and db['gap_trades'].docs['k4']['v']==49)
chk('openedAtDt datetime stamped for TTL',hasattr(db['gap_trades'].docs['k0']['openedAtDt'],'tzinfo'))
chk('inserts batched (1000/flush) -> 2000 rows',len(db['metalgap'].docs)==2000)
chk('rows get at=datetime from ts_ms',db['metalgap'].docs['m|5']['at'].year>=2023)
for i in range(10): P.insert('metalgap','m|%d'%i,dict(kind='metal_gap',ts_ms=1700000000000+i))
P.insert('metalgap','m|new',dict(kind='metal_gap',ts_ms=1800000000000))
e0=P.ST['errors']; P.flush()
chk('duplicate inserts are idempotent (not errors)',P.ST['errors']==e0 and P.ST['dupes']>=10 and 'm|new' in db['metalgap'].docs)
# failure: logged, not raised, retried
db.fail=True; P.upsert('gap_trades','kf',dict(rule='A',pair='SOL-XRP',closesAt=9.0,openedAt=2.0))
try: P.flush(); raised=False
except Exception: raised=True
chk('mongo failure does not raise',not raised and P.ST['errors']>e0 and P.ST['lastError'])
db.fail=False; P.flush(); chk('failed upsert retried next flush','kf' in db['gap_trades'].docs)
# owner gating (deploy overlap: stale instance never writes)
P._owner[0]=lambda:False; P.upsert('gap_trades','ko',dict(openedAt=1)); P.flush(); chk('non-owner never writes','ko' not in db['gap_trades'].docs)
P._owner[0]=lambda:True; P.flush(); chk('owner writes queued doc','ko' in db['gap_trades'].docs)
# bounds: cap + db-size guard
P.MAX_LOG_DOCS=500; P.maintain()
chk('log collection capped (oldest pruned)',len(db['metalgap'].docs)==500 and 'm|new' in db['metalgap'].docs)
db.mb=P.DB_GUARD_MB+1; P.maintain(); chk('db-size guard pauses log inserts',P.ST['paused'])
P.insert('cryptogap','c|1',dict(kind='crypto_gap',ts_ms=1)); P.upsert('gap_trades','kg',dict(openedAt=1)); P.flush()
chk('paused: logs dropped, trades still written','c|1' not in db['cryptogap'].docs and 'kg' in db['gap_trades'].docs)
db.mb=10; P.maintain(); chk('guard resumes',not P.ST['paused'])
P.MAX_LOG_DOCS=60000
# research snapshots: only written on change
P.register_snapshot('leadlag_events',lambda:[{'a':1}]); P.snapshots(); P.flush(); n1=P.ST['upserts']; P.snapshots(); P.flush()
chk('snapshot written once, skipped when unchanged',db['research_state'].docs['leadlag_events']['items']==[{'a':1}] and P.ST['upserts']==n1)
# merge_rows
dq=collections.deque([{'ts_ms':3}],maxlen=3); add=P.merge_rows(dq,[{'ts_ms':1},{'ts_ms':2},{'ts_ms':3},{'ts_ms':4}])
chk('merge_rows dedup+order+bounded',[r['ts_ms'] for r in dq]==[2,3,4] and len(add)==3)
chk('disabled (no mongo) is a no-op',(P.start(None,thread=False) or True) and P.upsert('x','y',{}) is None and P.load_all('x')==[] and not P.enabled())

# ---- server boot wiring (extracted from server.py, run against the mock) ----
src=open('server.py').read()
blk=src[src.index("SEED_DIR=os.path.join(APP,'gap_seed')"):src.index("try: _persist_boot()")]
def boot(dbx,seed_dir,data_dir):
    global G,SP,P
    P=importlib.reload(P); G=importlib.reload(G); SP=importlib.reload(SP); G.load(data_dir)
    class _MC: database=dbx
    env=dict(os=os,json=json,time=time,GP=P,GAPS=G,SPEC=SP,MCOL=_MC if dbx is not None else None,LV=None,APP=os.path.dirname(seed_dir))
    exec(blk,env); env['SEED_DIR']=seed_dir
    P.start=(lambda f:(lambda d,owner=None,thread=True:f(d,owner,False)))(P.start)  # no background thread in tests
    env['_persist_boot'](); return env
tmp=tempfile.mkdtemp(); sd=os.path.join(tmp,'gap_seed'); os.makedirs(sd)
now=time.time()
seedT=[dict(id='G1',mode='shadow',shadow=True,signalSource='gapshadow',rule='A',pair='BTC-ETH',laggard='ETH',ticker='KXETH-1',side='YES',entry=0.4,stake=1.0,contracts=2.4,closesAt=now-3000,openedAt=now-3400,status='won',pnl=1.4,hold_pnl=1.4),
       dict(id='G2',mode='shadow',shadow=True,signalSource='gapshadow',rule='A',pair='GOLD-SILVER',laggard='SILVER',ticker='KXSILVER-1',side='NO',entry=0.6,stake=1.0,contracts=1.6,closesAt=now+300,openedAt=now-200,status='open')]
json.dump(seedT,open(os.path.join(sd,'gapshadow.json'),'w'))
json.dump([dict(kind='metal_gap',ts_ms=int((now-60)*1000),gapDown=0.2),dict(kind='metal_gap',ts_ms=int((now-30*86400)*1000))],open(os.path.join(sd,'metalgap.json'),'w'))
json.dump([dict(kind='crypto_gap',ts_ms=int((now-60)*1000),pairs={})],open(os.path.join(sd,'cryptogap.json'),'w'))
db2=DB(); env=boot(db2,sd,tempfile.mkdtemp()); P.flush(); P.flush()
chk('boot: seed gapshadow history loaded into /api/gapshadow',len(G.snapshot())==2 and G.snapshot()[0]['pnl']==1.4)
chk('boot: seed imported into Mongo gap_trades',len(db2['gap_trades'].docs)==2)
chk('boot: tick-log seed (within TTL) into ring + Mongo',len(SP.METAL_GAP)==1 and len(db2['metalgap'].docs)==1 and len(db2['cryptogap'].docs)==1)
chk('boot: seed marker written',db2['research_state'].docs.get('seed_import',{}).get('sha'))
# simulate trading after boot: new shadow + paper trades and log rows go to Mongo via sinks
mk=lambda tk,y,c:dict(ticker=tk,close=c,yesBid=round(y-0.01,4),yesAsk=round(y+0.01,4),noBid=round(1-y-0.01,4),noAsk=round(1-y+0.01,4))
C=now+600
G.on_tick({'BTC':mk('KXBTC-9',0.46,C),'ETH':mk('KXETH-9',0.58,C),'SOL':mk('KXSOL-9',0.6,C),'XRP':mk('KXXRP-9',0.55,C)},C-400)
SP.log_metal_gap(mk('KXGOLD-9',0.4,C),mk('KXSILVER-9',0.5,C)); SP.log_crypto_gap({'BTC':mk('b',0.4,C),'ETH':mk('e',0.5,C)})
P.flush()
chk('new gap paper trade persisted',any(d.get('gap') and d.get('mode')=='paper' for d in db2['gap_trades'].docs.values()))
chk('new metal/crypto rows persisted',len(db2['metalgap'].docs)==2 and len(db2['cryptogap'].docs)==2)
nS,nP=len(G.TRADES),len(G.PAPER); G.settle(lambda tk:'yes',now+400); P.flush()
# "deploy": fresh process state, empty /tmp, same Mongo
c1=db2.calls; env=boot(db2,sd,tempfile.mkdtemp()); P.flush()
chk('redeploy: shadow+paper history reloaded from Mongo',len(G.TRADES)==nS and len(G.PAPER)==nP)
chk('redeploy: settled status survives',next(t for t in G.TRADES if t['pair']=='GOLD-SILVER')['status'] in ('won','lost'))
chk('redeploy: metal/crypto rings reloaded',len(SP.METAL_GAP)==2 and len(SP.CRYPTO_GAP)==2)
chk('redeploy: no duplicate docs, seed not re-imported',len(db2['gap_trades'].docs)==nS+nP and P.ST['seeded'].get('metalgap')==0)
chk('redeploy: no reopen of same window',(G.TRADES[-1]['rule'],G.TRADES[-1]['pair'],G.TRADES[-1]['closesAt']) in G._seen)
# boot with Mongo down: no crash, seed still shown
db3=DB(); db3.fail=True
try: boot(db3,sd,tempfile.mkdtemp()); crashed=False
except Exception as e: crashed=e
chk('boot with Mongo failing does not crash and still shows seed',not crashed and len(G.snapshot())==2)
try: boot(None,sd,tempfile.mkdtemp()); crashed=False
except Exception as e: crashed=e
chk('boot without Mongo (file mode) works',not crashed and len(G.snapshot())==2)
chk('server registers leadlag/ethlead snapshots + peak flusher',"register_snapshot('leadlag_events'" in src and "register_snapshot('ethlead_events'" in src and '_peak_flusher' in src)
chk('committed seed present',all(os.path.exists(os.path.join('gap_seed',f)) for f in ('gapshadow.json','metalgap.json','cryptogap.json')))
print('ALL PASS' if ok else 'SOME FAILED'); raise SystemExit(0 if ok else 1)
