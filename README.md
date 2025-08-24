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
python -m bot.signals.run --symbol TRX/USDT --interval 1m --sma_fast 10 --sma_slow 30 --rsi_len 14
B) Telegram bot
bash
Copy
Edit
python -m bot.telegram.run
Commands:

/track <symbol> — monitor a token

/buy <symbol> $amount — simulate/execute buy

/sell <symbol> $amount — simulate/execute sell

/pnl — compute PnL

C) On-chain logger
bash
Copy
Edit
python -m bot.tron.logger --mode live
D) AI agent
bash
Copy
Edit
python -m bot.agent.run
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

