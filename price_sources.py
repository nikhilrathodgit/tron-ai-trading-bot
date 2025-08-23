# price_sources.py
from __future__ import annotations
import re, os, json, time, math
import requests
from decimal import Decimal
from typing import Optional, List, Tuple
from urllib.parse import quote
import hashlib  # add


DEXSCREENER_BASE = os.getenv("DEXSCREENER_BASE", "https://api.dexscreener.com")
AVE_BASE         = os.getenv("AVE_BASE", "https://api.ave.ai")
GECKO_BASE       = os.getenv("GECKO_BASE", "https://api.geckoterminal.com")


# --- address detection (TRON + generic EVM 0x...) ---
_HEX41    = re.compile(r"^(?:0x)?41[0-9a-fA-F]{40}$", re.IGNORECASE)
_HEX0X    = re.compile(r"^(?:0x)[0-9a-fA-F]{40}$", re.IGNORECASE)   # generic EVM
_BASE58_T = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33,34}$")           # TRON base58


# base58 + checksum decode (TRON T-addresses)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def _b58decode_check(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    raw = b"\x00" * pad + raw
    if len(raw) < 5:
        raise ValueError("base58 too short")
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError("bad base58 checksum")
    return payload

# (keep your existing _b58decode_check here)

def _b58encode_check(payload: bytes) -> str:
    """Base58Check encode: payload + 4-byte double-SHA256 checksum."""
    import hashlib
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    raw = payload + chk
    n = int.from_bytes(raw, "big")
    out = []
    while n > 0:
        n, r = divmod(n, 58)
        out.append(_B58[r])
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(out)) if out else "1" * pad

def tron_to_base58(addr: str) -> Optional[str]:
    """
    Convert 41... or 0x... (20 bytes) to TRON base58 T... form. Return T... or None.
    """
    if not addr:
        return None
    s = addr.strip()
    if _BASE58_T.match(s):
        return s
    h = s.lower().removeprefix("0x")
    if h.startswith("41") and len(h) == 42:
        body = bytes.fromhex(h[2:])
    elif len(h) == 40:
        body = bytes.fromhex(h)
    else:
        return None
    return _b58encode_check(b"\x41" + body)


def tron_to_evm0x(addr: str) -> Optional[str]:
    """
    Best-effort EVM alias for a TRON address.
    - 41 + 20 bytes  ->  0x + 20 bytes
    - base58 T...    ->  decode to 41.. then 0x...
    Returns 0x.. or None.
    """
    if not addr:
        return None
    s = addr.strip()
    if s.lower().startswith("0x") and len(s) == 42:
        return "0x" + s[2:].lower()
    if s.lower().startswith("41") and len(s) == 42:
        return "0x" + s[2:].lower()
    if _BASE58_T.match(s):
        try:
            payload = _b58decode_check(s)
            if payload and payload[0] == 0x41 and len(payload) == 21:
                return "0x" + payload[1:].hex()
        except Exception:
            return None
    return None

# Map common Dexscreener chainId -> GeckoTerminal slug
_DS_CHAIN_TO_GECKO = {
    "ethereum": "eth",
    "bsc": "bsc",
    "polygon": "polygon",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "base": "base",
    "avalanche": "avax",
    "avalanche_c": "avax",
    "fantom": "fantom",
    "tron": "tron",
    "solana": "solana",
}

def guess_network_for_address(addr: str) -> Optional[str]:
    """
    Use Dexscreener search to infer the most likely chain for an address, then map
    to GeckoTerminal slug. Returns a slug like 'eth','bsc','tron',... or None.
    """
    try:
        q = quote(addr.strip())
        r = requests.get(f"{DEXSCREENER_BASE}/latest/dex/search?q={q}", timeout=12)
        if r.status_code != 200:
            return None
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return None

        def liq_usd(p):
            try:
                return float(((p.get("liquidity") or {}).get("usd")) or 0)
            except Exception:
                return 0.0

        best = max(pairs, key=liq_usd)
        chain = (best.get("chainId") or "").strip().lower()
        return _DS_CHAIN_TO_GECKO.get(chain)
    except Exception:
        return None


