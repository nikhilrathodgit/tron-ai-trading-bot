from supabase import create_client
import os
import ccxt
from dotenv import load_dotenv
from sma_bot_revised import get_latest_signal
from datetime import datetime, timedelta, timezone


load_dotenv()

print("🔹 Connecting to Supabase...")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
print("✅ Connected to Supabase")

# Initialize exchange (Binance)
exchange = ccxt.binance()
ticker = exchange.fetch_ticker('BTC/USDT')

# Generating trade
print("🔹 Getting latest SMA signal...")
signal = get_latest_signal()   # e.g., "BUY" or "SELL"
price = ticker['last']
print(f"✅ Signal: {signal}, Entry Price: {price}")              

# Inserting entry trade
trade = {
    "strategy": "SMA Crossover",
    "action": signal,
    "entry_price": price,
    "amount": 0.01,
    "pnl": None
}

print("🔹 Logging trade to Supabase...")
res = supabase.table("trades").insert(trade).execute()
print("✅Trade logged:", res.data)

# Simulating closing trade with PnL
trade_id = res.data[0]["id"]
exit_price = 40500 # arbitrary exit price for testing pnl calculator
pnl = (exit_price - price) * 0.01
print(f"🔹 Simulating trade exit at {exit_price}, PnL = {pnl}")

supabase.table("trades").update({
    "exit_price": exit_price,
    "exit_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), # exit time one hour ahead for testing
    "pnl": pnl,
    "status": "CLOSED"
}).eq("id", trade_id).execute()
print("✅ Trade updated with exit details!")
