# CapitalSense Paper Desk — Streamlit MVP

This is a Streamlit translation of the MAET paper-trading experience. It keeps the concepts visible in the repository and live terminal:

- resettable ₹10,00,000 paper account with margin enforcement;
- NAV, free margin, allocated margin, realised and unrealised P&L with % returns;
- symbol search across 20 popular NSE equities;
- **Upstox API v2** live market data (1-year Analytics Token — no daily login);
- graceful fallback: Upstox → Yahoo Finance (delayed) → offline demo data;
- candles/line chart with SMA, EMA, RSI, MACD, VWAP, and Bollinger Bands;
- configurable chart intervals (1m, 5m, 15m, 30m, 1h, 1D, 1W, 1M);
- market, limit and stop-loss-limit orders;
- bracket stop-loss/take-profit exits;
- persistent local SQLite account, positions, orders and fills;
- full order history tab (filled, cancelled, rejected);
- no broker connection and no real-money execution.

## Setup

### 1. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 2. Configure Upstox API (optional — app works without this)

1. Open an Upstox demat account (free) at [upstox.com](https://upstox.com).
2. Go to [Upstox Developer Portal](https://upstox.com/developer/api-documentation/).
3. Create an App.
4. Navigate to the **Analytics** tab → **Generate Token** → **Confirm**.
5. Copy `.env.example` to `.env` and paste your token:

```powershell
copy .env.example .env
# Edit .env and add your UPSTOX_ACCESS_TOKEN
```

The Analytics Token is **read-only** and valid for **1 year** — no daily login, no TOTP, no password needed.

### 3. Run

```powershell
streamlit run app.py
```

### Data Source Fallback

| Priority | Source | When used |
|---|---|---|
| 🟢 Upstox Live | `UPSTOX_ACCESS_TOKEN` in `.env` | Real-time during market hours |
| 🟡 Yahoo Finance | `yfinance` installed, no Upstox token | ~15 min delayed quotes |
| 🔴 Demo data | Always available | Deterministic synthetic data |

The active source is displayed in the sidebar and chart header.

### Rate Limits (Upstox)

| Endpoint | Limit |
|---|---|
| Market data / LTP | 50 req/sec (batch up to 500 symbols) |
| Historical candles | 50 req/sec |
| WebSocket feed | 5,000 symbols per connection |

The app caches historical data (`@st.cache_data(ttl=300)`) so multiple users sharing the same deployment share cache hits.

## Technical Indicators

| Indicator | Description |
|---|---|
| SMA 20 | Simple Moving Average (20 periods) |
| EMA 50 | Exponential Moving Average (50 periods) |
| RSI 14 | Relative Strength Index — Wilder's smoothing |
| MACD | Moving Average Convergence Divergence (12, 26, 9) |
| VWAP | Volume-Weighted Average Price |
| BB | Bollinger Bands (SMA ± 2σ, 20 periods) |

## Mapping from MAET

| MAET concept | Streamlit MVP |
|---|---|
| `paper_accounts` / paper ledgers | `account` table |
| `paper_orders` | `orders` table |
| `paper_fills` | `fills` table |
| `paper_positions` | `positions` table |
| quote-backed execution | `process_quote()` and `place_order()` |
| frontend terminal | `app.py` |

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI — charts, order ticket, metrics, tabs |
| `market_data.py` | Upstox/Yahoo/demo data adapter + technical indicators |
| `paper_engine.py` | SQLite-backed paper trading engine (orders, fills, positions, margin) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Credential template |
| `.gitignore` | Excludes secrets, DB, caches |

The local SQLite file is created beside `app.py`. For a multi-user deployment, replace this local store with Supabase and add authentication before exposing the app publicly.

This is a paper-execution research tool, not a broker or investment-advice system.