def is_token_address(s: str) -> bool:
   if not s: 
       return False
   s = s.strip()
   return bool(_BASE58_T.match(s) or _HEX41.match(s) or _HEX0X.match(s))

# ---------- prices ----------
class PriceNotFound(Exception): pass

def _ave_price_by_token(addr: str) -> Tuple[Decimal, dict]:
    """
    Ave.ai fallback. Endpoint can vary by chain; we try a generic token lookup.
    Returns (last_price_usd, token_json).
    """
    # 1) try pair search (broad)
    url = f"{AVE_BASE}/api/v2/token/overview?address={addr}"
    r = requests.get(url, timeout=15)
    if r.status_code == 404:
        raise PriceNotFound("ave.ai 404")
    r.raise_for_status()
    j = r.json() or {}
    price = j.get("priceUsd") or j.get("price")
    if price is None:
        raise PriceNotFound("ave.ai: no price field")
    return Decimal(str(price)), j

def lookup_evm_address_by_symbol(symbol: str) -> Optional[str]:
    """
    Search Dexscreener by symbol and return the base token's 0x address.
    Prefer exact symbol; else fall back to best partial (highest USD liquidity).
    """
    if not symbol:
        return None
    q = quote(symbol.strip())
    url = f"{DEXSCREENER_BASE}/latest/dex/search?q={q}"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json() or {}
    pairs = data.get("pairs") or []

    sym_up = symbol.strip().upper()
    # Exact symbol matches
    exacts = [p for p in pairs if ((p.get("baseToken") or {}).get("symbol","").upper() == sym_up)]

    def liq_usd(p):
        try:
            return float(((p.get("liquidity") or {}).get("usd")) or 0)
        except Exception:
            return 0.0

    # If no exacts, allow partial contains (helps when tickers include chars/emoji)
    candidates = exacts if exacts else [
        p for p in pairs
        if sym_up in ((p.get("baseToken") or {}).get("symbol","").upper())
    ]
    if not candidates:
        return None

    best = max(candidates, key=liq_usd)
    addr = ((best.get("baseToken") or {}).get("address") or "").strip()
    return addr if addr.lower().startswith("0x") and len(addr) == 42 else None



def fetch_onchain_price_and_meta(addr: str) -> Tuple[Decimal, str, str]:
    """
    Returns (price_usd, base_symbol, normalized_address_for_chain).
    Symbol is scraped from the DEX API.
    """
    try:
        px, meta = _dexscreener_price_by_token(addr)
        # Dexscreener uses: baseToken{symbol,address}, quoteToken{symbol}
        sym = (meta.get("baseToken", {}) or {}).get("symbol") or "UNKNOWN"
        norm_addr = (meta.get("baseToken", {}) or {}).get("address") or addr
        return px, sym.upper(), norm_addr
    except Exception:
        px, meta = _ave_price_by_token(addr)
        sym = (meta.get("symbol") or meta.get("tokenSymbol") or "UNKNOWN")
        norm_addr = meta.get("address") or addr
        return px, sym.upper(), norm_addr

# ---------- OHLCV for signal generator ----------
class CandlesNotFound(Exception): pass

