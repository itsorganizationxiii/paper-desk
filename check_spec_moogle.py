"""Offline checks: Spec uses MoogleEdge p when EloEdge is missing/stale (paper only).
Fake fresh MoogleEdge -> may trade, pSource recorded; stale/missing -> skip.
No network, no orders. Does not touch gaplive, stakes, paper books, shadows, or the live toggle.
"""
import os, re, sys, time, py_compile
os.environ['LIVE_TRADING']='0'
import spec as SPEC

ok=True
def chk(n,c):
    global ok; print(('PASS ' if c else 'FAIL ')+n); ok&=bool(c)

NOW=1_800_000_000
TK='KXBTC15M-W'

def row(p, age, ticker=TK, use_t=True):
    r=dict(p=p, ticker=ticker, lagSec=age)
    if use_t: r['t']=NOW-age
    return r

def decide(dirn, elo_p, moogle_row, now=NOW, ticker=TK):
    """Mirror of the Spec entry p-gate: resolve p, skip or trade, record pSource."""
    mo=SPEC.moogle_yes_p(moogle_row, now=now, ticker=ticker)
    p,src=SPEC.resolve_spec_p(elo_p, mo)
    skip=SPEC.spec_entry_skip(p, src)
    rec=dict(specP=p, pSource=src)
    if not dirn:
        return 'skip: momentum/CVD disagree', rec
    if skip:
        return skip, rec
    if p is not None and (p>0.5)!=(dirn=='YES'):
        return f'skip: {SPEC.spec_p_label(src)} disagrees (model_p {p})', rec
    return 'trade', rec

# ---- helper: fresh MoogleEdge -> trade, pSource recorded ----
act,rec=decide('YES', None, row(0.62, 10))
chk('fresh MoogleEdge (10s) YES agrees -> trade', act=='trade')
chk('fresh MoogleEdge records pSource=moogleedge', rec['pSource']=='moogleedge')
chk('fresh MoogleEdge records the p value', rec['specP']==0.62)

act,rec=decide('NO', None, row(0.30, 0))
chk('fresh MoogleEdge NO agrees (p=0.30) -> trade', act=='trade' and rec['pSource']=='moogleedge' and rec['specP']==0.30)

# ---- stale / missing -> skip ----
act,rec=decide('YES', None, row(0.80, 60.001))
chk('MoogleEdge stale >60s -> skip moogleedge-unavailable', act=='skip: moogleedge-unavailable' and rec['pSource'] is None and rec['specP'] is None)

act,rec=decide('YES', None, row(0.80, 60.0))
chk('MoogleEdge age exactly 60s still fresh', act=='trade' and rec['pSource']=='moogleedge')

act,rec=decide('YES', None, row(0.80, 120))
chk('MoogleEdge 120s stale -> skip', act=='skip: moogleedge-unavailable')

act,rec=decide('YES', None, None)
chk('missing MoogleEdge (and no Elo) -> skip', act=='skip: moogleedge-unavailable' and rec['pSource'] is None)

act,rec=decide('YES', None, dict(ticker=TK, t=NOW-5))  # no p
chk('MoogleEdge row without p -> skip', act=='skip: moogleedge-unavailable')

act,rec=decide('YES', None, row(0.70, 5, ticker='KXETH15M-W'))
chk('MoogleEdge wrong ticker -> skip', act=='skip: moogleedge-unavailable')

# lagSec-only (no t), same 60s rule
act,rec=decide('YES', None, dict(p=0.66, ticker=TK, lagSec=20), now=NOW)
chk('fresh lagSec=20 without t -> trade', act=='trade' and rec['pSource']=='moogleedge' and rec['specP']==0.66)
act,rec=decide('YES', None, dict(p=0.66, ticker=TK, lagSec=61), now=NOW)
chk('stale lagSec=61 without t -> skip', act=='skip: moogleedge-unavailable')

# disagree
act,rec=decide('YES', None, row(0.40, 5))
chk('fresh MoogleEdge disagrees with YES -> skip', act.startswith('skip: MoogleEdge disagrees') and rec['pSource']=='moogleedge' and rec['specP']==0.40)

# ---- prefer EloEdge if fresh ----
act,rec=decide('YES', 0.71, row(0.55, 5))
chk('fresh EloEdge preferred over MoogleEdge', act=='trade' and rec['pSource']=='eloedge' and rec['specP']==0.71)

