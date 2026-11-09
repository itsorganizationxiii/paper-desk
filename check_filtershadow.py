"""Tests for SHADOW-ONLY entry filters (filtershadow.py) + gap wiring. Pure; no network."""
import importlib, filtershadow as FS, gapshadow as G
R=900.0; C0=1_800_000_000.0
def tr(c,p,st='lost',ca=None): return dict(closesAt=c,pnl=p,status=st,closedAt=ca if ca is not None else c+5)
# skip2L: two consecutive losing rounds -> next round skipped, then reset
ts=[tr(C0,-1),tr(C0+R,-0.5)]
s=FS.skip2l_state(ts,C0+2*R,C0+2*R-600); assert s['skip'], s
assert not FS.skip2l_state(ts,C0+3*R,C0+3*R-600)['skip']
# non-consecutive windows don't count
assert not FS.skip2l_state([tr(C0,-1),tr(C0+2*R,-1)],C0+3*R,C0+3*R-600)['skip']
# win in between resets
assert not FS.skip2l_state([tr(C0,-1),tr(C0+R,0.3,'won'),tr(C0+2*R,-1)],C0+3*R,C0+3*R-600)['skip']
# skipped round never counts; reset after one skipped round
ts=[tr(C0,-1),tr(C0+R,-1),tr(C0+2*R,-1),tr(C0+3*R,-1)]
assert not FS.skip2l_state(ts,C0+4*R,C0+4*R-600)['skip']      # streak after skip is 1 (round 3 only)
ts.append(tr(C0+4*R,-1)); assert FS.skip2l_state(ts,C0+5*R,C0+5*R-600)['skip']
# no future data: an unsettled prior round (closedAt after now) is ignored
ts=[tr(C0,-1),tr(C0+R,-1,ca=C0+R+400)]
assert not FS.skip2l_state(ts,C0+2*R,C0+R+100)['skip'] and FS.skip2l_state(ts,C0+2*R,C0+R+500)['skip']
assert not FS.skip2l_state([tr(C0,-1),dict(closesAt=C0+R,status='open',pnl=None)],C0+2*R,C0+R+100)['skip']
# chop: crosses + prior flip
importlib.reload(FS)
def q(y): return dict(close=C0,yesBid=round(y-0.01,2),yesAsk=round(y+0.01,2))
for i,y in enumerate((0.55,0.45,0.50,0.40,0.60)): FS.observe('BTC',q(y),C0-800+i*10)
st=FS.chop_state('BTC',C0); assert st['crosses']==2 and st['crossChop'] and st['priorFlip'] is None, st
# outcomes: window C0 ends YES (last mid .60 within 90s?) -> no (last obs 760s before close) -> unknown
FS.observe('BTC',q(0.7),C0-30)
FS.observe('BTC',dict(close=C0+R,yesBid=0.3,yesAsk=0.32),C0+10)   # rolls window -> C0 outcome yes
assert FS.outcome('BTC',C0)=='yes'
FS.observe('BTC',dict(close=C0+R,yesBid=0.3,yesAsk=0.32),C0+R-20)
FS.observe('BTC',dict(close=C0+2*R,yesBid=0.5,yesAsk=0.52),C0+R+5)
assert FS.outcome('BTC',C0+R)=='no'
st=FS.chop_state('BTC',C0+2*R); assert st['priorFlip'] is True and st['crosses']==0, st
f=FS.build('spec',[],'BTC',C0+2*R,0.60,700,C0+R+200,spec=True)
assert f['chop65']['choppy'] and not f['chop65']['keep'] and not f['specStrict']['keep'] and f['skip2L']['keep']
f=FS.build('spec',[],'BTC',C0+2*R,0.66,500,C0+R+400,spec=True)
assert f['chop65']['keep'] and f['specStrict']['keep']
f=FS.build('spec',[],'BTC',C0+2*R,0.81,700,C0+R+200,spec=True); assert f['specStrict']['keep']
assert 'specStrict' not in FS.build('gapShadow',[],'BTC',C0+2*R,0.5,700,C0+R+200)
# state round-trip
d=FS.state(); importlib.reload(FS); FS.restore(d,now=C0+R+300); assert FS.outcome('BTC',C0+R)=='no'
# tag never raises / never alters other fields
t=dict(closesAt=C0+2*R,openedAt=C0+R+200,price=0.7,status='open'); b=dict(t)
FS.tag(t,'spec',[],'BTC',0.7,spec=True); assert {k:v for k,v in t.items() if k!='filters'}==b and 'filters' in t
assert FS.tag(dict(),'spec',[],'BTC',0.7) is None
# summary
a=dict(closesAt=C0,pnl=1.0,status='won',filters=dict(skip2L=dict(keep=True),specStrict=dict(keep=False),chop65=dict(keep=True)))
b2=dict(closesAt=C0,pnl=-0.5,status='lost',filters=dict(skip2L=dict(keep=False),specStrict=dict(keep=True),chop65=dict(keep=True)))
sm=FS.summarize(dict(spec=[a,b2],gapShadow=[],gapPaper=[]))
x=sm['summary']['spec']['filters']
assert x['skip2L']['deltaVsBaseline']==0.5 and x['specStrict']['deltaVsBaseline']==-1.0 and x['chop65']['deltaVsBaseline']==0 and len(sm['rounds'])==1
# gap wiring: tag hook runs, a raising hook never blocks the trade
importlib.reload(G); seen=[]
G.FILTER[0]=lambda t,book: seen.append(book)
G.PAPER_ENABLED=False; out=[]
G._add(dict(rule='A',pair='BTC-ETH',closesAt=C0,laggard='ETH',side='YES',entry=0.5,openedAt=C0-600,ticker='X'),out)
assert seen==['shadow'] and len(out)==1
def boom(t,b): raise RuntimeError('x')
G.FILTER[0]=boom; G._add(dict(rule='A',pair='SOL-XRP',closesAt=C0,laggard='XRP',side='YES',entry=0.5,openedAt=C0-600,ticker='Y'),out); assert len(out)==2
print('check_filtershadow OK')
