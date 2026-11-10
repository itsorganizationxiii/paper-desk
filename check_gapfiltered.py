"""Offline checks for the gap FILTERED paper book (owner t1155u). No network, no orders, live and $3 gap paper untouched."""
import tempfile, time, json
import gapfiltered as F
import gapshadow as G

def chk(msg,ok):
    print(('OK  ' if ok else 'FAIL')+' '+msg)
    if not ok: raise SystemExit(1)

def paper(pair='BTC-ETH', asset='ETH', side='YES', price=0.70, stake=3.0, closes=None, opened=None, ticker='KXETH-F', **k):
    now=time.time(); closes=now+600 if closes is None else closes; opened=now if opened is None else opened
    t=dict(id='GPTEST',mode='paper',shadow=False,gap=True,book='gap-paper',signalSource='gap',rule='A',
           pair=pair,asset=asset,laggard=asset,ticker=ticker,side=side,price=price,entry=price,stake=stake,
           contracts=4.0,closesAt=closes,openedAt=opened,status='open')
    t.update(k); return t

def mk(tk, bid, ask, close, asset_side='YES'):
    return dict(ticker=tk,close=close,yesBid=bid,yesAsk=ask,noBid=round(1-ask,4),noAsk=round(1-bid,4))

F.reset()
# ---- skip / enter rules (pure) ----
chk('BTC-ETH at 55c enters', F.skip_reason(paper(price=0.55)) is None)
chk('BTC-ETH at 70c enters', F.skip_reason(paper(price=0.70)) is None)
chk('entry 54c skipped as cheap-entry', F.skip_reason(paper(price=0.54))=='cheap-entry')
chk('entry 0.5499 skipped', F.skip_reason(paper(price=0.5499))=='cheap-entry')
chk('SOL-XRP pair skipped even at 70c', F.skip_reason(paper(pair='SOL-XRP',asset='SOL',price=0.70))=='sol-xrp')
chk('XRP asset skipped', F.skip_reason(paper(pair='SOL-XRP',asset='XRP',price=0.80,side='NO'))=='sol-xrp')
chk('SOL asset on a mislabeled pair still skipped', F.skip_reason(paper(pair='BTC-ETH',asset='SOL',price=0.70))=='sol-xrp')
chk('$1 default stake and 55c min', F.STAKE==1.0 and abs(F.MIN_ENTRY-0.55)<1e-12)

# ---- bookkeeping on paper signals ----
F.reset(); NOW=1_800_000_000.0; CLOSE=NOW+600
sig=paper(price=0.70,closes=CLOSE,opened=NOW,ticker='KXETH-F')
src=dict(sig)  # copy to prove we do not mutate the paper trade
ch=F.on_tick({'ETH':mk('KXETH-F',0.69,0.70,CLOSE)}, now=NOW, signals=[src])
chk('70c BTC-ETH paper signal fills $1', len(ch)==1 and ch[0]['status']=='filled' and ch[0]['stake']==1.0 and ch[0]['entry']==0.70)
chk('contracts sized from $1 not $3', abs(ch[0]['contracts']-round((1.0-F.fee(1.0/0.70,0.70))/0.70,4))<1e-9)
chk('source paper trade not mutated', src['stake']==3.0 and src['contracts']==4.0 and src.get('book')=='gap-paper')
chk('idempotent: second tick does not duplicate', F.on_tick({'ETH':mk('KXETH-F',0.69,0.70,CLOSE)}, now=NOW+1, signals=[src])==[])

F.reset()
sol=paper(pair='SOL-XRP',asset='SOL',price=0.70,closes=CLOSE,opened=NOW,ticker='KXSOL-F')
ch=F.on_tick({'SOL':mk('KXSOL-F',0.69,0.70,CLOSE)}, now=NOW, signals=[sol])
chk('SOL-XRP is recorded as a skip, not a fill', len(ch)==1 and ch[0]['status']=='skipped' and ch[0]['skipReason']=='sol-xrp' and ch[0].get('contracts') is None)

F.reset()
cheap=paper(price=0.50,closes=CLOSE,opened=NOW,ticker='KXETH-C')
ch=F.on_tick({'ETH':mk('KXETH-C',0.49,0.50,CLOSE)}, now=NOW, signals=[cheap])
chk('sub-55c entry skipped', len(ch)==1 and ch[0]['status']=='skipped' and ch[0]['skipReason']=='cheap-entry')

F.reset()
old=paper(price=0.70,closes=CLOSE,opened=NOW-30,ticker='KXETH-OLD')
chk('stale paper signal (opened >15s ago) is ignored', F.on_tick({'ETH':mk('KXETH-OLD',0.69,0.70,CLOSE)}, now=NOW, signals=[old])==[])

# non-paper / non-A signals ignored
F.reset()
sh=dict(paper(price=0.70,closes=CLOSE,opened=NOW),mode='shadow',gap=False,shadow=True)
chk('shadow signal ignored', F.on_tick({}, now=NOW, signals=[sh])==[])

