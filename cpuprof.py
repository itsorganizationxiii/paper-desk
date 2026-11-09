"""Read-only CPU / thread profiling for /api/cpuprof (owner t1025s3058). No secrets, no trading effects.
- process + per-thread CPU from /proc (delta over a short window), cgroup CPU/memory limits and throttling
- on-demand sampling profiler: sys._current_frames() every ~10ms for N seconds (max 5s), one run at a time
- cheap stage timers (STAGES) that server code can wrap around tick stages / saves / scans
"""
import os, sys, time, threading, collections
CLK=os.sysconf('SC_CLK_TCK') if hasattr(os,'sysconf') else 100
APP=os.path.dirname(os.path.abspath(__file__))
STAGES={}; _slk=threading.Lock(); _run=threading.Lock()
LAST={}
def stage(name,dt,size=None):
    with _slk:
        e=STAGES.setdefault(name,dict(n=0,total=0.0,max=0.0,last=0.0,bytes=None))
        e['n']+=1; e['total']+=dt; e['last']=dt; e['max']=max(e['max'],dt)
        if size is not None: e['bytes']=size
class timed:
    def __init__(s,name,size_fn=None): s.name=name; s.size_fn=size_fn
    def __enter__(s): s.t=time.perf_counter(); return s
    def __exit__(s,*a):
        sz=None
        if s.size_fn:
            try: sz=s.size_fn()
            except Exception: sz=None
        stage(s.name,time.perf_counter()-s.t,sz); return False
def stages():
    with _slk:
        return {k:dict(n=v['n'],avgMs=round(v['total']/v['n']*1000,2) if v['n'] else None,maxMs=round(v['max']*1000,1),
                       lastMs=round(v['last']*1000,2),totalSec=round(v['total'],1),bytes=v['bytes']) for k,v in sorted(STAGES.items(),key=lambda kv:-kv[1]['total'])}
def _read(p):
    try:
        with open(p) as f: return f.read().strip()
    except Exception: return None
def _ticks(stat):
    # /proc/.../stat: fields after the ')' ; utime=14, stime=15 (1-based overall)
    try: r=stat[stat.rindex(')')+2:].split(); return int(r[11])+int(r[12])
    except Exception: return None
def proc_ticks(): return _ticks(_read('/proc/self/stat') or '')
def thread_ticks():
    out={}
    try:
        for tid in os.listdir('/proc/self/task'):
            t=_ticks(_read(f'/proc/self/task/{tid}/stat') or '')
            if t is not None: out[int(tid)]=(t,_read(f'/proc/self/task/{tid}/comm'))
    except Exception: pass
    return out
def cgroup():
    d={}
    cm=_read('/sys/fs/cgroup/cpu.max')
    if cm:
        q,p=(cm.split()+['100000'])[:2]; d['cpuMax']=cm; d['cpuLimitCores']=None if q=='max' else round(int(q)/int(p),3)
    else:
        q=_read('/sys/fs/cgroup/cpu/cpu.cfs_quota_us'); p=_read('/sys/fs/cgroup/cpu/cpu.cfs_period_us')
        if q and p: d['cpuLimitCores']=None if int(q)<0 else round(int(q)/int(p),3)
    st=_read('/sys/fs/cgroup/cpu.stat') or _read('/sys/fs/cgroup/cpu/cpu.stat')
    if st:
        kv=dict(l.split() for l in st.splitlines() if len(l.split())==2)
        d['cpuStat']={k:int(v) for k,v in kv.items() if k in ('usage_usec','user_usec','system_usec','nr_periods','nr_throttled','throttled_usec','throttled_time')}
    mm=_read('/sys/fs/cgroup/memory.max') or _read('/sys/fs/cgroup/memory/memory.limit_in_bytes')
    mc=_read('/sys/fs/cgroup/memory.current') or _read('/sys/fs/cgroup/memory/memory.usage_in_bytes')
    try: d['memLimitMB']=None if (not mm or mm=='max' or int(mm)>1<<60) else round(int(mm)/2**20,1)
    except Exception: d['memLimitMB']=mm
    try: d['memUsedMB']=round(int(mc)/2**20,1) if mc else None
    except Exception: pass
    rss=_read('/proc/self/status')
    if rss:
        for l in rss.splitlines():
            if l.startswith('VmRSS:'): d['rssMB']=round(int(l.split()[1])/1024,1)
            if l.startswith('Threads:'): d['threads']=int(l.split()[1])
    d['cpuCount']=os.cpu_count()
    la=_read('/proc/loadavg'); d['loadavg']=la.split()[:3] if la else None
    return d
