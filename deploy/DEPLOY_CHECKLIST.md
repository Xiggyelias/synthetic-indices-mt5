# 🌐 QUANTA-SYNTH Production Deployment Runbook

> **Complete operational checklist and step-by-step setup guide for hosting QUANTA-SYNTH & QUANTUM BRAIN 24/7/365.**

---

## 📋 1. What You Need (Credentials & Configuration Checklist)

Before launching on your host/VPS, have these items ready. You will paste them into `.env` (using [`deploy/.env.production.template`](file:///c:/Users/emmanuel/Desktop/files%20(1)/deploy/.env.production.template)):

| Parameter | Description | Where to Get It | Example / Default |
| :--- | :--- | :--- | :--- |
| **`MT5_LOGIN`** | Deriv MT5 Account Number | Deriv.com Dashboard → CFDs → MT5 | `10293847` |
| **`MT5_PASSWORD`** | Deriv MT5 Account Password | Password you created for the MT5 account | `YourSecretPassword` |
| **`MT5_SERVER`** | Deriv Broker Server | Deriv MT5 account details card | `Deriv-Demo` *(or `Deriv-Server`)* |
| **`MT5_TERMINAL_PATH`** | Location of MT5 `terminal64.exe` | Default Windows installation path | `C:\Program Files\MetaTrader 5\terminal64.exe` |
| **`TELEGRAM_ENABLED`** | Turn on live mobile alerts | Set to `true` or `false` | `true` |
| **`TELEGRAM_BOT_TOKEN`** | Telegram Bot API Token | Message `@BotFather` on Telegram (`/newbot`) | `7123456789:AAH...` |
| **`TELEGRAM_CHAT_ID`** | Your personal Telegram user ID | Message `@userinfobot` or run chat ID finder | `8348592431` |
| **`DERIV_API_TOKEN`** | Deriv WebSocket API Token | Deriv.com → Settings → API Token *(Scope: Read, Trade)* | *(Optional for QB2)* |

---

## 🖥️ 2. Recommended Hosting Specifications

* **Operating System:** Windows Server 2022 / 2025, or Windows 10/11 Pro VPS.
  > **Why Windows?** MetaTrader 5's official Python C-API requires a native Windows environment to execute trades with microsecond latency.
* **Hardware:**
  * CPU: 2 vCPUs minimum (3.0 GHz+ recommended)
  * RAM: 4 GB RAM minimum
  * Disk: 20 GB+ SSD
* **Recommended VPS Providers:**
  * Kamatera, ForexVPS, Contabo, AWS Lightsail (Windows instance), OVHcloud.

---

## 🚀 3. Step-by-Step Deployment Guide (Once on the Host)

### Step 1: Install Prerequisites on the Host

1. **Install Python 3.10, 3.11, or 3.12:**
   * Download from [python.org](https://www.python.org/downloads/).
   * ⚠️ **CRITICAL:** Check the box **"Add Python to PATH"** on the first installation screen!
2. **Install MetaTrader 5:**
   * Download the Deriv MT5 installer directly from Deriv.com and install to standard path (`C:\Program Files\MetaTrader 5\`).

---

### Step 2: Clone or Copy the Repository

Extract or clone the project folder onto the host (e.g. `C:\Users\Administrator\Desktop\quanta-synth`).

Open PowerShell or Command Prompt inside the folder and install dependencies:
```cmd
pip install -r requirements-bot.txt
```

---

### Step 3: Configure Your Production `.env`

Copy the production template:
```cmd
copy deploy\.env.production.template .env
```
Open `.env` in Notepad and insert your **Deriv MT5 Login, Password, Server, and Telegram details** (from Section 1).

---

### Step 4: Authorize MT5 Terminal (First-Time Setup)

1. Open MetaTrader 5 manually on the VPS desktop.
2. Click **File → Login to Trade Account**:
   * Enter your Login ID, Password, and select `Deriv-Demo` (or `Deriv-Server`).
   * Verify the connection bars in the bottom right corner turn **green**.
3. **Enable Algorithmic Trading:**
   * Click the **"Algo Trading"** button in MT5's top toolbar so it turns **GREEN**.
   * Go to **Tools → Options → Expert Advisors** and ensure:
     * `[✓] Allow algorithmic trading` is checked.
     * `[✓] Allow DLL imports` is checked.

---

### Step 5: Run the Pre-Flight Verification Script

Before turning on autonomous trading, verify everything with the automated auditor:
```cmd
python deploy\verify_deployment.py
```

This runs a full diagnostic scan:
* Validates Python runtime & libraries
* Tests `.env` format
* Tests connection to MT5 terminal
* Tests Telegram bot communication
* Checks read/write permissions for `data/` and `logs/`

Ensure all core components show `[✓ PASS]`.

---

### Step 6: Launch 24/7 Production Execution

Run the production supervisor batch file:
```cmd
deploy\start_production_bot.bat
```
* Runs the pre-flight check automatically.
* Launches `synthetic_bot.py --run`.
* **Built-in Watchdog:** If MT5 disconnects or the system reboots, the watchdog automatically restarts the bot after 5 seconds without manual intervention.

To start the institutional web dashboard on port `8501`:
```cmd
deploy\start_production_dashboard.bat
```

---

### Step 7: (Optional) Auto-Start on System Boot

To ensure both the bot and dashboard automatically restart if your VPS host reboots or updates:

1. Open PowerShell as **Administrator**.
2. Run:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\deploy\install_windows_task.ps1
   ```
This creates Windows Task Scheduler jobs that launch the Watchdog and Dashboard silently on system startup.

---

## 🛡️ 4. Firewall & Remote Dashboard Access

To access the Streamlit dashboard remotely from your phone or laptop (`http://<VPS_IP>:8501`):

Run this command in an Administrator Command Prompt on the VPS to open port 8501:
```cmd
netsh advfirewall firewall add rule name="QuantaSynth_Dashboard" dir=in action=allow protocol=TCP localport=8501
```

---

## 🛑 5. Operational Controls & Monitoring

* **View Active Trade Status:**
  ```cmd
  python synthetic_bot.py --status
  ```
* **Real-time Live Logs:**
  Check `logs\synthetic_bot.log` or monitor directly through the Telegram bot.
* **Emergency Halt:**
  Press `Ctrl+C` in the watchdog window, or toggle bot switch in the dashboard.
