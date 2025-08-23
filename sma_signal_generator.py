#!/usr/bin/env python3
# sma_signal_writer.py
import os, time, json
from datetime import datetime, timezone
from decimal import Decimal
import pandas as pd
import price_sources as PS
from price_sources import is_token_address, fetch_ohlcv_like_ccxt, CandlesNotFound

import ccxt
from ccxt.base.errors import RequestTimeout, NetworkError
from dotenv import load_dotenv
from supabase import create_client
import argparse

load_dotenv()
SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY") 
EXCHANGE      = os.getenv("SIGNALS_EXCHANGE", "binance")   
PAIR_INPUT    = os.getenv("SIGNALS_PAIR", "TRX/USDT")      # normalize to CCXT format "TRX/USDT"
TIMEFRAME     = os.getenv("SIGNALS_TIMEFRAME", "1m")
FAST          = int(os.getenv("SIGNALS_FAST", "10"))
SLOW          = int(os.getenv("SIGNALS_SLOW", "30"))
MIN_BARS      = max(FAST, SLOW) + 5

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
# ...
ex = getattr(ccxt, EXCHANGE)({
    "enableRateLimit": True,
    "timeout": 30000,     # 30s instead of ~10s default
})
ex.load_markets()

# Default quote for market data (can override in .env)
DEFAULT_QUOTE = os.getenv("SIGNALS_QUOTE", "USDT").upper()

def ccxt_pair_from_symbol(symbol: str, quote: str = DEFAULT_QUOTE):
    """
    Build a CCXT pair from a base symbol + quote (default USDT).
    Returns (ccxt_pair, base, quote) or (None, None, None) if invalid.
    """
    base = (symbol or "").strip().upper()
    if not base or not quote:
        return None, None, None
    return f"{base}/{quote}", base, quote

def normalize_timeframe(tf: str) -> str:
    """Accept 1m 5m 10m 15m 30m 1h 3h 4h 6h 12h 1d 3d etc. Return as-is (we resample if needed)."""
    return (tf or "").strip().lower()


# --- add near the top, after your imports / dotenv load ---
def normalize_pair(p: str):
    """
    Return (BASE, QUOTE, CCXT_PAIR)
    Accepts 'TRX/USDT', 'trx-usdt', 'TRXUSDT', or just 'TRX' (assumes USDT).
    """
    s = (p or "").strip().upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
    else:
        # no slash: try ...USDT suffix
        if s.endswith("USDT"):
            base, quote = s[:-4], "USDT"
        else:
            base, quote = s, "USDT"
    return base, quote, f"{base}/{quote}"


def fetch_ohlcv_safe(pair: str, timeframe: str, limit=500, max_retries=5):
    delay = 1.5
    for attempt in range(1, max_retries + 1):
        try:
            # smaller limit lowers response size; 300 is plenty for SMA(1/2)
            return ex.fetch_ohlcv(pair, timeframe=timeframe, limit=min(limit, 300))
        except (RequestTimeout, NetworkError) as e:
            if attempt == max_retries:
                raise
            print(f"[warn] {type(e).__name__} on {pair} {timeframe}, retry {attempt}/{max_retries} in {delay:.1f}s")
            time.sleep(delay)
            delay *= 1.8


