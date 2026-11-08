# #1070 regression: the stop sale filled on Kalshi (buy YES 1.44 @0.58 == sell NO @0.42) but the bot logged "no fill".
import live as L
fills=[{'ts':1794127962,'action':'buy','side':'yes','count_fp':'1.44','yes_price_dollars':'0.5800','no_price_dollars':'0.4200','fee_cost':'0.024600','order_id':'01a1ba5c'},
       {'ts':1794127662,'action':'sell','side':'no','count_fp':'1.44','yes_price_dollars':'0.3100','no_price_dollars':'0.6900','fee_cost':'0.021600','order_id':'01a1ba57'}]
q,px,fee,o=L.Live.closing_from_fills(fills,'NO','01a1ba57',1794127670)
assert q==1.44 and px==0.42 and fee==0.0246, (q,px,fee)
pnl=round(q*px-fee-1.0152,4); assert abs(pnl-(-0.4350))<0.002, pnl
# #1001 (held YES; sale shows as action=sell side=no, yes price 0.05)
f2=[{'ts':10,'action':'sell','side':'no','count_fp':'3.80','yes_price_dollars':'0.05','no_price_dollars':'0.95','fee_cost':'0.0127','order_id':'x'},
    {'ts':1,'action':'buy','side':'yes','count_fp':'3.80','yes_price_dollars':'0.37','no_price_dollars':'0.63','fee_cost':'0.0621','order_id':'e'}]
q,px,fee,o=L.Live.closing_from_fills(f2,'YES','e',1); assert q==3.8 and px==0.05, (q,px)
print('fill recover OK: #1070 pnl',pnl)
