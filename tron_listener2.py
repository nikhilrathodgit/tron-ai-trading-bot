import os
import json
from decimal import Decimal, getcontext
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
from tronpy import Tron
from tronpy.providers import HTTPProvider
from supabase import create_client, Client

# High precision for money math
getcontext().prec = 40

print("🔄 Loading environment variables...")
load_dotenv()

# ==== ENV ====
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
TRON_API_KEY: str = os.getenv("TRON_API_KEY", "")
TRON_NETWORK = os.getenv("TRON_NETWORK", "nile").lower()
CONTRACT_ADDRESS: str = os.getenv("NILE_CONTRACT_ADDRESS" if TRON_NETWORK == "nile" else "MAINNET_CONTRACT_ADDRESS", "")
CONTRACT_ABI_JSON: str = os.getenv("NILE_CONTRACT_ABI" if TRON_NETWORK == "nile" else "MAINNET_CONTRACT_ABI", "")
DEPLOYER_ADDRESS: str = os.getenv("DEPLOYER_ADDRESS", "")

print(f"📜 Loaded ENV: TRON_NETWORK={TRON_NETWORK}, CONTRACT_ADDRESS={CONTRACT_ADDRESS}, "
      f"SUPABASE_URL={'set' if SUPABASE_URL else 'MISSING'}, TRON_API_KEY={'set' if TRON_API_KEY else 'not set'}")

# ==== Clients ====
RPC_URL = "https://api.nileex.io" if TRON_NETWORK == "nile" else "https://api.trongrid.io"
print(f"🌐 Connecting to TRON network: {RPC_URL} ...")
tron = Tron(HTTPProvider(RPC_URL, api_key=TRON_API_KEY) if TRON_API_KEY else HTTPProvider(RPC_URL))
print("✅ TRON connection OK")

# Load ABI
abi = None
if CONTRACT_ABI_JSON:
    try:
        abi = json.loads(CONTRACT_ABI_JSON)
        print(f"✅ ABI loaded with {len(abi)} entries")
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing ABI JSON: {e}")
else:
    print("⚠ No ABI in env, relying on TronScan fetch (requires verified contract)")

# Create contract
try:
    contract = tron.get_contract(CONTRACT_ADDRESS, abi=abi) if abi else tron.get_contract(CONTRACT_ADDRESS)
    print(f"✅ Contract loaded: {CONTRACT_ADDRESS}")
except Exception as e:
    print(f"❌ Contract load failed: {e}")
    raise

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
print("✅ Supabase client initialized")

def fetch_events(event_name: str, from_block: int, to_block: int):
    all_events = []
    for b in range(from_block, to_block + 1):
        try:
            res = tron.get_event_result(
                contract_address=CONTRACT_ADDRESS,
                event_name=event_name,
                block_number=b,
                # only_confirmed=True  # uncomment if you only want confirmed
            )
            if res:
                print(f"✅ {event_name} @ block {b}: {len(res)}")
                all_events.extend(res)
        except Exception as e:
            print(f"⚠ fetch {event_name} @ block {b} failed: {e}")
    return all_events


# ==== Price helpers ====
def fetch_price_from_ave(token_hex: str) -> Decimal | None:
    print(f"🌍 Fetching price from Ave for {token_hex}...")
    url = f"https://cloud.ave.ai/api/token/{token_hex}"
    try:
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        val = (data.get("price") or data.get("data", {}).get("price"))
        print(f"💲 Ave returned price: {val}")
        return Decimal(str(val)) if val is not None else None
    except Exception as e:
        print(f"⚠ Ave fetch failed: {e}")
        return None

def fetch_price_from_dexscreener(token_hex: str) -> Decimal | None:
    print(f"🌍 Fetching price from DexScreener for {token_hex}...")
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_hex}", timeout=7)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            price = pairs[0].get("priceUsd", "0")
            print(f"💲 DexScreener returned price: {price}")
            return Decimal(str(price))
        print("⚠ DexScreener returned no pairs")
        return None
    except Exception as e:
        print(f"⚠ DexScreener fetch failed: {e}")
        return None

def get_token_price_usd(token_hex: str) -> Decimal | None:
    return fetch_price_from_ave(token_hex) or fetch_price_from_dexscreener(token_hex)

# ==== Token decimals ====
def get_token_decimals(token_hex: str) -> int:
    try:
        t = tron.get_contract(token_hex)
        return int(t.functions.decimals())
    except Exception:
        return 18  # sensible default

