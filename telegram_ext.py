import os, re, json, hashlib
from decimal import Decimal, getcontext
from subprocess import run, PIPE
from dotenv import load_dotenv
from supabase import create_client

getcontext().prec = 50

# ---------- ENV / CONFIG ----------
load_dotenv()

PRICE_SCALE = Decimal(os.getenv("PRICE_SCALE", "1000000"))
TOKEN_DECIMALS_DEFAULT = int(os.getenv("TOKEN_DECIMALS_DEFAULT", "6"))
ADDR_HEX = os.getenv("TOKEN_ADDR_HEX", "1") != "0"

TOKENS = json.loads(os.getenv("TOKEN_SYMBOLS_MAP", "{}"))  # symbol -> base58 addr
DECIMALS_MAP = json.loads(os.getenv("TOKEN_DECIMALS_MAP", "{}"))  # addr -> decimals

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_{SERVICE|ANON}_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- Address utils (Base58 → hex 41...) ----------
_B58_ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def _b58decode_check(s: str) -> bytes:
    num = 0
    for ch in s:
        num = num * 58 + _B58_ALPH.index(ch)
    full = num.to_bytes((num.bit_length() + 7)//8, "big")
    n_pad = len(s) - len(s.lstrip("1"))
    full = b"\x00"*n_pad + full
    if len(full) < 5:
        raise ValueError("bad base58")
    payload, checksum = full[:-4], full[-4:]
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if chk != checksum:
        raise ValueError("bad checksum")
    return payload

def tron_to_hex(addr: str) -> str:
    a = (addr or "").strip()
    if not a:
        return a
    if a.startswith("0x") or all(c in "0123456789abcdefABCDEF" for c in a):
        return a.lower().removeprefix("0x")
    return _b58decode_check(a).hex().lower()

# ---------- Helpers ----------
def decimals_for(token_addr: str) -> int:
    # accept both base58 and hex keys in DECIMALS_MAP
    return int(
        DECIMALS_MAP.get(token_addr)
        or DECIMALS_MAP.get(token_addr.lower())
        or DECIMALS_MAP.get(tron_to_hex(token_addr))
        or TOKEN_DECIMALS_DEFAULT
    )

def to_base_units(qty_h: Decimal, decimals: int) -> int:
    return int((qty_h * (Decimal(10) ** decimals)).to_integral_value())

def price_to_int(px_h: Decimal) -> int:
    return int((px_h * PRICE_SCALE).to_integral_value())

def fetch_open(token_addr_b58: str):
    # query using the same form as stored in DB
    key = tron_to_hex(token_addr_b58) if ADDR_HEX else token_addr_b58
    resp = supabase.table("open_trades").select("*").eq("token_address", key).limit(1).execute()
    data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
    return data[0] if isinstance(data, list) and data else None

# ---------- Core: /sell parser + emitter call ----------
SELL_RE = re.compile(r"^\s*/sell\s+([A-Za-z0-9_]+)\s+([\d.]+)\s*@\s*([\d.]+)\s*$")

def handle_sell(text: str):
    """
    Accepts: '/sell TUSDT 100 @ 0.5'
    Returns: (user_message, ok_bool)
    """
    m = SELL_RE.match(text or "")
    if not m:
        return "❌ Format: /sell <SYMBOL> <QTY> @ <PRICE>", False

    symbol, qty_s, px_s = m.groups()
    symbol = symbol.upper()
    try:
        qty_h = Decimal(qty_s)
        px_h = Decimal(px_s)
    except Exception:
        return "❌ Numbers look invalid. Example: /sell TUSDT 100 @ 0.5", False

    token_addr = TOKENS.get(symbol)
    if not token_addr:
        return f"❌ Unknown token '{symbol}'. Configure TOKEN_SYMBOLS_MAP.", False

    dec = decimals_for(token_addr)
    sell_amount_int = to_base_units(qty_h, dec)
    exit_price_int = price_to_int(px_h)

    open_row = fetch_open(token_addr)
    if not open_row:
        return f"⚠️ No open position for {symbol}. Use /buy first.", False

    trade_id = int(open_row["trade_id_onchain"])
    avg_entry_h = Decimal(str(open_row["avg_entry_price"]))
    open_amt_h = Decimal(str(open_row["amount"]))

    # Cap sell to available position (defensive)
    if qty_h > open_amt_h:
        qty_h = open_amt_h
        sell_amount_int = to_base_units(qty_h, dec)

    realized_h = (px_h - avg_entry_h) * qty_h
    pnl_int = price_to_int(realized_h)

    # Call your emitter with --sell-amount (NEW!)
    cmd = [
        "python", "emit_events.py", "close",
        "--trade-id", str(trade_id),
        "--token-address", token_addr,
        "--exit-price", str(exit_price_int),
        "--pnl", str(pnl_int),
        "--sell-amount", str(sell_amount_int),
    ]
    proc = run(cmd, stdout=PIPE, stderr=PIPE, text=True)

    if proc.returncode != 0:
        # show concise stderr to the user
        return f"❌ {proc.stderr.strip()}", False

    tx_line = proc.stdout.strip().splitlines()[-1] if proc.stdout else ""
    return f"✅ Submitted.\n{tx_line}", True
