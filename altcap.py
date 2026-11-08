"""History-derived live alt-consensus entry limits; paper/shadow never count."""
import time

ALT_MAX_ENTRIES=1
ALT_WINDOW_SEC=3600
ALT_LOSS_PAUSE_SEC=3600

def state(trades,now=None):
    now=time.time() if now is None else now
    live=[t for t in trades if t.get('mode')=='live' and not t.get('shadow')
          and (t.get('altConsensus') or t.get('signalSource')=='alt-consensus')]
    recent=[t for t in live if float(t.get('openedAt') or 0)>now-ALT_WINDOW_SEC]
    latest=max(live,key=lambda t:float(t.get('openedAt') or 0),default=None)
    closed=[t for t in live if t.get('status') in ('exited','won','lost') and t.get('closedAt') is not None]
    last=max(closed,key=lambda t:float(t['closedAt']),default=None)
    loss=last if last and float(last.get('pnl') or 0)<0 else None
    loss_until=float(loss['closedAt'])+ALT_LOSS_PAUSE_SEC if loss else 0
    entry_until=float(latest.get('openedAt') or 0)+ALT_WINDOW_SEC if recent else 0
    until=max(loss_until,entry_until)
    reasons=[]
    if len(recent)>=ALT_MAX_ENTRIES: reasons.append('one live alt-consensus entry per rolling 60 minutes')
    if loss_until>now: reasons.append('most recent closed live alt-consensus trade lost; pause 60 minutes from close')
    return dict(maxEntries=ALT_MAX_ENTRIES,rollingSec=ALT_WINDOW_SEC,lossPauseSec=ALT_LOSS_PAUSE_SEC,
                entriesLastHour=len(recent),blocked=bool(reasons),blockedUntil=until if reasons else None,
                reason='; '.join(reasons) if reasons else None,lastLiveEntryAt=latest.get('openedAt') if latest else None,
                lastClosedTradeId=last.get('id') if last else None,lastClosedPnl=last.get('pnl') if last else None,
                lastLossAt=loss.get('closedAt') if loss else None)
