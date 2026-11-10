"""Async, batched MongoDB persistence for state that otherwise lives only in process memory (Nov 9 2026).
Never blocks the trading thread: callers only touch in-memory dicts/deques; one daemon thread does all I/O.
Failures are counted/logged, never raised. Collections stay bounded (TTL index + doc cap + db-size guard).
Collections (in the app's existing Mongo db):
  gap_trades      gap shadow trades + gap PAPER trades (upsert by deterministic _id), TTL TRADE_TTL_DAYS on openedAtDt
  metalgap        /api/metalgap rows   (_id 'm|<ts_ms>', TTL LOG_TTL_DAYS on 'at', cap MAX_LOG_DOCS)
  cryptogap       /api/cryptogap rows  (_id 'c|<ts_ms>', same bounds)
  spec_research   Spec settlement research rows (TTL LOG_TTL_DAYS, cap MAX_LOG_DOCS)
  research_state  small capped snapshots (leadlag/ethlead trigger logs), one doc each
  gap_night       night-mode shadow items (upsert by id), TTL TRADE_TTL_DAYS on openedAtDt"""
import collections, json, os, threading, time
from datetime import datetime, timezone
E=os.environ.get
LOG_TTL_DAYS=int(E('GAPLOG_TTL_DAYS','14')); TRADE_TTL_DAYS=int(E('GAP_TRADE_TTL_DAYS','120'))
MAX_LOG_DOCS=int(E('GAPLOG_MAX_DOCS','60000')); DB_GUARD_MB=float(E('PERSIST_DB_GUARD_MB','470'))
FLUSH_SEC=2.0; MAXQ=20000; BATCH=1000; SNAP_SEC=60.0; MAINT_SEC=600.0
LOG_COLS=('metalgap','cryptogap','spec_research')
ST=dict(enabled=False,upserts=0,inserts=0,dupes=0,errors=0,dropped=0,lastError=None,lastFlush=None,paused=False,
        dbMB=None,loaded={},seeded={},pruned=0,queuedUpserts=0,queuedInserts=0)
_db=[None]; _owner=[lambda:True]; _lk=threading.Lock()
_up={}                                      # (col,_id) -> doc   (coalesced: last write wins)
_ins=collections.defaultdict(lambda:collections.deque(maxlen=MAXQ))
_snaps={}                                   # name -> (fn, last_json)
_started=[False]
def _clean(d):
    try: return json.loads(json.dumps(d,default=str))
    except Exception: return None
def _err(where,e):
    ST['errors']+=1; ST['lastError']=f'{where}: {type(e).__name__}: {str(e)[:160]}'
    try: print('persist err',ST['lastError'],flush=True)
    except Exception: pass
def enabled(): return _db[0] is not None
# ---------- non-blocking producers (safe to call from the trading thread) ----------
def upsert(col,_id,doc):
    if _db[0] is None: return
    try:
        d=dict(doc); d['_id']=_id
        with _lk: _up[(col,_id)]=d
    except Exception as e: _err('upsert-enqueue',e)
def insert(col,_id,row):
    if _db[0] is None: return
    try:
        q=_ins[col]
        if len(q)>=MAXQ: ST['dropped']+=1
        d=dict(row); d['_id']=_id; q.append(d)
    except Exception as e: _err('insert-enqueue',e)
def register_snapshot(name,fn):
    _snaps[name]=[fn,None]
# ---------- writer ----------
def _dt(ts):
    try: return datetime.fromtimestamp(float(ts),timezone.utc)
    except Exception: return datetime.now(timezone.utc)
def flush():
    db=_db[0]
    if db is None: return
    try:
        if not _owner[0](): return   # stale instance during a deploy overlap: never write
    except Exception: pass
    from pymongo import ReplaceOne
    with _lk: items=list(_up.items()); _up.clear()
    bycol=collections.defaultdict(list)
    for (col,_id),d in items:
        d=_clean(d)
        if d is None: continue
        if col in ('gap_trades','gap_night'): d['openedAtDt']=_dt(d.get('openedAt'))
        bycol[col].append(d)
    for col,docs in bycol.items():
        try: db[col].bulk_write([ReplaceOne({'_id':d['_id']},d,upsert=True) for d in docs],ordered=False); ST['upserts']+=len(docs)
        except Exception as e:
            _err('upsert '+col,e)
            with _lk:   # retry next pass unless a newer version was queued meanwhile
                for d in docs: _up.setdefault((col,d['_id']),d)
    for col in list(_ins.keys()):
        q=_ins[col]
        if not q: continue
        if ST['paused'] and col in LOG_COLS: q.clear(); continue
        rows=[]
        while q and len(rows)<BATCH: rows.append(q.popleft())
        now=datetime.now(timezone.utc)
        rows=[r for r in (_clean(x) for x in rows) if r is not None]
        for r in rows: r['at']=_dt(r['ts_ms']/1000.0) if isinstance(r.get('ts_ms'),(int,float)) else now
        try:
            db[col].insert_many(rows,ordered=False); ST['inserts']+=len(rows)
        except Exception as e:
            det=getattr(e,'details',None) or {}
            we=det.get('writeErrors') if isinstance(det,dict) else None
            if we is not None and all(x.get('code')==11000 for x in we):
                ST['dupes']+=len(we); ST['inserts']+=len(rows)-len(we)   # idempotent re-insert (seed / deploy overlap)
            else: _err('insert '+col,e)
    ST['lastFlush']=time.time(); ST['queuedUpserts']=len(_up); ST['queuedInserts']=sum(len(q) for q in _ins.values())