# ---- trail15 / hold ----
F.reset()
sig=paper(price=0.70,closes=CLOSE,opened=NOW,ticker='KXETH-T',side='YES')
F.on_tick({'ETH':mk('KXETH-T',0.69,0.70,CLOSE)}, now=NOW, signals=[sig])
# bid ramps to 0.86 (arms at 0.85) then falls to 0.75 (= peak-10c) -> should exit
F.on_tick({'ETH':mk('KXETH-T',0.86,0.87,CLOSE)}, now=NOW+10, signals=[])
ch=F.on_tick({'ETH':mk('KXETH-T',0.75,0.76,CLOSE)}, now=NOW+20, signals=[])
ex=[o for o in F.snapshot() if o.get('exit') is not None]
chk('trail exits at bid after +15c arm / 10c giveback', len(ex)==1 and ex[0]['status']=='exited' and ex[0]['exit']==0.75 and ex[0]['peakBid']==0.86)
want=round(ex[0]['contracts']*0.75-F.fee(ex[0]['contracts'],0.75)-1.0,2)
chk('trail pnl = contracts*bid - fee - $1', ex[0]['pnl']==want)

# hold path: never arms, settles as a win
F.reset()
sig=paper(price=0.70,closes=CLOSE,opened=NOW,ticker='KXETH-H')
F.on_tick({'ETH':mk('KXETH-H',0.69,0.70,CLOSE)}, now=NOW, signals=[sig])
F.on_tick({'ETH':mk('KXETH-H',0.72,0.73,CLOSE)}, now=NOW+30, signals=[])  # peak 72c, not armed
ok_settle=F.settle(lambda tk:'yes', now=CLOSE+6)
o=F.snapshot()[0]
chk('hold settle records hold_pnl and uses it as pnl when trail never fired', ok_settle and o['settle']=='yes' and o['hold_pnl']==round(o['contracts']-1.0,2) and o['pnl']==o['hold_pnl'] and o.get('heldToSettle') is True)

# trail exit then settle keeps booked trail pnl and still records hold
F.reset()
sig=paper(price=0.70,closes=CLOSE,opened=NOW,ticker='KXETH-TH')
F.on_tick({'ETH':mk('KXETH-TH',0.69,0.70,CLOSE)}, now=NOW, signals=[sig])
F.on_tick({'ETH':mk('KXETH-TH',0.86,0.87,CLOSE)}, now=NOW+10, signals=[])
F.on_tick({'ETH':mk('KXETH-TH',0.75,0.76,CLOSE)}, now=NOW+20, signals=[])
F.settle(lambda tk:'no', now=CLOSE+6)
o=F.snapshot()[0]
chk('after trail, settle keeps trail pnl and records hold_pnl at $1 loss', o['exit']==0.75 and o['pnl']!=o['hold_pnl'] and o['hold_pnl']==round(0-1.0,2))

# ---- persist / brief / api ----
F.reset()
td=tempfile.mkdtemp(); F.load(td)
chk('file persist path uses gap_filtered.json', F._path[0].endswith('gap_filtered.json'))
sig=paper(price=0.70,closes=CLOSE,opened=NOW,ticker='KXETH-P')
F.SINK[0]=lambda o: sunk.append(o); sunk=[]
F.on_tick({'ETH':mk('KXETH-P',0.69,0.70,CLOSE)}, now=NOW, signals=[sig])
F.settle(lambda tk:'yes', now=CLOSE+6)
chk('sink received open + settle', len(sunk)>=2 and sunk[-1].get('settle')=='yes')
chk('file written on settle', __import__('os').path.exists(__import__('os').path.join(td,'gap_filtered.json')))
items=F.snapshot(); F.reset(); F.merge_in(items)
chk('merge_in restores settled fill', len(F.snapshot())==1 and F.snapshot()[0]['settle']=='yes')
s=F.summary(); b=F.brief(); api=F.api()
chk('summary n/W-L/pnl trail and hold', s['n']==1 and s['wins']==1 and s['trail']['pnl']==s['holdPnl'] and s['stakeUsd']==1.0)
chk('brief has n, W-L, trail / hold', 'n=1' in b and '1-0' in b and 'trail' in b and 'hold' in b)
chk('api exposes summary + items', api['summary']['book']=='gap-filtered' and len(api['items'])==1)
F.reset(); F.load(td)
chk('reload from file restores settled fill', len(F.snapshot())==1 and F.snapshot()[0].get('settle')=='yes')

# running filtered must not change the $3 paper book
nP,stake0=len(G.PAPER), G.PAPER_STAKE
F.reset(); F.on_tick({}, now=NOW, signals=[paper(price=0.70,closes=CLOSE,opened=NOW)])
chk('PAPER_STAKE still $3 and PAPER list unchanged', G.PAPER_STAKE==3.0 and stake0==3.0 and len(G.PAPER)==nP)
print('ALL PASS')
