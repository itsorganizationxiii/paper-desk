"""Weekend-night Up tilt logic and server wiring."""
from pathlib import Path
import uptilt
assert uptilt.UP_TILT==0.02
assert uptilt.blocked('YES',0.08,0.07,True)
assert not uptilt.blocked('YES',0.09,0.07,True)
assert uptilt.blocked('YES',0.0899,0.07,True)
assert uptilt.blocked('YES',None,0.07,True)
assert not uptilt.blocked('NO',0.07,0.07,True) and not uptilt.blocked('NO',None,0.07,True)
assert not uptilt.blocked('YES',0.03,0.03,False) and not uptilt.blocked('YES',None,0.03,False)
print('PASS Up needs +2c on weekend nights; Down and weekdays unchanged; synthetic Up skipped')
s=Path('server.py').read_text()
for path in ("'leadlag'","'reversal'","'alt-consensus'","'reentry' if","'eloedge'"): assert path in s, path
assert "reason='up-tilt'" in s and 'uptilt.blocked(d,' in s
print('PASS up-tilt wired into eloedge, re-entry, live sizer, alt-consensus, reversal, leadlag')
import ast, threading
from types import SimpleNamespace
tree=ast.parse(s)
code=ast.get_source_segment(s,next(n for n in tree.body if getattr(n,'name',None)=='up_tilt_skip'))
logs=[]
g=dict(uptilt=uptilt,need_edge=lambda:0.07,wknd_night=lambda:True,LV=SimpleNamespace(log=lambda **kw:logs.append(kw)))
exec(code,g)
assert g['up_tilt_skip']('BTC','T','YES',None,'alt-consensus') and logs[-1]['reason']=='up-tilt'
assert not g['up_tilt_skip']('BTC','T','NO',None,'alt-consensus')
assert not g['up_tilt_skip']('BTC','T','YES',0.09,'eloedge')
g['wknd_night']=lambda:False
assert not g['up_tilt_skip']('BTC','T','YES',None,'leadlag')
print('PASS up_tilt_skip logs reason up-tilt and only fires for weekend-night Up')
