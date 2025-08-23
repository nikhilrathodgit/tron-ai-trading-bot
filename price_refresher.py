#!/usr/bin/env python3
"""
price_refresher.py
Refresh latest USDT prices for all open positions into `prices_latest`.
Run on cron / Task Scheduler (e.g., every 1–5 minutes).
"""

from __future__ import annotations
import os, sys, asyncio
from decimal import Decimal
from datetime import datetime, timezone
from os import getenv
from dotenv import load_dotenv
from supabase import create_client
import ccxt
import json


SYMBOLS_MAP = {}
try:
    SYMBOLS_MAP = json.loads(os.getenv("TOKEN_SYMBOLS_MAP", "{}"))
except Exception:
    SYMBOLS_MAP = {}


# Reuse your helpers
from price_sources import (
    is_token_address,
    fetch_onchain_price_and_meta,  # returns (price: Decimal, symbol: str|None, normalized_addr: str)
)

# ---------- env ----------
load_dotenv()

SB_URL = os.getenv("SUPABASE_URL")
SB_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
if not (SB_URL and SB_KEY):
    print("❌ Supabase creds missing")
    sys.exit(1)
sb = create_client(SB_URL, SB_KEY)

EX_NAME = os.getenv("MARKET_EXCHANGE", "binance")
ex = getattr(ccxt, EX_NAME)({"enableRateLimit": True, "timeout": 20000})
try:
    ex.load_markets()
except Exception:
    pass

def ccxt_symbol(symbol: str) -> str:
    return f"{symbol.upper()}/USDT"

def has_ccxt_market(symbol: str) -> bool:
    sym = ccxt_symbol(symbol)
    return hasattr(ex, "markets") and sym in ex.markets

def now_iso():
    return datetime.now(timezone.utc).isoformat()

async def fetch_ccxt_price(symbol: str) -> Decimal | None:
    def _fetch():
        t = ex.fetch_ticker(ccxt_symbol(symbol))
        return Decimal(str(t["last"]))
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return None

async def refresh_once():

    try:
        SYMBOLS_MAP = json.loads(getenv("TOKEN_SYMBOLS_MAP", "{}"))
    except Exception:
        SYMBOLS_MAP = {}
    # 1) load open positions
    resp = (sb.table("open_trades")
              .select("token_symbol, token_address, avg_entry_price, amount")
              .execute())
    rows = getattr(resp, "data", None) or []
    if not rows:
        print("[prices] no open_trades"); return

    updates = []
    for r in rows:
        sym  = (r.get("token_symbol") or "").strip().upper()
        addr = (r.get("token_address") or "").strip()
        if not addr:
            continue

        price = None
        source = None

        # 1) CCXT for majors by symbol
        if sym and has_ccxt_market(sym):
            px = await fetch_ccxt_price(sym)
            if px and px > 0:
                price, source = px, "ccxt"

                # 3) fallback to on-chain (prefer Dexscreener ds_address from map)
        if price is None:
            # Use mapped ds_address for fetch if present; else use the canonical
            fetch_addr = SYMBOLS_MAP.get(sym) or addr
            try:
                px, scraped_symbol, _ = fetch_onchain_price_and_meta(fetch_addr)
                if px and px > 0:
                    price, source = px, "dex"
                    if scraped_symbol:
                        sym = scraped_symbol.upper()
            except Exception:
                price = None


        if price is None:
            print(f"[prices] no price for {addr} ({sym}) – skipped")
            continue

        # IMPORTANT: upsert under the *canonical* address stored in open_trades (addr)
        updates.append({
            "token_address": addr,          # keep canonical here for UI join
            "token_symbol": sym or None,
            "last_price": str(price),
            "source": source,
            "price_ts": now_iso(),
            "updated_at": now_iso(),
        })

    # 4) upsert in one go (small batches OK)
    if updates:
        sb.table("prices_latest").upsert(updates, on_conflict="token_address").execute()
        print(f"[prices] upserted {len(updates)} row(s)")
    else:
        print("[prices] nothing to update")

def main():
    asyncio.run(refresh_once())

if __name__ == "__main__":
    main()