def _dexscreener_price_by_token(addr: str) -> Tuple[Decimal, dict]:
    """
    Dexscreener: GET /latest/dex/tokens/{address}
    Returns (last_price_usd, chosen_pair_json).
    Tries the given address form and, if it's TRON, also tries the EVM 0x alias.
    """
    addr = (addr or "").strip()
    cands = []
    if addr:
        cands.append(addr.lower() if addr.startswith("41") else addr)
    evm = tron_to_evm0x(addr)
    if evm:
        cands.append(evm)

    seen = set()
    last_err = None
    for q in cands:
        qn = q.lower()
        if not qn or qn in seen:
            continue
        seen.add(qn)
        try:
            url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{q}"
            r = requests.get(url, timeout=15); r.raise_for_status()
            data = r.json() or {}
            pairs = data.get("pairs") or []
            if not pairs:
                continue
            best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            price = best.get("priceUsd") or best.get("priceNative")
            if price is None:
                continue
            return Decimal(str(price)), best
        except Exception as e:
            last_err = e
            continue
    raise PriceNotFound("no pairs on dexscreener")


# ---------- OHLCV for signal generator ----------
class CandlesNotFound(Exception):
    pass

def _dexscreener_candles(addr: str, interval: str, limit: int = 300):
    """
    Dexscreener candles API (TradingView-like). Many chains support:
      /chart/bars/{pairId}?from=...&to=...&resolution=1
    We:
      1) resolve a pair via /latest/dex/tokens/{q}, falling back to /latest/dex/search?q={q}
      2) pick the most-liquid pair
      3) fetch bars for the requested resolution
    """
    addr = (addr or "").strip()

    # candidate identifiers to try: as‑given, evm 0x alias, TRON base58 T...
    cands = []
    if addr:
        cands.append(addr)
    evm = tron_to_evm0x(addr)
    if evm:
        cands.append(evm)
    t58 = tron_to_base58(addr)
    if t58:
        cands.append(t58)

    def _pick_best_pair(pairs: list[dict]) -> dict | None:
        if not pairs:
            return None
        def liq_usd(p):
            try:
                return float(((p.get("liquidity") or {}).get("usd")) or 0)
            except Exception:
                return 0.0
        return max(pairs, key=liq_usd)

    seen = set()
    for q in cands:
        qn = q.lower()
        if not qn or qn in seen:
            continue
        seen.add(qn)
        try:
            # 1) try exact token lookup
            url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{q}"
            r = requests.get(url, timeout=15); r.raise_for_status()
            pairs = (r.json() or {}).get("pairs") or []

            # 2) fallback: global search
            if not pairs:
                url2 = f"{DEXSCREENER_BASE}/latest/dex/search?q={q}"
                r2 = requests.get(url2, timeout=15); r2.raise_for_status()
                pairs = (r2.json() or {}).get("pairs") or []

            best = _pick_best_pair(pairs)
            if not best:
                continue
            pair_id = best.get("pairId")
            if not pair_id:
                continue

            # map timeframe → resolution
            tf = interval.lower()
            res_map = {"1m":"1","3m":"3","5m":"5","10m":"10","15m":"15","30m":"30",
                       "1h":"60","2h":"120","4h":"240","6h":"360","12h":"720",
                       "1d":"1D","3d":"3D"}
            resolution = res_map.get(tf)
            if not resolution:
                raise CandlesNotFound(f"unsupported timeframe: {interval}")

            now = int(time.time())
            approx_sec = 60 if tf.endswith("m") else (3600 if tf.endswith("h") else 86400)
            mul = int(re.sub(r"[^\d]", "", resolution)) if not resolution.endswith("D") else 1440
            span = max(200, limit) * approx_sec * max(1, mul)
            frm = now - span

            bars_url = f"{DEXSCREENER_BASE}/chart/bars/{pair_id}?from={frm}&to={now}&resolution={resolution}"
            r3 = requests.get(bars_url, timeout=20); r3.raise_for_status()
            bars = r3.json() or []
            if not isinstance(bars, list) or not bars:
                continue

            # CCXT‑style rows: [timestamp(ms), open, high, low, close, volume]
            out = []
            for b in bars[-limit:]:
                ts_ms = int(float(b["time"]) * 1000)
                out.append([ts_ms,
                            float(b["open"]), float(b["high"]), float(b["low"]),
                            float(b["close"]), float(b.get("volume", 0))])
            return out
        except Exception:
            continue

    raise CandlesNotFound("no pairs")

