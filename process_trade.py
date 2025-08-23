from tronpy import Tron
from tronpy.providers import HTTPProvider
from supabase import create_client
from decimal import Decimal
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# Connect to TRON full node
client = Tron(HTTPProvider(api_key=os.getenv("TRON_API_KEY")))  # TRON API key from TronGrid
contract_address = os.getenv("CONTRACT_ADDRESS")
contract = client.get_contract(contract_address)

# Connect to Supabase
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def process_trade_event(event, action):
    token = event['tokenAddress']
    price = Decimal(event['price'])  # already adjusted for decimals
    amount = Decimal(event['amount'])
    strategy = event.get('strategy', None)
    trader = event['trader']
    timestamp = datetime.utcfromtimestamp(event['timestamp'])

    resp = supabase.table("open_trades").select("*").eq("token_address", token).execute()
    open_pos = resp.data[0] if resp.data else None

    if action == "BUY":
        if open_pos:
            total_cost = (Decimal(open_pos["avg_entry_price"]) * Decimal(open_pos["amount"])) + (price * amount)
            new_amount = Decimal(open_pos["amount"]) + amount
            new_avg_price = total_cost / new_amount

            supabase.table("open_trades").update({
                "avg_entry_price": float(new_avg_price),
                "amount": float(new_amount)
            }).eq("token_address", token).execute()
        else:
            supabase.table("open_trades").insert({
                "token_address": token,
                "avg_entry_price": float(price),
                "amount": float(amount),
                "strategy": strategy,
                "trader": trader
            }).execute()

        supabase.table("trade_history").insert({
            "token_address": token,
            "action": "BUY",
            "price": float(price),
            "avg_entry_price": float(price if not open_pos else open_pos["avg_entry_price"]),
            "avg_exit_price": None,
            "amount": float(amount),
            "pnl": None,
            "timestamp": timestamp
        }).execute()

    elif action == "SELL" and open_pos:
        if amount > Decimal(open_pos["amount"]):
            amount = Decimal(open_pos["amount"])  # avoid negatives

        pnl_per_unit = price - Decimal(open_pos["avg_entry_price"])
        pnl = pnl_per_unit * amount
        remaining_amount = Decimal(open_pos["amount"]) - amount

        if remaining_amount > 0:
            supabase.table("open_trades").update({
                "amount": float(remaining_amount)
            }).eq("token_address", token).execute()
        else:
            supabase.table("open_trades").delete().eq("token_address", token).execute()

        supabase.table("trade_history").insert({
            "token_address": token,
            "action": "SELL",
            "price": float(price),
            "avg_entry_price": float(open_pos["avg_entry_price"]),
            "avg_exit_price": float(price),
            "amount": float(amount),
            "pnl": float(pnl),
            "timestamp": timestamp
        }).execute()

def listen_for_events():
    print("Listening for TRON TradeLogger events...")
    latest_block = client.get_latest_block_number()

    while True:
        current_block = client.get_latest_block_number()
        if current_block > latest_block:
            events = contract.events.TradeOpen(from_block=latest_block+1, to_block=current_block)
            for ev in events:
                process_trade_event({
                    "tokenAddress": ev['args']['tokenAddress'],
                    "amount": ev['args']['amount'] / (10 ** 18),
                    "price": ev['args']['price'] / (10 ** 6),
                    "strategy": ev['args']['strategy'],
                    "trader": ev['args']['trader'],
                    "timestamp": ev['args']['timestamp']
                }, "BUY")

            events = contract.events.TradeClosed(from_block=latest_block+1, to_block=current_block)
            for ev in events:
                process_trade_event({
                    "tokenAddress": ev['args']['tokenAddress'],
                    "amount": ev['args']['amount'] / (10 ** 18),
                    "price": ev['args']['price'] / (10 ** 6),
                    "trader": ev['args']['trader'],
                    "timestamp": ev['args']['timestamp']
                }, "SELL")

            latest_block = current_block

if __name__ == "__main__":
    listen_for_events()