act,rec=decide('NO', 0.71, row(0.20, 5))
chk('fresh EloEdge disagree wins even if Moogle agrees', act.startswith('skip: EloEdge disagrees') and rec['pSource']=='eloedge')

act,rec=decide('YES', None, row(0.71, 5))
chk('no EloEdge falls through to MoogleEdge', act=='trade' and rec['pSource']=='moogleedge' and rec['specP']==0.71)

chk('resolve_spec_p labels',
    SPEC.resolve_spec_p(0.6, 0.9)==(0.6,'eloedge')
    and SPEC.resolve_spec_p(None, 0.9)==(0.9,'moogleedge')
    and SPEC.resolve_spec_p(None, None)==(None, None))
chk('spec_entry_skip only when p is missing',
    SPEC.spec_entry_skip(0.6,'moogleedge') is None
    and SPEC.spec_entry_skip(None,None)=='skip: moogleedge-unavailable')
chk('60s stale constant matches live-gap rule', SPEC.SPEC_P_STALE_SEC==60.0)
chk('moogle_yes_p_for returns None when CUR empty', SPEC.moogle_yes_p_for('BTC','KXBTC15M-W', now=NOW) is None)

# ---- server / dashboard wiring (Spec paper path only) ----
src=open('server.py').read()
html=open('index.html').read()
chk('server resolves Spec p via SPEC.resolve_spec_p + moogle_yes_p_for',
    'SPEC.resolve_spec_p(mp, SPEC.moogle_yes_p_for(a, k.get(\'ticker\')))' in src
    and 'SPEC.resolve_spec_p(fresh(t[\'ticker\']), SPEC.moogle_yes_p_for(a, t[\'ticker\']))' in src)
chk('entry chain uses spec_entry_skip, not elo_entry_skip',
    'SPEC.spec_entry_skip(_sp,_psrc)' in src and 'SPEC.elo_entry_skip(est,mp,a)' not in src)
chk('opened Spec trades record pSource and specP',
    'pSource=_psrc,specP=_sp' in src and "sg['specP']=_sp; sg['pSource']=_psrc" in src)
chk('signalSource is coinbase+moogleedge when not Elo',
    "used='coinbase+eloedge' if _psrc=='eloedge' else 'coinbase+moogleedge'" in src)
chk('no Coinbase heur fallback on Spec exits',
    "p_yes=mp if mp is not None else (heur(cb) if est!='live' else None)" not in src
    and "used='coinbase-proxy (XRP shadow, no EloEdge)'" not in src)
chk('TP-hold / flip / lock use SPEC.spec_p_label',
    '{SPEC.spec_p_label(psrc)} p={qs}' in src
    and '{SPEC.spec_p_label(psrc)} p={q:.3f}' in src)
chk('rules prefer EloEdge else MoogleEdge, no Coinbase fallback',
    'prefer fresh EloEdge, else MoogleEdge p' in src
    and 'no Coinbase fallback' in src
    and 'Coinbase-only fallback if EloEdge unavailable' not in src)
chk('signalMode advertises MoogleEdge when Elo is down',
    'Coinbase momentum+CVD + MoogleEdge p' in src
    and 'SKIP: MoogleEdge unavailable — no Coinbase-fallback Spec entries' in src)
chk('LIVE_STRATEGIES default stays gap (Spec paper only)',
    "E('LIVE_STRATEGIES','gap')" in open('gaplive.py').read()
    and "return GL.strategy_live(name,LV)" in src)
chk('Spec live order path still gated on live_for(spec) — default off',
    "if live_for('spec') and not shd:" in src
    and "else:\n                with lock: t=pt.openTrade(a,dirn,ask,mp,k['strike'],k['close'],k['ticker'],meta)" in src)
chk('dashboard says Spec (MoogleEdge), not EloEdge removed/paused',
    'Spec (MoogleEdge)' in html
    and 'Spec paused (EloEdge removed)' not in html
    and 'FALLBACK (Coinbase only)' not in html)

# gaplive / session-4 files must be byte-identical to this checkout's committed copies
import subprocess
diff=subprocess.check_output(['git','status','--porcelain','gaplive.py','check_gaplive_regimebar.py','check_gaplive_session4.py'], text=True)
chk('gaplive.py and session-4/regimebar checks unmodified', diff.strip()=='')

py_compile.compile('spec.py', doraise=True)
py_compile.compile('server.py', doraise=True)
chk('py_compile spec+server', True)

sys.exit(0 if ok else 1)
