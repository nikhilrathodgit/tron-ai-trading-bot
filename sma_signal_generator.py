#!/usr/bin/env python3
# sma_signal_writer.py
import os, time, json
from datetime import datetime, timezone
from decimal import Decimal
import pandas as pd
import ccxt
from ccxt.base.errors import RequestTimeout, NetworkError
from dotenv import load_dotenv
from supabase import create_client

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
import ccxt
# ...
ex = getattr(ccxt, EXCHANGE)({
    "enableRateLimit": True,
    "timeout": 30000,     # 30s instead of ~10s default
})
ex.load_markets()

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
        "pair": pair_norm,
        "fast": fast,
        "slow": slow,
        "timeframe": tf,
        "signal": signal,
        "price": str(price),
        "crossed_at": crossed_at.isoformat(),
        "source": "sma",
        "dedupe_key": dedupe,
        "extra": {},
    }
    # one shot, idempotent
    sb.table("signals").upsert(row, on_conflict="dedupe_key").execute()
    print(f"[signals] {pair_norm} {tf} SMA{fast}/{slow} {signal} @ {price}")

def normalize_pair_ccxt(pair_norm: str) -> str:
    # 'TRXUSDT' -> 'TRX/USDT'
    pair_norm = (pair_norm or "").upper()
    return f"{pair_norm[:-4]}/USDT" if pair_norm.endswith("USDT") else pair_norm

def fetch_subscriptions(sb):
    r = sb.table("signal_subscriptions").select("*").eq("is_enabled", True).execute()
    return r.data or []


import argparse, time

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="poll forever")
    parser.add_argument("--interval", type=int, default=60, help="poll seconds between runs")
    args = parser.parse_args()

    while True:
        new_signals = 0
        try:
            # 1) Load all enabled subscriptions (may be empty)
            subs = fetch_subscriptions(sb)
        except Exception as e:
            print(f"[run] error loading subscriptions: {type(e).__name__}: {e}")
            subs = []

        if subs:
            # 2) Process each subscription independently
            for s in subs:
                pair_norm = s.get("pair")            # e.g., 'TRXUSDT'
                tf        = s.get("timeframe")       # e.g., '1m'
                fast      = int(s.get("fast", 0))
                slow      = int(s.get("slow", 0))

                # skip bad rows
                if not pair_norm or not tf or fast <= 0 or slow <= 0:
                    print(f"[run] skip invalid sub: {s}")
                    continue

                try:
                    pair_ccxt = normalize_pair_ccxt(pair_norm)  # 'TRX/USDT'
                    # fetch candles (resamples if TF is non-native)
                    lookback = max(500, max(fast, slow) + 5)
                    df = fetch_ohlcv_resampled(pair_ccxt, tf, limit=lookback)

                    info = last_crossover(df, fast, slow)  # {'signal','price','crossed_at'} or None
                    if info:
                        upsert_signal(
                            pair_norm, tf, fast, slow,
                            info["signal"], info["price"], info["crossed_at"]
                        )
                        new_signals += 1
                except Exception as e:
                    # Keep the loop alive even if this one sub fails
                    print(f"[run] {pair_norm} {tf} error: {type(e).__name__}: {e}")
        else:
            # 3) Fallback: single target from .env (runs if no subs exist)
            try:
                pair = PAIR_INPUT.upper().replace("-", "/").replace("USDTUSDT", "USDT")
                lookback = max(500, MIN_BARS + 5)
                df = fetch_ohlcv_resampled(pair, TIMEFRAME, limit=lookback)

                info = last_crossover(df, FAST, SLOW)
                if info:
                    upsert_signal(
                        pair.replace("/", ""), TIMEFRAME, FAST, SLOW,
                        info["signal"], info["price"], info["crossed_at"]
                    )
                    new_signals += 1
                else:
                    # Optional: keep this message to understand single-shot behaviour
                    if not args.loop:
                        print("❌ Crossover has not occurred yet")
            except Exception as e:
                print(f"[run] fallback error: {type(e).__name__}: {e}")

        # 4) Run summary
        processed_targets = len(subs) if subs else 1
        print(f"[run] processed {processed_targets} target(s); new signals: {new_signals}")

        # 5) One-shot vs loop
        if not args.loop:
            break

        time.sleep(args.interval)

if __name__ == "__main__":
    main()