# ==== Supabase helpers ====
def get_open_position(token_address: str):
    print(f"🔍 Checking open position for {token_address}")
    res = supabase.table("open_trades").select("*").eq("token_address", token_address).limit(1).execute()
    return res.data[0] if res.data else None

def upsert_open_position(token_address: str, avg_entry_price: Decimal, amount: Decimal,
                         strategy: str | None, trader: str, trade_id_onchain: int | None):
    payload = {
        "token_address": token_address,
        "avg_entry_price": float(avg_entry_price),
        "amount": float(amount),
        "strategy": strategy,
        "trader": trader,
        "trade_id_onchain": trade_id_onchain
    }
    # If strategy or trade_id_onchain missing, keep existing values (handled by code before calling)
    supabase.table("open_trades").upsert(payload).execute()

def update_open_amount(token_address: str, amount: Decimal):
    supabase.table("open_trades").update({"amount": float(amount)}).eq("token_address", token_address).execute()

def update_open_avg_amount_and_maybe_strategy(token_address: str, avg_entry_price: Decimal, amount: Decimal,
                                              strategy: str | None = None):
    payload = {"avg_entry_price": float(avg_entry_price), "amount": float(amount)}
    if strategy is not None:
        payload["strategy"] = strategy
    supabase.table("open_trades").update(payload).eq("token_address", token_address).execute()

def delete_open_position(token_address: str):
    supabase.table("open_trades").delete().eq("token_address", token_address).execute()

def insert_history_row(token: str, action: str, price: Decimal,
                       avg_entry: Decimal | None, avg_exit: Decimal | None,
                       amount: Decimal, pnl: Decimal | None, ts: datetime,
                       trade_id_onchain: int | None, strategy: str | None):
    supabase.table("trade_history").insert({
        "token_address": token,
        "action": action,
        "price": float(price),
        "avg_entry_price": float(avg_entry) if avg_entry is not None else None,
        "avg_exit_price": float(avg_exit) if avg_exit is not None else None,
        "amount": float(amount),
        "pnl": float(pnl) if pnl is not None else None,
        "timestamp": ts.replace(tzinfo=timezone.utc),
        "trade_id_onchain": trade_id_onchain,
        "strategy": strategy
    }).execute()

# ==== Core aggregation ====
def handle_buy(token_hex: str, human_amount: Decimal, price_usd: Decimal,
               strategy_in: str | None, trader_hex: str, ts_unix: int,
               trade_id_onchain: int | None):
    ts = datetime.utcfromtimestamp(int(ts_unix))
    token_key = token_hex.lower()

    current = get_open_position(token_key)
    if current:
        cur_amt = Decimal(str(current["amount"]))
        cur_avg = Decimal(str(current["avg_entry_price"]))
        # Prefer existing strategy if incoming is None
        strategy = strategy_in or current.get("strategy")

        new_amt = cur_amt + human_amount
        total_cost = (cur_avg * cur_amt) + (price_usd * human_amount)
        new_avg = total_cost / new_amt if new_amt != 0 else price_usd

        update_open_avg_amount_and_maybe_strategy(token_key, new_avg, new_amt, strategy=strategy)
        insert_history_row(token_key, "BUY", price_usd, new_avg, None, human_amount, None, ts,
                           trade_id_onchain=current.get("trade_id_onchain") or trade_id_onchain,
                           strategy=strategy)
    else:
        # New position: set strategy and onchain trade id if provided
        upsert_open_position(token_key, price_usd, human_amount,
                             strategy=strategy_in, trader=trader_hex.lower(),
                             trade_id_onchain=trade_id_onchain)
        insert_history_row(token_key, "BUY", price_usd, price_usd, None, human_amount, None, ts,
                           trade_id_onchain=trade_id_onchain, strategy=strategy_in)

def handle_sell(token_hex: str, human_amount: Decimal, price_usd: Decimal,
                trader_hex: str, ts_unix: int, trade_id_onchain: int | None):
    ts = datetime.utcfromtimestamp(int(ts_unix))
    token_key = token_hex.lower()

    current = get_open_position(token_key)
    if not current:
        print(f"⚠ SELL ignored: no open position for {token_key}")
        return

    cur_amt = Decimal(str(current["amount"]))
    cur_avg = Decimal(str(current["avg_entry_price"]))
    strategy = current.get("strategy")
    trade_id_keep = current.get("trade_id_onchain") or trade_id_onchain

    sell_amt = min(human_amount, cur_amt)
    if sell_amt <= 0:
        print(f"⚠ SELL ignored: zero sell amount for {token_key}")
        return

    pnl_per_unit = price_usd - cur_avg
    pnl = pnl_per_unit * sell_amt
    remaining = cur_amt - sell_amt

    if remaining > 0:
        update_open_amount(token_key, remaining)
    else:
        delete_open_position(token_key)

    insert_history_row(token_key, "SELL", price_usd, cur_avg, price_usd, sell_amt, pnl, ts,
                       trade_id_onchain=trade_id_keep, strategy=strategy)

