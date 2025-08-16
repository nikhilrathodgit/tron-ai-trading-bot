#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Reads TradeOpen / TradeClosed from TronGrid (Nile) and writes into:
#   - open_trades (one active position per token)
#   - trade_history (full action log with PnL on sells)
#
# ENV (required):
#   SUPABASE_URL=...
#   SUPABASE_KEY=...   
#   NILE_CONTRACT_ADDRESS=TBw...   # same emit script uses
#   TRONG_API_KEY=... optional addition in future to avoid api rate limits
#
# Scaling (these will be changed at the end when adaptig user experience):
#   PRICE_SCALE=1000000               # 67_500_000 => 67.5 if scale=1e6
#   TOKEN_DECIMALS_DEFAULT=6          # default decimals for amount
#   TOKEN_DECIMALS_MAP={"Txxx":6,"Tyyy":18}  # JSON map per token
#
# Address format (DB primary key = lowercased hex "41..."):
#   TOKEN_ADDR_HEX=1   # if set to "0" stores base58 unchanged (causes problems so decided to normalise everything)

from __future__ import annotations
import os, sys, time, argparse, requests, json, hashlib
from decimal import Decimal, getcontext, ROUND_HALF_UP
from dotenv import load_dotenv
from supabase import create_client


getcontext().prec = 50

EVENTS_BASE = os.getenv("TRON_EVENTS_BASE", "https://nile.trongrid.io")
EVENTS_URL  = EVENTS_BASE + "/v1/contracts/{addr}/events"

def fetch_events(addr, key="", limit=200, fingerprint=None, **extra):
    # addr can be T... or 41...; TronGrid accepts both. Normalize if you prefer.
    url = EVENTS_URL.format(addr=addr)
    headers = {}
    if key:
        headers["TRON-PRO-API-KEY"] = key  # not needed on Nile, required on mainnet
    params = {"limit": limit, "only_confirmed": "true", **extra}
    if fingerprint:
        params["fingerprint"] = fingerprint  # TronGrid uses cursor-based pagination
    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code == 404:
        raise RuntimeError(
            f"404 from {url}. Check base host (should be nile.trongrid.io), "
            f"network, and contract address."
        )
    r.raise_for_status()
    return r.json()


# Address canonicalization