def fetch_ohlcv(pair: str, timeframe: str, limit=500):
    raw = fetch_ohlcv_safe(pair, timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","vol"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def fetch_ohlcv_resampled(pair: str, tf: str, limit=1000):
    native = getattr(ex, "timeframes", {})
    if tf in native:
        return fetch_ohlcv(pair, tf, limit=limit)

    # fallback: resample from 1m
    base = "1m"
    base_df = fetch_ohlcv(pair, base, limit=limit)
    rule = tf.replace("m", "T").upper()  # '10m' -> '10T', '3h'-> '3H', '3d'-> '3D'
    idx = base_df.set_index("dt")
    res = pd.DataFrame({
        "open":  idx["open"].resample(rule).first(),
        "high":  idx["high"].resample(rule).max(),
        "low":   idx["low"].resample(rule).min(),
        "close": idx["close"].resample(rule).last(),
        "vol":   idx["vol"].resample(rule).sum(),
    }).dropna().reset_index()
    return res

def fetch_ohlcv_resolved(pair: str, timeframe: str, limit=500, network: str | None = None):
    """
    Accepts 'TRX/USDT' or an on-chain address (0x… / T… / 41…).
    If address: delegate to price_sources with optional network (GeckoTerminal primary).
    """
    if is_token_address(pair):
        rows = PS.fetch_ohlcv_like_ccxt(pair, timeframe, limit=min(limit, 500), network=network)
        import pandas as pd
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df
    return fetch_ohlcv_resampled(pair, timeframe, limit=limit)




def last_crossover(df: pd.DataFrame, fast: int, slow: int):
    """
    Detects the latest crossover:
    BUY  when sma_fast crosses above sma_slow
    SELL when sma_fast crosses below sma_slow
    Returns dict or None.
    """
    df = df.copy()
    df["sma_fast"] = df["close"].rolling(fast).mean()
    df["sma_slow"] = df["close"].rolling(slow).mean()
    df = df.dropna()
    if len(df) < 2: return None

    # Look at last two points for sign change
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_diff = prev["sma_fast"] - prev["sma_slow"]
    curr_diff = curr["sma_fast"] - curr["sma_slow"]

    if prev_diff < 0 and curr_diff > 0:
        sig = "BUY"
    elif prev_diff > 0 and curr_diff < 0:
        sig = "SELL"
    else:
        return None

    return {
        "signal": sig,
        "price": Decimal(str(curr["close"])),
        "crossed_at": curr["dt"].to_pydatetime(),  # aware UTC
    }

def minutes_ago(dt: datetime):
    now = datetime.now(timezone.utc)
    return int((now - dt).total_seconds() // 60)

def upsert_signal(pair_norm: str, tf: str, fast: int, slow: int,
                  signal: str, price, crossed_at):
    dedupe = f"{pair_norm}|{tf}|{fast}|{slow}|{crossed_at.isoformat()}"
    row = {
        "token_symbol": BASE.upper(),     # e.g., "TRX"
        "fast": fast,
        "slow": slow,
        "timeframe": tf,
        "signal": signal,
        "price": str(price),
        "crossed_at": crossed_at.isoformat(),
        "source": "sma",
        "dedupe_key": f"{BASE.upper()}|{tf}|{fast}|{slow}|{crossed_at.isoformat()}",
    }
    sb.table("signals").insert(row).execute()

    try:
        resp = sb.table("signals").upsert(row, on_conflict="dedupe_key").execute()
        data = getattr(resp, "data", None)
        if data is None:
            # PostgREST returns error body on failure; print whole resp
            print("[signals] insert/upsert returned no data. Response:", resp)
        else:
            print(f"[signals] {pair_norm} {tf} SMA{fast}/{slow} {signal} @ {price}")
    except Exception as e:
        print(f"[signals] upsert error: {type(e).__name__}: {e}")

def normalize_pair_ccxt(pair_norm: str) -> str:
    # 'TRXUSDT' -> 'TRX/USDT'
    pair_norm = (pair_norm or "").upper()
    return f"{pair_norm[:-4]}/USDT" if pair_norm.endswith("USDT") else pair_norm

def fetch_subscriptions(sb):
    """
    Load active subscriptions. Prefer ds_address for Dexscreener/Gecko fetches.
    """
    try:
        res = (sb.table("signal_subscriptions")
                 .select("id, token_symbol, ds_address, network, token_address, fast, slow, timeframe, is_enabled, tg_chat_id")
                 .eq("is_enabled", True)
                 .execute())
        rows = getattr(res, "data", None) or []
    except Exception as e:
        print(f"[subs] load error: {type(e).__name__}: {e}")
        return []

    targets = []
    for s in rows:
        sym   = (s.get("token_symbol") or "").strip().upper()
        ds    = (s.get("ds_address") or "").strip()
        net   = (s.get("network") or "").strip().lower() or None
        taddr = (s.get("token_address") or "").strip()
        try:
            fast = int(s.get("fast", 0)); slow = int(s.get("slow", 0))
        except Exception:
            continue
        tf = normalize_timeframe(s.get("timeframe"))
        if fast <= 0 or slow <= 0 or not tf:
            continue
        f, sl = (fast, slow) if fast <= slow else (slow, fast)

        if ds:
            pair_for_fetch = ds             # exact address user typed
        elif taddr:
            pair_for_fetch = taddr          # legacy TRON
        else:
            pair_ccxt, base, _ = ccxt_pair_from_symbol(sym or "")
            if not pair_ccxt:
                print(f"[subs] skip invalid CCXT pair: {s}"); continue
            pair_for_fetch = pair_ccxt

        targets.append({
            "token_symbol": sym or "UNKNOWN",
            "ds_address": ds or None,
            "network": net,                         # <— keep the network slug
            "token_address": taddr or None,
            "fast": f, "slow": sl, "timeframe": tf,
            "pair_for_fetch": pair_for_fetch,
        })

    print(f"[subs] loaded {len(targets)} active subscription(s)")
    return targets



def upsert_signal_row(symbol: str, ds_address: str | None, token_address: str | None,
                      tf: str, fast: int, slow: int, signal: str,
                      price: Decimal, crossed_at: datetime):
    """
    Write a row into signals with a dedupe key that prefers ds_address > token_address > symbol.
    """
    core = (ds_address or token_address or (symbol or "").upper())
    crossed_iso = crossed_at.isoformat()
    dedupe = f"{core}|{tf}|{fast}|{slow}|{crossed_iso}"

    row = {
        "token_symbol": (symbol or "").upper() or None,
        "ds_address": ds_address or None,
        "token_address": token_address or None,
        "fast": fast,
        "slow": slow,
        "timeframe": tf,
        "signal": signal,
        "price": str(price),
        "crossed_at": crossed_iso,
        "source": "sma",
        "dedupe_key": dedupe,
    }
    try:
        sb.table("signals").upsert(row, on_conflict="dedupe_key").execute()
        print(f"[signals] {core} {tf} SMA{fast}/{slow} {signal} @ {price}")
        return True
    except Exception as e:
        print(f"[signals] upsert error: {type(e).__name__}: {e}")
        return False



def upsert_signal_token(symbol: str, tf: str, fast: int, slow: int,
                        signal: str, price: Decimal, crossed_at: datetime):
    dedupe = f"{symbol}|{tf}|{fast}|{slow}|{crossed_at.isoformat()}"
    row = {
        "token_symbol": symbol,
        "fast": fast,
        "slow": slow,
        "timeframe": tf,
        "signal": signal,
        "price": str(price),
        "crossed_at": crossed_at.isoformat(),
        "source": "sma",
        "dedupe_key": dedupe,
    }
    # insert if not exists (dedupe_key UNIQUE in DB)
    existing = (sb.table("signals")
                  .select("id")
                  .eq("dedupe_key", dedupe)
                  .maybe_single()
                  .execute())
    if not getattr(existing, "data", None):
        sb.table("signals").insert(row).execute()
        print(f"[signals] {symbol} {tf} SMA{fast}/{slow} {signal} @ {price}")
        return True
    return False

def process_once():
    new = 0
    targets = fetch_subscriptions(sb)
    for t in targets:
        try:
            # fetch candles with your existing resolver
            df = fetch_ohlcv_resolved(
                t["pair_for_fetch"],
                t["timeframe"],
                limit=max(500, max(t["fast"], t["slow"]) + 5),
                network=t.get("network")
                )

            info = last_crossover(df, t["fast"], t["slow"])
            if info:
                ok = upsert_signal_row(
                    symbol=t["token_symbol"],
                    ds_address=t["ds_address"],
                    token_address=t["token_address"],
                    tf=t["timeframe"],
                    fast=t["fast"], slow=t["slow"],
                    signal=info["signal"],
                    price=info["price"],
                    crossed_at=info["crossed_at"]
                )
                if ok:
                    new += 1
        except (RequestTimeout, NetworkError) as ne:
            what = t.get("pair_for_fetch") or "pair"
            print(f"[warn] network timeout for {what} {t['timeframe']}: {ne}")
        except CandlesNotFound as ce:
            print(f"[run] no candles for {t['pair_for_fetch']} {t['timeframe']}: {ce}")
        except Exception as e:
            print(f"[run] error for {t}: {type(e).__name__}: {e}")
    print(f"[run] processed {len(targets)} target(s); new signals: {new}")



def main():
    import argparse, time
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
