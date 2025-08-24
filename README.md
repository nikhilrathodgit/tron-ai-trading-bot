# 🤖 Tron Algo AI Trading Bot

AI-powered algorithmic trading on the TRON blockchain.  
This project fuses off-chain alpha (SMA/RSI signals via CCXT) with on-chain transparency (TradeLogger contract), an AI agent for natural-language analytics, Supabase for storage, and a Telegram interface for real-time control.

---

## 🚀 Features
- 📊 Signal generation  
  SMA crossovers and RSI triggers on exchange data (via CCXT). Extensible to more indicators.

- 🔗 On-chain logging (TRON)  
  Emits `TradeOpen` and `TradeClosed` events to an immutable TradeLogger contract (Nile testnet first).

- 🗄️ Data layer (Supabase/Postgres)  
  Persists signals, trades, PnL, and chat interactions for analytics and the AI agent.

- 🧠 AI agent (LangChain)  
  Natural-language queries over trades/signals, e.g. “show last 5 trades”, “current PnL”, “alert me when RSI < 30”.

- 💬 Telegram bot  
  `/track`, `/buy`, `/sell`, `/pnl` and signal alerts.

- 🧪 Backtesting & notebooks  
  Jupyter workflows for strategy iteration.

---

## 🧱 Project structure
tron-ai-trading-bot/
├─ contracts/ # Solidity (TRON) — TradeLogger, etc.
├─ bot/ # Core services
│ ├─ signals/ # SMA/RSI generators (CCXT)
│ ├─ agent/ # LangChain SQL agent bindings
│ ├─ telegram/ # Telegram bot commands & handlers
│ ├─ tron/ # TronPy utilities & on-chain logger
│ └─ db/ # Supabase client & queries
├─ notebooks/ # Backtests / research
├─ tests/ # Unit/integration tests
├─ .env.example # Template env vars
├─ requirements.txt # Python deps
└─ README.md # You are here

yaml
Copy
Edit

---

## ⚙️ Setup

### 1) Clone and create a virtual environment
```bash
git clone <your-repo-url>.git
cd tron-ai-trading-bot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
2) Install dependencies
bash
Copy
Edit
pip install -r requirements.txt
3) Configure environment
Create .env at the repo root (copy from .env.example if present):

ini
Copy
Edit
# Exchanges / data
CCXT_EXCHANGE=mexc
CCXT_API_KEY=
CCXT_API_SECRET=

# TRON / on-chain
TRON_NETWORK=nile
TRON_API_KEY=
CONTRACT_ADDRESS=
PRIVATE_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE=

# OpenAI / Agent
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

# Telegram
TELEGRAM_BOT_TOKEN=
▶️ How to run
A) Signal generator (SMA/RSI)
bash
Copy
Edit
python sma_signal_generator.py --loop --interval 60

B) Signal generator (SMA/RSI)
bash
Copy
Edit
python rsi_signal_generator.py --loop --interval 30 

C) Telegram bot
bash
Copy
Edit
python telegram_bot.py


Commands:

## 📜 Commands

- `/start` — show welcome menu and navigation buttons  
- `/help` — quick overview of available commands  

### Trading
- `/buy SYMBOL AMOUNT` — buy tokens (e.g. `/buy TRX 100`)  
- `/buy SYMBOL $AMOUNT` — buy by dollar value (e.g. `/buy TRX $200`)  
- `/buy SYMBOL AMOUNT @ PRICE|market` — buy with manual or explicit price  

- `/sell SYMBOL %` — sell by percent of position (e.g. `/sell TRX 50%`)  
- `/sell SYMBOL AMOUNT` — sell token units (e.g. `/sell TRX 10`)  
- `/sell SYMBOL $AMOUNT` — sell by dollar value (e.g. `/sell TRX $500`)  
- `/sell SYMBOL ... @ PRICE|market` — sell with manual/explicit price  
- `/confirmaddr ADDRESS` — confirm token address for a pending sell  
- `/confirm0x ADDRESS` — alias of `/confirmaddr`  

### Signals
- `/cs TOKEN sma FAST SLOW TF [network]` — create SMA signal subscription  
  e.g. `/cs ADA sma 10 30 1h`  
- `/crsi TOKEN PERIOD TF [network]` — create RSI signal subscription  
  e.g. `/crsi TRX 14 5m`  
- `/ls` — list active subscriptions  
- `/rm TOKEN sma FAST SLOW TF` — remove SMA subscription  
- `/rmrsi TOKEN PERIOD TF` — remove RSI subscription  
- `/rmconfirm ADDRESS` — confirm removal by token address  

### Data & analytics
- `/positions [SYMBOL]` — show open positions (all or specific token)  
- `/refresh_prices` — refresh cached market prices  
- `/ask QUESTION` — ask AI agent about trades/positions (e.g. `/ask last 5 trades`)  
- `/search QUERY` — research prices, indicators, strategies, or market info  
- `/ping` — test bot responsiveness  

### Maintenance
- `/rebuild` — prepare to wipe and rebuild trade history from chain events  
- `/rebuild_confirm` — confirm and execute rebuild  


D) On-chain logger
bash
Copy
Edit
deploy soldity contract TradeLogger.sol on nile testnet

E) AI agent
bash
Copy
Edit
python agent.py
Examples:
“show last 5 trades”, “current pnl by strategy”, “alert me when RSI < 30”.

🧪 Backtesting
bash
Copy
Edit
pip install jupyter
jupyter notebook
🛡️ Safety
Use .env and never commit secrets

Test on Nile before mainnet

Add position sizing and risk guards before real trading

🔮 Roadmap
Add more indicators (MACD, Bollinger)

Web dashboard (FastAPI)

Risk engine (vol targeting, drawdown caps)

Multi-exchange routing