def snapshots():
    for name,ent in list(_snaps.items()):
        try:
            v=_clean(ent[0]())
            j=json.dumps(v,sort_keys=True)
            if j!=ent[1]: ent[1]=j; upsert('research_state',name,dict(items=v,updatedAt=time.time()))
        except Exception as e: _err('snapshot '+name,e)
def maintain():
    db=_db[0]
    if db is None: return
    try:
        s=db.command('dbStats'); mb=(s.get('storageSize',0)+s.get('indexSize',0))/1e6; ST['dbMB']=round(mb,1)
        ST['paused']=mb>=DB_GUARD_MB   # trades/state keep writing; only the high-volume logs pause
    except Exception as e: _err('dbStats',e)
    for col in LOG_COLS:
        try:
            n=db[col].estimated_document_count()
            if n>MAX_LOG_DOCS:
                cut=list(db[col].find({},{'ts_ms':1}).sort('ts_ms',-1).skip(MAX_LOG_DOCS).limit(1))
                if cut: r=db[col].delete_many({'ts_ms':{'$lte':cut[0]['ts_ms']}}); ST['pruned']+=r.deleted_count
        except Exception as e: _err('prune '+col,e)
def ensure_indexes():
    db=_db[0]
    if db is None: return
    for col in LOG_COLS:
        try:
            db[col].create_index('ts_ms')
            try: db[col].create_index('at',expireAfterSeconds=LOG_TTL_DAYS*86400)
            except Exception: db.command('collMod',col,index={'keyPattern':{'at':1},'expireAfterSeconds':LOG_TTL_DAYS*86400})
        except Exception as e: _err('index '+col,e)
    try:
        db['gap_trades'].create_index('openedAt')
        try: db['gap_trades'].create_index('openedAtDt',expireAfterSeconds=TRADE_TTL_DAYS*86400)
        except Exception: db.command('collMod','gap_trades',index={'keyPattern':{'openedAtDt':1},'expireAfterSeconds':TRADE_TTL_DAYS*86400})
    except Exception as e: _err('index gap_trades',e)
    try:
        db['gap_night'].create_index('openedAt')
        try: db['gap_night'].create_index('openedAtDt',expireAfterSeconds=TRADE_TTL_DAYS*86400)
        except Exception: db.command('collMod','gap_night',index={'keyPattern':{'openedAtDt':1},'expireAfterSeconds':TRADE_TTL_DAYS*86400})
    except Exception as e: _err('index gap_night',e)
def _loop():
    ensure_indexes(); ST['enabled']=True; last_snap=0; last_maint=0
    while True:
        time.sleep(FLUSH_SEC)
        try:
            now=time.time()
            if now-last_snap>=SNAP_SEC: last_snap=now; snapshots()
            if now-last_maint>=MAINT_SEC: last_maint=now; maintain()
            flush()
        except Exception as e: _err('loop',e); time.sleep(5)
def start(db,owner=None,thread=True):
    _db[0]=db
    if owner: _owner[0]=owner
    if db is not None and thread and not _started[0]:
        _started[0]=True; threading.Thread(target=_loop,daemon=True,name='gappersist').start()
# ---------- startup reads (synchronous, bounded, never raise) ----------
def load_all(col,flt=None,limit=50000):
    db=_db[0]
    if db is None: return []
    try:
        out=list(db[col].find(flt or {},{'openedAtDt':0,'at':0}).limit(limit)); ST['loaded'][col]=len(out); return out
    except Exception as e: _err('load '+col,e); return []
def load_recent(col,n):
    db=_db[0]
    if db is None: return []
    try:
        out=list(db[col].find({},{'_id':0,'at':0}).sort('ts_ms',-1).limit(n))[::-1]; ST['loaded'][col]=len(out); return out
    except Exception as e: _err('load '+col,e); return []
def load_doc(col,_id):
    db=_db[0]
    if db is None: return None
    try: return db[col].find_one({'_id':_id})
    except Exception as e: _err('load '+col,e); return None
def merge_rows(dq,rows):
    """Merge rows (dicts with ts_ms) into a bounded deque, dedup by ts_ms, oldest->newest. Returns rows added."""
    have={r.get('ts_ms') for r in dq}; add=[r for r in rows if isinstance(r,dict) and r.get('ts_ms') not in have]
    if not add: return []
    allr=sorted(list(dq)+add,key=lambda r:r.get('ts_ms') or 0)
    dq.clear(); dq.extend(allr[-(dq.maxlen or len(allr)):])
    return add
def stats():
    return dict(ST,queuedUpserts=len(_up),queuedInserts=sum(len(q) for q in _ins.values()),ttlDays=LOG_TTL_DAYS,tradeTtlDays=TRADE_TTL_DAYS,maxLogDocs=MAX_LOG_DOCS,dbGuardMB=DB_GUARD_MB)