def _fname(f):
    co=f.f_code; fn=co.co_filename
    short=os.path.relpath(fn,APP) if fn.startswith(APP) else ('/'.join(fn.split('/')[-2:]))
    return f'{short}:{co.co_name}',fn.startswith(APP)
def profile(sec=3.0,interval=0.01,top=20):
    """Sample all threads for `sec` seconds. Returns CPU deltas + top functions. One run at a time."""
    sec=max(0.5,min(float(sec),5.0)); interval=max(0.005,float(interval))
    if not _run.acquire(blocking=False): return dict(error='profile already running')
    try:
        names={th.native_id:th.name for th in threading.enumerate() if getattr(th,'native_id',None)}
        ident2tid={th.ident:th.native_id for th in threading.enumerate()}
        me=threading.get_ident()
        p0=proc_ticks(); t0=thread_ticks(); w0=time.time(); cs0=(cgroup().get('cpuStat') or {})
        leaf=collections.Counter(); own=collections.Counter(); perth=collections.defaultdict(collections.Counter); n=0
        end=time.time()+sec
        while time.time()<end:
            for ident,fr in sys._current_frames().items():
                if ident==me: continue
                tn=names.get(ident2tid.get(ident),str(ident))
                lf,_=_fname(fr); leaf[lf]+=1
                f=fr; ow=None
                while f is not None:
                    nm,mine=_fname(f)
                    if mine and not nm.startswith('cpuprof.py'): ow=nm; break
                    f=f.f_back
                if ow: own[ow]+=1; perth[tn][ow]+=1
                else: perth[tn][lf]+=1
            n+=1; time.sleep(interval)
        wall=time.time()-w0; p1=proc_ticks(); t1=thread_ticks(); cs1=(cgroup().get('cpuStat') or {})
        names={th.native_id:th.name for th in threading.enumerate() if getattr(th,'native_id',None)}
        thr=[]
        for tid,(tk,comm) in t1.items():
            d=tk-(t0.get(tid,(tk,None))[0])
            thr.append(dict(tid=tid,name=names.get(tid) or comm,cpuPct=round(d/CLK/wall*100,1)))
        thr.sort(key=lambda x:-x['cpuPct'])
        thrc={}
        for th in thr[:8]:
            c=perth.get(th['name']); thrc[th['name']]=[[k,v] for k,v in c.most_common(5)] if c else []
        dcs={k:cs1.get(k,0)-cs0.get(k,0) for k in cs1}
        out=dict(at=time.time(),windowSec=round(wall,2),samples=n,
                 processCpuPct=round((p1-p0)/CLK/wall*100,1) if (p0 is not None and p1 is not None) else None,
                 cgroup=cgroup(),cgroupDelta=dcs or None,
                 throttledPct=round(dcs.get('throttled_usec',0)/1e6/wall*100,1) if 'throttled_usec' in dcs else None,
                 topThreads=thr[:10],threadHotspots=thrc,
                 topOwnFunctions=[[k,v,round(v/max(n,1)*100,1)] for k,v in own.most_common(top)],
                 topLeafFunctions=[[k,v,round(v/max(n,1)*100,1)] for k,v in leaf.most_common(top)],
                 stages=stages())
        LAST.clear(); LAST.update(out); return out
    finally: _run.release()
