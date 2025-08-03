# TRON AI Trading Bot

> **AI-powered algorithmic trading bot** with **TRON smart contract integration**, built for the UK AI Agent Hackathon.  
> Combines **LangChain AI agents** and **on-chain execution** to automate crypto trading strategies.

---

## Features
- **AI Trading Agent** using LangChain / Superagent  
- **Simple Moving Average (SMA) crossover strategy** (extendable to more strategies)  
- **TRON Smart Contract Integration** for on-chain logging or simulated trades  
- **Natural Language Interface** – control the bot via text commands  
- **Backtesting and Simulation** with historical crypto price data

---

## 📂 Project Structure
```
tron-ai-trading-bot/
│
├─ contracts/         # TRON smart contracts (Solidity)
├─ bot/               # AI trading logic (Python / Node.js)
├─ notebooks/         # Jupyter notebooks for data exploration & backtesting
├─ tests/             # Unit tests
├─ requirements.txt   # Python dependencies
├─ README.md          # Project documentation
└─ LICENSE            # Optional, MIT recommended
```

---

## Quick Start

### 1. Clone the Repo
```bash
git clone https://github.com/nikhilrathodgit/tron-ai-trading-bot.git
cd tron-ai-trading-bot
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
# or Node.js: npm install
```

### 3. Run the Bot (Simulation Mode)
```bash
python bot/main.py
```

---

## Tech Stack
- **Python** for AI logic & backtesting  
- **LangChain / Superagent** for AI agents  
- **TRON Solidity Contracts** for on-chain logging/execution  
- **Pandas & NumPy** for market data analysis  

---

## License
MIT License – free to use and modify for hackathons and personal projects.
