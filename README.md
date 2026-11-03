# Paper Desk (PAPER ONLY)

Paper-trading simulator for Kalshi 15-minute BTC/ETH markets with a mobile dashboard.
**It never places real orders.** Kalshi is read only for prices and results.

- Signals: Coinbase Exchange 15m momentum + taker volume delta (CVD), EloEdge `model_p` (`/api/v1/windows`), Coinbase-only fallback if EloEdge is unavailable.
- Sizing: quarter Kelly, max 8% of balance, min edge 3%.
- Exits: sell at bid if momentum/CVD and EloEdge flip against the position with >=3% exit edge, or stop when mark <= 40% of entry.
- Endpoints: `/` dashboard (polls every 3s), `/api/status` JSON, `/healthz`.

## Env vars
| Var | Purpose |
|---|---|
| `PORT` | Set by Render automatically (default 8080 locally) |
| `ELOEDGE_API_KEY` | EloEdge key. Set it in the Render dashboard; never commit it |
| `DATA_DIR` | Where `trades.json` lives (default: app dir) |
| `START_BALANCE` | Starting paper balance (default 10) |

On first start, if `DATA_DIR/trades.json` is missing, it is seeded from `seed_trades.json`.
Render's free plan has an **ephemeral disk**: each redeploy or restart resets the state back to the seed. The free plan also sleeps after ~15 min with no traffic, which pauses the trading loop; keeping the dashboard open keeps it awake.

## Deploy on Render
1. Push this folder to a GitHub repo (public or private), e.g.:
   `git remote add origin https://github.com/<you>/paper-desk.git && git push -u origin main`
2. Render dashboard → **New** → **Blueprint** → pick the repo (it reads `render.yaml`).
   Or **New → Web Service** → pick the repo → Runtime Python, Build `pip install -r requirements.txt`, Start `python3 server.py`, Plan Free.
3. When asked, enter `ELOEDGE_API_KEY` (Environment tab).
4. Open `https://paper-desk-xxxx.onrender.com` on your iPhone → Share → Add to Home Screen.

## Local
`ELOEDGE_API_KEY=... python3 server.py` → http://localhost:8080
