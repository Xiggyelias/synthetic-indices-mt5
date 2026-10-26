# 🐳 Dokploy Deployment Guide for QUANTA-SYNTH

> **Complete guide for hosting QUANTA-SYNTH on Dokploy (Self-Hosted PaaS alternative to Heroku & Coolify).**

---

## 🛠️ Step 1: Create an Application in Dokploy

1. Open your **Dokploy Dashboard**.
2. Click **Create Application**.
3. Choose **GitHub**:
   * **Repository:** `Xiggyelias/synthetic-indices-mt5`
   * **Branch:** `main`
4. **Build Type:** You can select either:
   * **Nixpacks** *(Dokploy's default — now 100% pre-configured via `nixpacks.toml`)*
   * **Dockerfile** *(Pre-configured via `Dockerfile`)*

---

## ⚙️ Step 2: Configure Application Settings in Dokploy

### 1. General / Networking Settings
* **Port:** `8501`
  > ⚠️ **IMPORTANT:** In Dokploy under the **General** or **Networking** tab, set the **Port** field to **`8501`**. This tells Dokploy's Traefik reverse proxy to route domain traffic to the Streamlit dashboard.
* **Healthcheck Path:** `/_stcore/health`

### 2. Environment Variables Tab
Click on the **Environment** tab in Dokploy and paste your environment variables:

```ini
PORT=8501
PYTHONUNBUFFERED=1

# ── MT5 Credentials ──
MT5_LOGIN=10010627296
MT5_PASSWORD=YourPassword
MT5_SERVER=Deriv-Demo

# ── Portfolio & Strategy ──
SYMBOLS=Volatility 75 Index,Volatility 75 (1s) Index,Volatility 100 Index,Volatility 50 Index,Volatility 25 Index,Volatility 10 Index,Crash 1000 Index,Crash 500 Index,Boom 1000 Index,Boom 500 Index,Step Index
TIMEFRAME=M5
POLL_SECONDS=10
BOT_MAGIC=20260909

# ── Risk Engine ──
AGGRESSIVE_MODE=true
FRACTIONAL_KELLY=0.45
MAX_RISK_PER_TRADE=0.05
MAX_OPEN_TRADES=4

# ── Telegram Notifications ──
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=8490649322:AAFg5O9ljUHvdJpLrQik7UEVZuAFR5uf1aQ
TELEGRAM_CHAT_ID=8348592431
```

### 3. Persistent Volumes Tab (Optional but Recommended)
To preserve calibration and trade logs across redeploys in Dokploy:
Add volume mounts:
* `/app/data`
* `/app/logs`
* `/app/config`

---

## 🚀 Step 3: Deploy

1. Click **Deploy** in Dokploy.
2. Watch the deployment log:
   * It will pull the repository.
   * Nixpacks / Docker will build Python dependencies from `requirements.txt`.
   * It will find the start command:
     `streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0 ...`
   * Dokploy's Traefik will assign your domain/subdomain with free automatic SSL.
3. Access your live institutional dashboard!