# ==== Event processing ====
def process_trade_event(event_args: dict, action: str):
    """
    event_args needs:
      trader, tokenAddress, amount, timestamp
      optional: price, strategy, tradeId (or trade_id)
    """
    trader = str(event_args["trader"]).lower()
    if DEPLOYER_ADDRESS and trader != DEPLOYER_ADDRESS.lower():
        print(f"⚠ Ignored: trader {trader} != deployer {DEPLOYER_ADDRESS.lower()}")
        return

    token_hex = str(event_args["tokenAddress"]).lower()
    raw_amount = Decimal(str(event_args["amount"]))
    ts_unix = int(event_args["timestamp"])

    # Trade ID (support a couple of key names)
    trade_id_onchain = event_args.get("tradeId")
    if trade_id_onchain is None:
        trade_id_onchain = event_args.get("trade_id")

    # Price: take from event if present; else fetch via API
    raw_price = event_args.get("price") or event_args.get("entryPrice") or event_args.get("exitPrice")
    price_usd = Decimal(str(raw_price)) if raw_price is not None else Decimal(0)
    if price_usd == 0:
        price_api = get_token_price_usd(token_hex)
        if price_api is None:
            print(f"⚠ No price for token {token_hex}, skipping.")
            return
        price_usd = price_api

    # Amount to human units
    decimals = get_token_decimals(token_hex)
    human_amount = raw_amount / (Decimal(10) ** Decimal(decimals))

    # Strategy: prefer event’s; if absent on BUY, we’ll set/keep it in open_trades
    strategy = event_args.get("strategy")

    if action == "BUY":
        handle_buy(token_hex, human_amount, price_usd, strategy, trader, ts_unix, trade_id_onchain)
    elif action == "SELL":
        handle_sell(token_hex, human_amount, price_usd, trader, ts_unix, trade_id_onchain)

# ==== Poller ====
def listen_for_events(poll_backfill: int = 3):
    print("🔊 Listening for TRON TradeLogger events...")
    latest = tron.get_latest_block_number()
    start_block = max(0, latest - poll_backfill)
    print(f"📦 Latest block: {latest}")
    print(f"⏳ Starting from block: {start_block}")

    while True:
        current = tron.get_latest_block_number()
        if current > start_block:
            print(f"📦 Checking blocks {start_block + 1} → {current}")
            found_event = False

            # TradeOpen (BUY)
            opens = fetch_events("TradeOpen", start_block + 1, current)
            for ev in opens:
                print(f"🟢 TradeOpen raw: {ev}")
                args = ev.get("result") or {}
                event_args = {
                    "tradeId":     args.get("tradeId") or args.get("trade_id"),
                    "trader":      args.get("trader"),
                    "tokenAddress":args.get("tokenAddress"),
                    "amount":      args.get("amount"),
                    "price":       args.get("entryPrice") or args.get("price"),
                    "strategy":    args.get("strategy"),
                    "timestamp":   args.get("timestamp"),
                }
                process_trade_event(event_args, "BUY")
                found_event = True

            # TradeClosed (SELL)
            closes = fetch_events("TradeClosed", start_block + 1, current)
            for ev in closes:
                print(f"🔴 TradeClosed raw: {ev}")
                args = ev.get("result") or {}
                event_args = {
                    "tradeId":     args.get("tradeId") or args.get("trade_id"),
                    "trader":      args.get("trader"),
                    "tokenAddress":args.get("tokenAddress"),
                    "amount":      args.get("amount") or args.get("sellAmount") or args.get("amountSold") or 0,
                    "price":       args.get("exitPrice") or args.get("price"),
                    "timestamp":   args.get("timestamp"),
                }
                process_trade_event(event_args, "SELL")
                found_event = True

            if not found_event:
                print("📭 No events found in this range")

            start_block = current

if __name__ == "__main__":
    listen_for_events(poll_backfill=3)