def fetch_ohlcv_like_ccxt(symbol_or_addr: str, timeframe: str, limit: int = 300, network: str | None = None):
    """
    If input looks like an address:
      1) Try GeckoTerminal (token -> best pool -> OHLCV, or pool directly) using the provided
         or auto-guessed network slug.
      2) If that fails or returns no rows, fall back to Dexscreener candles.
    Else:
      Raise CandlesNotFound (CCXT symbols handled elsewhere).
    """
    if not is_token_address(symbol_or_addr):
        raise CandlesNotFound("not an on-chain address")

    addr = symbol_or_addr.strip()

    # 1) GeckoTerminal primary
    try:
        net = (network or guess_network_for_address(addr))
        if net:
            # Try as a pool first
            rows = _gt_ohlcv_by_pool(net, addr, timeframe, limit)
            if not rows:
                # Resolve best pool for a token, then fetch
                pool = _gt_best_pool_for_token(net, addr)
                if pool:
                    rows = _gt_ohlcv_by_pool(net, pool, timeframe, limit)
            if rows:
                return rows
    except Exception:
        pass  # continue to DS fallback

    # 2) Dexscreener fallback
    return _dexscreener_candles(addr, timeframe, limit)



def _gt_best_pool_for_token(network: str, token_addr: str) -> Optional[str]:
    """
    GeckoTerminal: list pools for a token; return the most-liquid pool address.
    GET /api/v3/onchain/networks/{network}/tokens/{token}/pools
    """
    url = f"{GECKO_BASE}/api/v3/onchain/networks/{network}/tokens/{token_addr}/pools"
    r = requests.get(url, timeout=15); r.raise_for_status()
    j = r.json() or {}
    data = j.get("data") or []
    if not isinstance(data, list) or not data:
        return None

    def liq_usd(item):
        try:
            attrs = item.get("attributes") or {}
            # GT uses reserve_usd or reserve_in_usd depending on network
            return float(attrs.get("reserve_in_usd") or attrs.get("reserve_usd") or 0)
        except Exception:
            return 0.0

    best = max(data, key=liq_usd)
    attrs = best.get("attributes") or {}
    return attrs.get("address") or attrs.get("pool_address")


def _gt_ohlcv_by_pool(network: str, pool_addr: str, timeframe: str, limit: int = 300):
    """
    GeckoTerminal OHLCV by pool.
    GET /api/v3/onchain/networks/{network}/pools/{pool}/ohlcv/{timeframe}
    Returns CCXT-like list: [ts(ms), open, high, low, close, volume]
    """
    tf = (timeframe or "").lower()
    url = f"{GECKO_BASE}/api/v3/onchain/networks/{network}/pools/{pool_addr}/ohlcv/{tf}"
    r = requests.get(url, timeout=20); r.raise_for_status()
    j = r.json() or {}
    data = j.get("data") or []

    out = []

    # Shape A: attributes has 'ohlcv_list' as arrays [ts_sec, o,h,l,c,v]
    if isinstance(data, dict):
        attrs = data.get("attributes") or {}
        rows = attrs.get("ohlcv_list") or []
        for row in rows[-limit:]:
            ts_ms = int(float(row[0]) * 1000)
            out.append([ts_ms, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
        return out

    # Shape B: data is a list of { attributes: {timestamp, open, high, low, close, volume}}
    if isinstance(data, list) and data:
        for item in data[-limit:]:
            attrs = (item or {}).get("attributes") or {}
            ts_sec = attrs.get("timestamp") or attrs.get("time") or attrs.get("t")
            if ts_sec is None:
                continue
            ts_ms = int(float(ts_sec) * 1000)
            out.append([
                ts_ms,
                float(attrs.get("open")), float(attrs.get("high")),
                float(attrs.get("low")), float(attrs.get("close")),
                float(attrs.get("volume") or 0),
            ])
        return out

    return out

