# Paper Desk v3 Spec stage — deploy steps (ONLY when user says go)

**Do not run these until the user explicitly says deploy/go.** Production freeze stays in force until then.

## Preconditions
1. Confirm the 300-window reconstruction study is COMPLETE (or user explicitly authorizes pause/cutover).
2. Confirm Live Mode remains paused; do not call `/api/status` (triggers `live.status()` → `balance()` → possible `LIVE_SWEEP`).
3. Archive current production state/logs if desired (existing Mongo/file data must not be deleted).
4. This patch requires a Render redeploy (shared paper+live process restart). Expect a brief interruption of the collector loop.

## Env flags to set on Render (recommended first deploy)
- `EXPERIMENTAL_SHADOW_ONLY=1` (default in code; keep on)
- `SPEC_EDGE=1`
- `SPEC_GATES=1`
- `SPEC_MARGIN=0.02`
- `PAPER_STOP_MODE=off` (or `time` / `edgedecay` for paper A/B; live hard stop unchanged)
- `SPEC_RESEARCH_LOG=1`
- Leave `LIVE_TRADING` / pause as currently configured (live stays paused)

Flags-off (`SPEC_EDGE=0`, `SPEC_GATES=0`, `PAPER_STOP_MODE=current`) restores prior sizing/gates/stop behavior; research JSONL logging remains additive.

## Deploy sequence (fail-stop: any failed pre-step aborts; no second push without user OK)
1. On the staging copy branch `v3-spec-stage`, re-run: `python -m py_compile server.py spec.py && python check_v3_spec.py`
2. Final local archive: `git -C /workspace/paper-desk-v3-stage diff f0e5fbc > /workspace/paper-desk-v3-stage/v3.patch` (refresh)
3. Merge/cherry-pick onto the deploy clone only after user OK.
4. Push **one** commit to `main` (triggers Render). No force-push.
5. Verify **only**: `GET /healthz` and Render logs. **Do not** hit `/api/status` or open the dashboard (it polls `/api/status`).
6. Confirm research spool path `spec_research.jsonl` under `DATA_DIR` is appending (logs), and experimental paths show `experimentalShadowForced` / shadow mode.
7. If anything looks wrong: leave live paused, report, wait for user — do not auto-revert/push again.

## Rollback
- Set `SPEC_EDGE=0`, `SPEC_GATES=0`, `PAPER_STOP_MODE=current` (no code push needed for filter rollback), or redeploy prior commit `f0e5fbc` if a code rollback is required (user OK first).
