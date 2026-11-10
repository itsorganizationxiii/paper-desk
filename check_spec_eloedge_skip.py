"""Offline checks: Spec skips new entries when EloEdge is unavailable (no Coinbase-fallback).
No network, no orders, live/gap/trail/stakes untouched.
"""
import os, re, sys, collections
os.environ['LIVE_TRADING']='0'
import spec as SPEC
import predlog as PL

ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)

# ---- helper behavior ----
chk('402/unavailable/error -> eloedge-unavailable',
    SPEC.elo_entry_skip('unavailable',None,'BTC')=='skip: eloedge-unavailable'
    and SPEC.elo_entry_skip('error: 402',None,'ETH')=='skip: eloedge-unavailable'
    and SPEC.elo_entry_skip('unknown',None,'SOL')=='skip: eloedge-unavailable'
    and SPEC.elo_entry_skip('',None,'GOLD')=='skip: eloedge-unavailable')
chk('live but stale/missing p -> existing no-fresh skip',
    SPEC.elo_entry_skip('live',None,'BTC')=='skip: no fresh EloEdge model_p')
chk('usable EloEdge p never skipped by this gate',
    SPEC.elo_entry_skip('live',0.62,'BTC') is None
    and SPEC.elo_entry_skip('unavailable',0.71,'ETH') is None)
chk('XRP may still use coinbase-proxy (no EloEdge feed)',
    SPEC.elo_entry_skip('unavailable',None,'XRP') is None
    and SPEC.elo_entry_skip('live',None,'XRP') is None)

# ---- server wiring / no fallback entries ----
src=open('server.py').read()
chk('server calls SPEC.elo_entry_skip on the entry path', 'SPEC.elo_entry_skip(est,mp,a)' in src)
chk('entry chain uses helper skip before opening',
    re.search(r'_es=SPEC\.elo_entry_skip\(est,mp,a\)\s*\n\s*if not dirn:.*\n\s*elif _es: sg\[.action.\]=_es', src) is not None)
chk('no coinbase-fallback signalSource assignment',
    "used='coinbase-fallback" not in src and 'coinbase-fallback (EloEdge unavailable)' not in src)
chk('non-XRP mp is None in the else branch skips instead of heur',
    "sg['action']='skip: eloedge-unavailable'; sigs[a]=sg; continue" in src
    and "used='coinbase-proxy (XRP shadow, no EloEdge)'" in src)
chk('open-position monitor still uses heur for settlement/exits',
    "p_yes=mp if mp is not None else (heur(cb) if est!='live' else None)" in src)
chk('signalMode no longer advertises Coinbase fallback entries',
    'no Coinbase-fallback Spec entries' in src and 'FALLBACK: Coinbase momentum+CVD only' not in src)
chk('rules text says skip, not Coinbase-only fallback',
    'no Coinbase fallback' in src and 'Coinbase-only fallback if EloEdge unavailable' not in src)
chk('fallbackInUse forced False (no fallback entries)',
    'fallbackInUse=False' in src)
chk('EloEdge env var still read, not changed',
    "os.environ.get('ELOEDGE_API_KEY'" in src)

# gap / live / trail / stakes / toggle not touched by this patch — source still has them
chk('gap live / limit / trail hooks unchanged in source',
    'GAPLIVE.tick()' in src and 'GAPLIVE.manage_exits' in src and 'gaplimit' in src)

html=open('index.html').read()
chk('dashboard banner says Spec skipping, not FALLBACK (Coinbase only)',
    'EloEdge unavailable — Spec skipping (no Coinbase fallback)' in html
    and 'FALLBACK (Coinbase only)' not in html)

# ---- predlog skip counts surface the new reason ----
PL._win.clear(); PL.ST.update(observed=0,obsErrors=0)
PL.observe({'BTC':dict(action='skip: eloedge-unavailable',direction='YES',kalshi=dict(ticker='KXBTC15M-W',close=int(10**9)+900,yesAsk=0.4,yesBid=0.38,noAsk=0.62,noBid=0.6,strike=1))},'paper',now=10**9+100)
st=PL.stats()
chk('predlog counts skip: eloedge-unavailable',
    (st.get('openSkipReasons') or {}).get('skip: eloedge-unavailable')==1)
chk('predlog.stats exposes openSkipReasons', 'openSkipReasons' in st)

sys.exit(0 if ok else 1)