_B58_ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _b58decode_check(s: str) -> bytes:
    """Base58Check decode (Bitcoin/Tron). Returns payload (no 4-byte checksum)."""
    num = 0
    for ch in s:
        num = num * 58 + _B58_ALPH.index(ch)
    # convert to bytes, add leading zeros for each leading '1'
    full = num.to_bytes((num.bit_length() + 7) // 8, "big")
    n_pad = len(s) - len(s.lstrip("1"))
    full = b"\x00" * n_pad + full
    if len(full) < 5:
        raise ValueError("base58 string too short")
    payload, checksum = full[:-4], full[-4:]
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if chk != checksum:
        raise ValueError("invalid base58 checksum")
    return payload

def tron_to_hex(addr: str) -> str:
    """
    Convert TRON base58 "T..." or hex "41..." to lowercased hex (without 0x).
    Keeps '41' version byte (21 bytes total, 42 hex chars).
    """
    a = (addr or "").strip()
    if not a:
        return a
    # already hex?
    if a.startswith("0x") or all(c in "0123456789abcdefABCDEF" for c in a):
        h = a.lower().removeprefix("0x")
        if not h.startswith("41"):
            # If it's 20-byte hex, you can optionally prefix "41"
            pass
        return h
    # base58 -> hex
    payload = _b58decode_check(a)  # 21 bytes: 0x41 + 20
    if payload[0] != 0x41:
        # still accept, but TRON addresses normally start with 0x41
        pass
    return payload.hex().lower()

# Config & helpers 

def load_env():
    load_dotenv()
    # allow either var name (yours uses NILE_CONTRACT_ADDRESS)
    contract = os.getenv("NILE_CONTRACT_ADDRESS") or os.getenv("CONTRACT_ADDRESS")
    cfg = {
        "contract": contract,
        "supabase_url": os.getenv("SUPABASE_URL"),
        "supabase_key": os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY"),
        "trongrid_key": os.getenv("TRON_API_KEY", ""),
        "price_scale": Decimal(os.getenv("PRICE_SCALE", "1000000")),
        "decimals_default": int(os.getenv("TOKEN_DECIMALS_DEFAULT", "6")),
        "decimals_map": {},
        "addr_hex": os.getenv("TOKEN_ADDR_HEX", "1") != "0",
    }
    dm = os.getenv("TOKEN_DECIMALS_MAP")
    if dm:
        try:
            cfg["decimals_map"] = json.loads(dm)
        except Exception as e:
            print(f"[WARN] TOKEN_DECIMALS_MAP parse failed: {e}")
    for k in ("contract", "supabase_url", "supabase_key"):
        if not cfg[k]:
            print(f"[ERR] Missing env: {k}", file=sys.stderr); sys.exit(1)
    return cfg

def supabase_client(cfg) -> Client:
    return create_client(cfg["supabase_url"], cfg["supabase_key"])

def token_decimals(cfg, token_address: str) -> int:
    # Lookup by both base58 and hex (lowercased)
    m = cfg["decimals_map"]
    return int(m.get(token_address, m.get(token_address.lower(), cfg["decimals_default"])))

def to_price(cfg, raw_int: int) -> Decimal:
    return (Decimal(int(raw_int)) / cfg["price_scale"]).quantize(Decimal("0.000000000000000001"))

def to_amount(cfg, token_address: str, raw_int: int) -> Decimal:
    dec = token_decimals(cfg, token_address)
    scale = Decimal(10) ** dec
    return (Decimal(int(raw_int)) / scale).quantize(Decimal("0.000000000000000001"))

def event_uid(ev: dict) -> str:
    """Stable unique id per event to prevent duplicates across polls."""
    payload = {
        "tx": ev.get("transaction_id"),
        "bn": ev.get("block_number"),
        "idx": ev.get("event_index", 0),
        "name": ev.get("event_name"),
        "res": ev.get("result"),
    }
    s = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(s.encode()).hexdigest()

def quant_amount(cfg, token_src: str, x: Decimal) -> Decimal:
    dec = token_decimals(cfg, token_src)
    q = Decimal(1) / (Decimal(10) ** dec)
    return x.quantize(q, rounding=ROUND_HALF_UP)

def is_zero_amount(cfg, token_src: str, x: Decimal) -> bool:
    return quant_amount(cfg, token_src, x) == Decimal("0")

# Parsers for events

def parse_tradeopen(ev, cfg):
    r = ev["result"]
    tok = r["tokenAddress"]
    price = to_price(cfg, r["entryPrice"])
    amt = to_amount(cfg, tok, r["amount"])
    token_db = tron_to_hex(tok) if cfg["addr_hex"] else tok
    return {
        "uid": event_uid(ev),
        "tx_id": ev.get("transaction_id"),
        "event_name": "TradeOpen",
        "trade_id": int(r["tradeId"]),
        "trader": r["trader"],
        "token_address": token_db,
        "token_src": tok,
        "strategy": r.get("strategy"),
        "action": (r.get("action", "BUY") or "BUY").upper(),
        "price": price,
        "amount": amt,
        "block_number": ev.get("block_number"),
    }

def parse_tradeclosed(ev, cfg):
    r = ev["result"]
    tok = r["tokenAddress"]
    price = to_price(cfg, r["exitPrice"])
    token_db = tron_to_hex(tok) if cfg["addr_hex"] else tok
    pnl = Decimal(int(r["pnl"])) / cfg["price_scale"]
    return {
        "uid": event_uid(ev),
        "tx_id": ev.get("transaction_id"),
        "event_name": "TradeClosed",
        "trade_id": int(r["tradeId"]),
        "trader": r["trader"],
        "token_address": token_db,
        "token_src": tok,
        "price": price,
        "pnl": pnl,
        "block_number": ev.get("block_number"),
    }


# DB ops 

def _jsonify_decimals(obj):
    """Convert any Decimal in dict/list scalars to str (safe for PostgREST)."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonify_decimals(v) for k, v in obj.items()}  # typo? see below
    if isinstance(obj, list):
        return [_jsonify_decimals(v) for v in obj]
    return obj

def upsert_open(sup, row: dict):
    sup.table("open_trades").upsert(_jsonify_decimals(row), on_conflict="token_address").execute()

def insert_history(sup, row: dict):
    insert_history_once(sup, row)


def delete_open(sup: Client, token_address: str):
    sup.table("open_trades").delete().eq("token_address", token_address).execute()

def get_open(sup, token_address: str):
    try:
        q = (
            sup.table("open_trades")
               .select("*")
               .eq("token_address", token_address)
               .limit(1)
        )
        resp = q.execute()
    except Exception as e:
        print(f"[get_open] query failed: {e}")
        return None

    # Normalize response shape
    if resp is None:
        return None

    # supabase-py v2: object with .data
    data = getattr(resp, "data", None)

    # older/newer variants: dict with 'data'
    if data is None and isinstance(resp, dict):
        data = resp.get("data")

    # If it's a list, return first or None
    if isinstance(data, list):
        return data[0] if data else None

    # If it's already a dict (single row) or None, return it
    return data

def history_exists_by_uid(sup: Client, uid: str) -> bool:
    if not uid:
        return False
    resp = sup.table("trade_history").select("id").eq("event_uid", uid).limit(1).execute()
    return bool(resp.data)

def insert_history_once(sup: Client, row: dict):
    """Insert once per event_uid. Requires event_uid column to be UNIQUE."""
    # ensure Decimals are strings for PostgREST
    row = _jsonify_decimals(row)
    sup.table("trade_history").upsert(row, on_conflict="event_uid").execute()
    


def get_open_any(sup: Client, token_src: str):
    # Try both stored forms
    possibles = {token_src, tron_to_hex(token_src)}
    resp = sup.table("open_trades").select("*").in_("token_address", list(possibles)).limit(1).execute()
    return resp.data[0] if resp.data else None

def delete_open_any(sup: Client, token_src: str):
    possibles = {token_src, tron_to_hex(token_src)}
    sup.table("open_trades").delete().in_("token_address", list(possibles)).execute()


# Position math 

def buy_merge(avg_entry_price: Decimal, amount: Decimal, buy_price: Decimal, buy_amount: Decimal):
    if amount <= 0:
        return (buy_price, buy_amount)
    new_amt = amount + buy_amount
    new_avg = ((avg_entry_price * amount) + (buy_price * buy_amount)) / new_amt
    return (new_avg, new_amt)

def sell_pnl(avg_entry_price: Decimal, sell_price: Decimal, sell_amount: Decimal):
    return (sell_price - avg_entry_price) * sell_amount

# Apply events

def apply_tradeopen(sup: Client, ev: dict, cfg):
    token = ev["token_address"]
    action = ev["action"]
    price  = ev["price"]
    amt    = ev["amount"]
    trade_id = ev["trade_id"]

    op = get_open(sup, token)

    hist = {
        "trade_id_onchain": trade_id,
        "token_address": token,
        "action": action,
        "price": price,
        "amount": amt,
        "strategy": ev.get("strategy"),
        "avg_entry_price": None,
        "avg_exit_price": None,
        "pnl": None,
    }

    if action == "BUY":
        if op and Decimal(str(op["amount"])) > 0:
            # merging without overwriting opening trade_id_onchain
            new_avg, new_amt = buy_merge(
                Decimal(str(op["avg_entry_price"])),
                Decimal(str(op["amount"])),
                price, amt
            )
            upsert_open(sup, {
                "token_address": token,
                # keep the original opening trade id:
                "trade_id_onchain": op.get("trade_id_onchain"),
                "avg_entry_price": new_avg,
                "amount": new_amt,
                "strategy": ev.get("strategy") or op.get("strategy"),
                "trader": ev.get("trader"),
                "last_tx_id": ev.get("tx_id"),
            })
            hist["avg_entry_price"] = new_avg
        else:
            # first buy: set opening trade id
            upsert_open(sup, {
                "token_address": token,
                "trade_id_onchain": trade_id,   # <- only here
                "avg_entry_price": price,
                "amount": amt,
                "strategy": ev.get("strategy"),
                "trader": ev.get("trader"),
                "last_tx_id": ev.get("tx_id"),
            })
            hist["avg_entry_price"] = price

        # carry idempotency fields into history
        hist.update({"tx_id": ev.get("tx_id"), "event_uid": ev.get("uid")})
        insert_history_once(sup, hist)
        return


    if action == "SELL":
        if not op or Decimal(str(op["amount"])) <= 0:
            hist.update({"tx_id": ev.get("tx_id"), "event_uid": ev.get("uid")})
            insert_history_once(sup, hist)
            return
        open_amt = Decimal(str(op["amount"]))
        avg_entry = Decimal(str(op["avg_entry_price"]))
        sell_amt  = min(amt, open_amt)
        realized  = sell_pnl(avg_entry, price, sell_amt)
        hist.update({
            "avg_entry_price": avg_entry,
            "avg_exit_price": price,
            "amount": sell_amt,
            "pnl": realized,
            "tx_id": ev.get("tx_id"),
            "event_uid": ev.get("uid"),
        })
        insert_history_once(sup, hist)

        remaining = quant_amount(cfg, ev["token_src"], (open_amt - sell_amt))
        if is_zero_amount(cfg, ev["token_src"], remaining):
            delete_open(sup, token)
        else:
            upsert_open(sup, {
                "token_address": token,
                "trade_id_onchain": op.get("trade_id_onchain"),
                "avg_entry_price": avg_entry,
                "amount": remaining,
                "strategy": ev.get("strategy") or op.get("strategy"),
                "trader": ev.get("trader"),
                "last_tx_id": ev.get("tx_id"),
            })

        return

def apply_tradeclosed(sup: Client, ev: dict, cfg):
    token = ev["token_address"]
    close_price = ev["price"]
    trade_id = ev["trade_id"]
    pnl_ev = ev.get("pnl")

    op = get_open(sup, token)

    hist = {
        "trade_id_onchain": trade_id,
        "token_address": token,
        "action": "SELL",
        "price": close_price,
        "avg_exit_price": close_price,
        "amount": Decimal("0"),
        "strategy": None,
        "avg_entry_price": None,
        "pnl": pnl_ev,
        "tx_id": ev.get("tx_id"),
        "event_uid": ev.get("uid"),
    }

    if not op or Decimal(str(op["amount"])) <= 0:
        insert_history_once(sup, hist)
        return

        # in apply_tradeclosed():
    open_amt = quant_amount(cfg, ev["token_src"], Decimal(str(op["amount"]))) # safe now because the exact-close case won’t leave crumbs
    avg_entry = Decimal(str(op["avg_entry_price"]))
    realized  = sell_pnl(avg_entry, close_price, open_amt)

    hist.update({
        "amount": open_amt,
        "strategy": op.get("strategy"),
        "avg_entry_price": avg_entry,
        "pnl": pnl_ev if pnl_ev is not None else realized,
    })

    insert_history_once(sup, hist)
    delete_open(sup, token)


# Run modes

def run_once(cfg):
    sup = supabase_client(cfg)
    fingerprint = None
    total = 0
    pages = 0

    while True:
        j = fetch_events(cfg["contract"], key=cfg["trongrid_key"], limit=200, fingerprint=fingerprint)
        events = j.get("data", [])
        if not events:
            break

        # process oldest → newest so buys land before sells
        events.sort(key=lambda e: (e.get("block_number", 0), e.get("event_index", 0)))

        for ev in events:
            name = ev.get("event_name")
            if name == "TradeOpen":
                apply_tradeopen(sup, parse_tradeopen(ev, cfg), cfg)
            elif name == "TradeClosed":
                apply_tradeclosed(sup, parse_tradeclosed(ev, cfg), cfg)
            total += 1

        # advance the cursor so not looping page 1 forever
        fingerprint = (
            j.get("meta", {}).get("fingerprint")
            or j.get("fingerprint")
            or (j.get("meta", {}).get("links", {}).get("next") if isinstance(j.get("meta", {}).get("links"), dict) else None)
        )
        if not fingerprint:
            break
        pages += 1

    print(f"✅ once: processed {total} events across {pages+1} page(s). Tables are up to date!")


def tail(cfg, interval=5):
    sup = supabase_client(cfg)
    seen = set()
    print(f"[tail] polling every {interval}s...")
    while True:
        try:
            j = fetch_events(cfg["contract"], key=cfg["trongrid_key"], limit=200)
            events = j.get("data", [])
            # oldest → newest
            events.sort(key=lambda e: (e.get("block_number", 0), e.get("event_index", 0)))
            new_count = 0
            for ev in events:
                uid = event_uid(ev)
                if uid in seen: 
                    continue
                seen.add(uid)
                name = ev.get("event_name")
                if name == "TradeOpen":
                    apply_tradeopen(sup, parse_tradeopen(ev, cfg), cfg)
                elif name == "TradeClosed":
                    apply_tradeclosed(sup, parse_tradeclosed(ev, cfg), cfg)
                new_count += 1
            print(f"[tail] +{new_count} new events; sleeping {interval}s")
        except Exception as e:
            print("[tail] error:", e)
        time.sleep(interval)


def main():
    cfg = load_env()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once")
    sub.add_parser("tail")
    args = ap.parse_args()
    if args.cmd == "once":
        run_once(cfg)
    else:
        tail(cfg, 5)

if __name__ == "__main__":
    main()
