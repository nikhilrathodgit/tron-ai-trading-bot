#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSI signal generator (CCXT + on-chain address fallback).
"""

import os, time, json
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
from dotenv import load_dotenv
from supabase import create_client

# ---- local modules (existing) ----
import price_sources as PS
from price_sources import is_token_address, CandlesNotFound

# ---- optional: CCXT for CEX pairs ----
try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None  # we’ll guard at runtime

load_dotenv()

SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY")

# Generator defaults (can be overridden via env)
PAIR_INPUT    = os.getenv("SIGNALS_PAIR", "TRX/USDT")
TIMEFRAME     = os.getenv("SIGNALS_TIMEFRAME", "1m")
RSI_PERIOD    = int(os.getenv("SIGNALS_RSI_PERIOD", "14"))
RSI_OB        = float(os.getenv("SIGNALS_RSI_OVERBOUGHT", "70"))
RSI_OS        = float(os.getenv("SIGNALS_RSI_OVERSOLD",  "30"))

# CCXT config
CCXT_EXCHANGE = (os.getenv("CCXT_EXCHANGE") or "binance").lower()      
CCXT_MARKET   = (os.getenv("CCXT_MARKET") or "spot").lower()          
CCXT_API_KEY  = os.getenv("CCXT_API_KEY") or ""
CCXT_SECRET   = os.getenv("CCXT_SECRET") or ""


try:
    SYMBOLS_MAP = json.loads(os.getenv("TOKEN_SYMBOLS_MAP", "{}"))
except Exception:
    SYMBOLS_MAP = {}

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------- CCXT HELPERS -------------

_ccxt = None
_markets = None

def _ccxt_client():
    """Build a cached CCXT client for the chosen exchange."""
    global _ccxt, _markets
    if _ccxt is not None:
        return _ccxt
    if ccxt is None:
        raise RuntimeError("ccxt not installed. Run: pip install ccxt")

    if not hasattr(ccxt, CCXT_EXCHANGE):
        raise RuntimeError(f"Unsupported CCXT exchange '{CCXT_EXCHANGE}'")

    klass = getattr(ccxt, CCXT_EXCHANGE)
    # api keys not required for OHLCV, but harmless if provided
    params = {"enableRateLimit": True}
    if CCXT_API_KEY and CCXT_SECRET:
        params.update({"apiKey": CCXT_API_KEY, "secret": CCXT_SECRET})
    _ccxt = klass(params)
    # spot vs perp tweaks (some exchanges require 'options' to pick default market)
    try:
        if CCXT_MARKET == "linear" and hasattr(_ccxt, "options"):
            # common for bybit/okx
            _ccxt.options = {**getattr(_ccxt, "options", {}), "defaultType": "swap"}
    except Exception:
        pass

    _markets = _ccxt.load_markets()
    return _ccxt

# CCXT timeframe normalization: accept "1m/5m/15m/1h/4h/1d/1w"
def _tf_ok(tf: str) -> str:
    s = (tf or "").lower()
    mapping = {
        "1m":"1m","3m":"3m","5m":"5m","10m":"10m","15m":"15m","30m":"30m",
        "1h":"1h","2h":"2h","4h":"4h","6h":"6h","8h":"8h","12h":"12h",
        "1d":"1d","3d":"3d","1w":"1w","1M":"1M".lower()
    }
    return mapping.get(s, "1m")

def fetch_ohlcv_ccxt(pair: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
    ex = _ccxt_client()
    tf = _tf_ok(timeframe)
    # Ensure symbol exists; try common quote fallbacks
    sym = pair.upper().replace("-", "/")
    if sym not in ex.markets:
        # might be missing suffix like :USDT or need exact market id per exchange
        # we'll try a couple of typical aliases
        aliases = {sym, sym.replace("/USDT", "/USDT"), sym.replace("/USDT", "/USDT:USDT")}
        found = None
        for a in aliases:
            if a in ex.markets:
                found = a
                break
        if not found:
            # last attempt: try to add ':USDT' contract-specifier for perps
            alt = sym + ":USDT"
            if alt in ex.markets:
                found = alt
        if not found:
            raise CandlesNotFound(f"{sym} not listed on {CCXT_EXCHANGE}")
        sym = found
    raw = ex.fetch_ohlcv(sym, tf, limit=min(limit, 1000))
    if not raw:
        raise CandlesNotFound(f"No OHLCV from {CCXT_EXCHANGE} for {sym} {tf}")
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","vol"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

# ------------- CORE FETCH -------------

def fetch_ohlcv_resolved(token_like: str, timeframe: str, limit=500, network: str | None = None) -> pd.DataFrame:
    """
    If token_like is an address => use on-chain OHLCV via price_sources.
    Else => treat as CCXT symbol/pair and fetch via ccxt.
    """
    if is_token_address(token_like):
        rows = PS.fetch_ohlcv_like_ccxt(token_like, timeframe, limit=min(limit, 500), network=network)
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df

    # allow symbol only (assume USDT quote) or pair with slash
    sym = (token_like or "").upper()
    if "/" not in sym:
        sym = f"{sym}/USDT"
    return fetch_ohlcv_ccxt(sym, timeframe, limit=limit)

# ------------- RSI CALC -------------

def rsi_series(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0.0)
    loss  = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, 1e-12)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def last_rsi_cross(df: pd.DataFrame, period: int, ob: float = 70.0, os_: float = 30.0):
    d = df.sort_values("dt").copy()
    d["rsi"] = rsi_series(d["close"].astype(float), period)
    d = d.dropna()
    if len(d) < 2:
        return None
    prev = d.iloc[-2]
    curr = d.iloc[-1]
    r_prev, r_curr = float(prev["rsi"]), float(curr["rsi"])
    # BUY when RSI rises back above oversold threshold
    if (r_prev < os_) and (r_curr >= os_):
        return {"signal":"BUY", "price": Decimal(str(curr["close"])), "crossed_at": curr["dt"].to_pydatetime().replace(tzinfo=timezone.utc)}
    # SELL when RSI falls back below overbought threshold
    if (r_prev > ob) and (r_curr <= ob):
        return {"signal":"SELL", "price": Decimal(str(curr["close"])), "crossed_at": curr["dt"].to_pydatetime().replace(tzinfo=timezone.utc)}
    return None

# ------------- SUBSCRIPTIONS -------------

def _resolve_pair_for_fetch(row: dict) -> tuple[str, str | None]:
    """
    Returns (token_like, network)
    Priority: ds_address > token_address > token_symbol (CCXT pair).
    - For symbols, we will call CCXT (e.g., TRX -> TRX/USDT).
    - You can still force on-chain by saving ds_address/token_address.
    """
    ds = (row.get("ds_address") or "").strip()
    taddr = (row.get("token_address") or "").strip()
    sym = (row.get("token_symbol") or "").strip().upper()
    network = (row.get("network") or None)
    if ds: return ds, network
    if taddr: return taddr, network
    if sym: return sym, network
    return "", network

def fetch_subscriptions_rsi(sup) -> list[dict]:
    """
    Load active RSI subscriptions. Prefers `strategy='rsi'`. If column missing, we also accept slow=0 as RSI.
    """
    # Has 'strategy' column
    try:
        rows = (sup.table("signal_subscriptions")
                  .select("token_symbol, token_address, ds_address, timeframe, fast, slow, network, strategy")
                  .eq("is_enabled", True)
                  .eq("strategy", "rsi")
                  .limit(500)
                  .execute()).data or []
    except Exception:
        rows = (sup.table("signal_subscriptions")
                  .select("token_symbol, token_address, ds_address, timeframe, fast, slow, network")
                  .eq("is_enabled", True)
                  .eq("slow", 0)
                  .limit(500)
                  .execute()).data or []

    targets = []
    for r in rows:
        token_like, net = _resolve_pair_for_fetch(r)
        if not token_like:
            continue
        targets.append({
            "token_symbol": (r.get("token_symbol") or "").upper() or None,
            "ds_address": (r.get("ds_address") or None),
            "token_address": (r.get("token_address") or None),
            "timeframe": (r.get("timeframe") or TIMEFRAME),
            "period": int(r.get("fast") or RSI_PERIOD),  # RSI period stored in fast
            "slow": int(r.get("slow") or 0),
            "network": net,
            "token_like": token_like,
        })
    print(f"[subs-rsi] loaded {len(targets)} active subscription(s)")
    return targets

# ------------- DB WRITE -------------

def upsert_signal_row(symbol: str | None, ds_address: str | None, token_address: str | None,
                      tf: str, period: int, signal: str, price: Decimal, crossed_at: datetime):
    core = ds_address or token_address or (symbol or "").upper()
    crossed_iso = crossed_at.isoformat()
    dedupe = f"{core}|{tf}|{period}|0|{crossed_iso}"
    row = {
        "token_symbol": (symbol or "").upper() or None,
        "ds_address": ds_address or None,
        "token_address": token_address or None,
        "fast": int(period),
        "slow": 0,
        "timeframe": tf,
        "signal": signal,
        "price": str(price),
        "crossed_at": crossed_iso,
        "source": "rsi",
        "dedupe_key": dedupe,
    }
    try:
        sb.table("signals").upsert(row, on_conflict="dedupe_key").execute()
        print(f"[signals] {core} {tf} RSI{period} {signal} @ {price}")
        return True
    except Exception as e:
        print(f"[signals] upsert error: {type(e).__name__}: {e}")
        return False

# ------------- MAIN RUN -------------

def process_once():
    new = 0
    targets = fetch_subscriptions_rsi(sb)
    for t in targets:
        token_like = t["token_like"]
        try:
            df = fetch_ohlcv_resolved(
                token_like,
                t["timeframe"],
                limit=max(500, t["period"] + 5),
                network=t.get("network")
            )
            info = last_rsi_cross(df, t["period"], ob=RSI_OB, os_=RSI_OS)
            if info:
                ok = upsert_signal_row(
                    symbol=t["token_symbol"],
                    ds_address=t["ds_address"],
                    token_address=t["token_address"],
                    tf=t["timeframe"],
                    period=t["period"],
                    signal=info["signal"],
                    price=info["price"],
                    crossed_at=info["crossed_at"],
                )
                if ok: new += 1
        except CandlesNotFound as ce:
            print(f"[rsi] no candles for {token_like} {t['timeframe']}: {ce}")
        except Exception as e:
            print(f"[rsi] error for {token_like}: {type(e).__name__}: {e}")
    print(f"[rsi] processed {len(targets)} target(s); new RSI signals: {new}")

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[ERR] SUPABASE_URL / SUPABASE_KEY missing in .env"); return

    if args.loop:
        while True:
            process_once()
            time.sleep(args.interval)
    else:
        process_once()

if __name__ == "__main__":
    main()
