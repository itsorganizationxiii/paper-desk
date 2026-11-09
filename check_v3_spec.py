"""Offline checks for Spec-aligned v3 staging. No network, no production calls."""
import importlib, math, os, sys, tempfile

ok=True
def chk(name,cond):
    global ok
    print(('PASS ' if cond else 'FAIL ')+name)
    ok&=bool(cond)

# isolate env for import
for k in list(os.environ):
    if k.startswith(('SPEC_','PAPER_STOP','EXPERIMENTAL_SHADOW','SPEC_RESEARCH')):
        os.environ.pop(k)

os.environ['SPEC_EDGE']='0'; os.environ['SPEC_GATES']='0'
os.environ['PAPER_STOP_MODE']='current'; os.environ['EXPERIMENTAL_SHADOW_ONLY']='0'
os.environ['SPEC_RESEARCH_LOG']='1'

import spec as SPEC
importlib.reload(SPEC)

# ---- fee / edge math ----
fee=SPEC.kalshi_fee_1(0.50)
chk('fee at 50c is ceil(7*0.5*0.5)/100 = 0.02', abs(fee-0.02)<1e-12)
bd=SPEC.edge_breakdown(0.60,0.50,bid=0.48,margin=0.02)
chk('raw edge 0.10', abs(bd['edge_raw']-0.10)<1e-9)
chk('after fee', abs(bd['edge_after_fee']-(0.10-fee))<1e-9)
chk('half spread 0.01', abs(bd['half_spread']-0.01)<1e-9)
chk('after margin', abs(bd['edge_after_margin']-(0.10-fee-0.01-0.02))<1e-9)

e,bd2=SPEC.decision_edge(0.60,0.50,bid=0.48,use_spec=False)
chk('flags-off decision edge == raw', abs(e-0.10)<1e-9)
e,bd2=SPEC.decision_edge(0.60,0.50,bid=0.48,use_spec=True)
chk('spec decision edge == after margin', abs(e-bd['edge_after_margin'])<1e-9)

# ---- gates ----
g=SPEC.gate_results(0.55,0.52)
chk('gates ok mid band', g['gates_ok'] and g['price_band_ok'] and g['spread_ok'])
chk('gate ask too low', not SPEC.gate_results(0.05,0.04)['price_band_ok'])
chk('gate ask too high', not SPEC.gate_results(0.95,0.94)['price_band_ok'])
chk('gate spread wide', not SPEC.gate_results(0.50,0.40)['spread_ok'])
chk('gates_block off never blocks', SPEC.gates_block(0.05,0.01,use_gates=False) is False)
chk('gates_block on blocks low ask', SPEC.gates_block(0.05,0.04,use_gates=True) is True)

# ---- paper stop modes (never for live) ----
t=dict(mode='paper',price=0.50,side='YES')
(do,why),cf=SPEC.paper_stop_action(t,0.20,0.40)
chk('current mode fires hard stop', do and 'stop' in why and cf['currentHardStopWouldFire'])
SPEC.PAPER_STOP_MODE='off'
(do,why),cf=SPEC.paper_stop_action(t,0.20,0.40)
chk('off mode suppresses stop but logs counterfactual', (not do) and cf['currentHardStopWouldFire'])
SPEC.PAPER_STOP_MODE='time'
(do,why),cf=SPEC.paper_stop_action(t,0.20,0.40,left_sec=30)
chk('time mode no price stop', not do and cf['currentHardStopWouldFire'])
SPEC.PAPER_STOP_MODE='edgedecay'
(do,why),cf=SPEC.paper_stop_action(t,0.45,0.40,model_q=0.40,ask=0.48)
# after-cost edge deeply negative -> exit
chk('edgedecay exits on negative after-cost edge', do and 'edgedecay' in why)
SPEC.PAPER_STOP_MODE='current'
chk('live trades are not paperish', SPEC.is_paperish(dict(mode='live',shadow=False)) is False)
chk('paper trades are paperish', SPEC.is_paperish(dict(mode='paper')) is True)
chk('shadow live-tagged still paperish', SPEC.is_paperish(dict(mode='live',shadow=True)) is True)

# ---- experimental shadow ----
SPEC.EXPERIMENTAL_SHADOW_ONLY=True
m=SPEC.experimental_force_shadow(dict(signalSource='alt-consensus'))
chk('experimental forced shadow', m.get('shadow') is True and m.get('mode')=='shadow')
SPEC.EXPERIMENTAL_SHADOW_ONLY=False
m2=SPEC.experimental_force_shadow(dict(signalSource='alt-consensus'))
chk('experimental force noop when flag off', 'shadow' not in m2)

# ---- research log non-blocking ----
td=tempfile.mkdtemp()
SPEC._path=None
SPEC.append_research(dict(kind='test',x=1),td)
path=os.path.join(td,'spec_research.jsonl')
chk('research jsonl written', os.path.isfile(path) and 'test' in open(path).read())
# must not raise
SPEC.append_research(dict(kind='bad',obj=object()),td)
chk('research log swallows errors', True)

# ---- size() flags-off parity via light exec of method body ----
# Re-import SPEC with flags off and simulate size math
os.environ['SPEC_EDGE']='0'; os.environ['SPEC_GATES']='0'
importlib.reload(SPEC)
q,ask=0.62,0.50
edge_off,_=SPEC.decision_edge(q,ask,bid=0.49,use_spec=False)
chk('parity raw edge path', abs(edge_off-(q-ask))<1e-12)

os.environ['SPEC_EDGE']='1'; os.environ['SPEC_GATES']='1'; os.environ['SPEC_MARGIN']='0.02'
importlib.reload(SPEC)
edge_on,bd=SPEC.decision_edge(q,ask,bid=0.49,use_spec=True)
chk('spec edge stricter than raw', edge_on < (q-ask) and abs(edge_on-bd['edge_after_margin'])<1e-12)
chk('spec gates block 5c ask', SPEC.gates_block(0.05,0.04) is True)
chk('spec gates allow 55c with 2c spread', SPEC.gates_block(0.55,0.53) is False)

# metal index notes present
chk('gold index note', '1OZGOLD' in SPEC.METAL_INDEX_NOTES['GOLD'])
chk('silver index note', 'SILVER' in SPEC.METAL_INDEX_NOTES['SILVER'])

# compile server.py
import py_compile
try:
    py_compile.compile('server.py',doraise=True)
    py_compile.compile('spec.py',doraise=True)
    chk('py_compile server+spec', True)
except Exception as e:
    chk(f'py_compile failed: {e}', False)

# AST sanity: EXPERIMENTAL_SHADOW_ONLY and PAPER_STOP hooks exist in server
src=open('server.py').read()
chk('server imports spec', 'import spec as SPEC' in src)
chk('server forces experimental shadow', 'EXPERIMENTAL_SHADOW_ONLY' in src and src.count('experimental_force_shadow')>=3)
chk('server paper stop hook', 'paper_stop_action' in src)
chk('server settle research log', 'settlement_row' in src)
chk('server decision research log', 'decision_row' in src)

# size signature accepts bid
chk('size has bid kw', 'def size(side,ask,p_yes,bid=None)' in src)

sys.exit(0 if ok else 1)
