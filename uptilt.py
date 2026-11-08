"""Weekend-night Up tilt (owner approved Nov 7): live YES entries need 2c more edge Fri 5 PM-Mon 9 AM ET."""
# Off Nov 7 11:26 PM ET at owner request; set 0.02 to re-enable
UP_TILT=0.0

def extra(side, weekend):
    return UP_TILT if side=='YES' and weekend else 0.0

def blocked(side, edge, base_need, weekend):
    # True when a live entry fails the tilted requirement; synthetic-edge paths pass edge=None and are skipped
    x=extra(side,weekend)
    if not x: return False
    return edge is None or edge+1e-9<base_need+x
