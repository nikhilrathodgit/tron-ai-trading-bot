#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
from synthetic_addr import make_synth_hex41
from price_sources import is_token_address, fetch_onchain_price_and_meta, guess_network_for_address
import os, sys, json, re, asyncio, logging, hashlib, subprocess
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from secrets import token_urlsafe
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.keyboard import InlineKeyboardBuilder
from search_tools import (
    get_token_price, explain_indicator,
    research_strategies, research_general,
    tron_dex_volume_24h
)
from aiogram.enums import ChatAction
from postgrest.exceptions import APIError  # top
from dotenv import load_dotenv
from supabase import create_client
import ccxt
import logging
from datetime import datetime, timezone
# --- SQL agent (optional /ask) ---
from agent import ask_db


# ---------- env ----------
load_dotenv()

SB_URL = os.getenv("SUPABASE_URL")
SB_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
sb = create_client(SB_URL, SB_KEY) if (SB_URL and SB_KEY) else None

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
EMITTER_PATH = os.getenv("EMIT_SCRIPT_PATH", "emit_events.py")


PRICE_SCALE = Decimal(os.getenv("PRICE_SCALE", "1000000"))
DEC_DEFAULT = int(os.getenv("TOKEN_DECIMALS_DEFAULT", "6"))
# SYMBOL to base58 address, e.g. {"TUSDT":"TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"}
SYMBOLS_MAP = json.loads(os.getenv("TOKEN_SYMBOLS_MAP", "{}"))
# SYMBOL to decimals, e.g. {"TUSDT":6}
DECIMALS_MAP = json.loads(os.getenv("TOKEN_DECIMALS_MAP", "{}"))
STRATEGY_NAME = os.getenv("STRATEGY_NAME", "MANUAL")
ADDR_HEX = os.getenv("TOKEN_ADDR_HEX", "1") != "0"  # if your DB stores 41.. hex

if not BOT_TOKEN:
    print("[ERR] TELEGRAM_BOT_TOKEN missing in .env")
    sys.exit(1)

PY = sys.executable

# --- Strategy memory (per chat) ---
LAST_STRAT: dict[int, str] = {}

def set_strategy_for_chat(chat_id: int, strat: str):
    if not strat:
        return
    s = strat.strip().upper()
    if s not in ("SMA", "RSI", "MANUAL"):
        s = "MANUAL"
    LAST_STRAT[chat_id] = s

def strategy_for_chat(chat_id: int) -> str:
    # prefer last picked in wizard; else .env STRATEGY_NAME; else MANUAL
    s = (LAST_STRAT.get(chat_id) or STRATEGY_NAME or "MANUAL").upper()
    return s if s in ("SMA", "RSI", "MANUAL") else "MANUAL"


# ---------- logging ----------


# ---------- helpers ----------

# --- Rebuild helpers ---

def _listener_path() -> str:
    """Resolve absolute path to tron_listener3.py next to this file."""
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(here, "tron_listener3.py")

def _py_exe() -> str:
    """Current interpreter (venv-safe) for subprocess calls."""
    return sys.executable

def _safe_delete_all(table: str, filter_col: str = None):
    """
    Delete all rows from a table. If PostgREST requires a filter, we use a broad 'neq' on a known column.
    """
    q = sb.table(table).delete()
    if filter_col:
        # wide-true filter to satisfy 'must provide a filter' settings on some Supabase projects
        q = q.neq(filter_col, "__never__")
    q.execute()

# --- logging hygiene (put this near the top of telegram_bot.py) ---


# keep our own prints minimal; or switch to logger.info if you prefer
logging.basicConfig(
    level=logging.INFO,  # you can use WARNING to be extra quiet
    format="%(levelname)s:%(name)s:%(message)s",
)

logging.getLogger("aiogram.client.session").setLevel(logging.WARNING)
logging.getLogger("aiogram.dispatcher.dispatcher").setLevel(logging.INFO)


# silence noisy libs
for noisy in (
    "httpx",          # Supabase client HTTP logs
    "httpcore",       # lower-level HTTP logs
    "aiogram",        # framework logs
    "aiogram.event",
    "aiogram.dispatcher",
    "postgrest",      # supabase-py stack
    "gotrue",
    "storage3",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# (optional) if you want *no* library chatter at all:
# for n in (...):
#     logging.getLogger(n).setLevel(logging.ERROR)


# ---- Signals: parsing & normalization ----

# ---- CCXT (market price) ----
_EX_NAME = os.getenv("MARKET_EXCHANGE", "binance")
_ex = getattr(ccxt, _EX_NAME)({"enableRateLimit": True, "timeout": 20000})
try:
    _ex.load_markets()
except Exception:
    pass

def ccxt_symbol(symbol: str) -> str:
    """'trx' -> 'TRX/USDT'"""
    return f"{symbol.upper()}/USDT"

def _money(x):
    try:
        f = float(Decimal(str(x)))
        return f"${f:,.2f}"
    except Exception:
        return str(x)


async def get_market_price(symbol: str) -> Decimal:
    """Fetch last price from exchange for BASE/USDT."""
    def _fetch():
        t = _ex.fetch_ticker(ccxt_symbol(symbol))
        return Decimal(str(t["last"]))
    return await asyncio.to_thread(_fetch)

# ---- token address/decimals helpers ----
def token_address_for_symbol(sym: str) -> str | None:
    s = (sym or "").upper()
    # try direct key, or tolerate 'TRXUSDT' style
    return SYMBOLS_MAP.get(s) or (SYMBOLS_MAP.get(s[:-4]) if s.endswith("USDT") else None)

def token_decimals_for_address(addr: str) -> int:
    if not addr:
        return DEC_DEFAULT
    return int(DECIMALS_MAP.get(addr) or DECIMALS_MAP.get(addr.lower()) or DEC_DEFAULT)

def token_address_for_symbol(symbol: str) -> str:
    """
    Resolve the address for a symbol:
    - If present in TOKEN_SYMBOLS_MAP, return that (Base58 T… or hex 41… both ok).
    - Otherwise return a deterministic synthetic 41… hex address.
    """
    sym = (symbol or "").strip().upper()
    addr = SYMBOLS_MAP.get(sym)
    if addr:
        return addr  # emitter accepts base58 or hex
    return make_synth_hex41(sym)  # hex 41… (valid address encoding for params)


import time
# ...
PENDING_SELLS: dict[int, dict] = {}
# Track which chats have requested /rebuild and await confirmation
PENDING_REBUILD = set()

SELL_CONFIRM_TIMEOUT_S = int(os.getenv("SELL_CONFIRM_TIMEOUT", "180"))

PENDING_RM: dict[int, dict] = {}
def set_pending_rm(chat_id: int, payload: dict): PENDING_RM[chat_id] = {"at": time.time(), **payload}
def pop_pending_rm(chat_id: int) -> dict | None:
    data = PENDING_RM.get(chat_id)
    if not data: return None
    if time.time() - data["at"] > SELL_CONFIRM_TIMEOUT_S:
        del PENDING_RM[chat_id]; return None
    return PENDING_RM.pop(chat_id)


def set_pending_sell(chat_id: int, payload: dict):
    PENDING_SELLS[chat_id] = {"at": time.time(), **payload}

def pop_pending_sell(chat_id: int) -> dict | None:
    data = PENDING_SELLS.get(chat_id)
    if not data:
        return None
    if time.time() - data["at"] > SELL_CONFIRM_TIMEOUT_S:
        del PENDING_SELLS[chat_id]
        return None
    return PENDING_SELLS.pop(chat_id)


# ---- scaling helpers ----
def scale_price(p: Decimal) -> int:
    return int((p * PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))

def scale_amount(units: Decimal, decimals: int) -> int:
    q = Decimal(10) ** decimals
    return int((units * q).to_integral_value(rounding=ROUND_HALF_UP))

def quant_amount(units: Decimal, decimals: int) -> Decimal:
    step = Decimal(1) / (Decimal(10) ** decimals)
    return units.quantize(step, rounding=ROUND_HALF_UP)

# ---------- alias helpers ----------
def save_alias(alias: str, canonical: str):
    if not sb or not alias or not canonical:
        return
    a = alias.strip().lower()
    sb.table("token_aliases").upsert(
        {"alias": a, "canonical_address": canonical}
    ).execute()

def resolve_alias(token_or_addr: str) -> str | None:
    if not sb or not token_or_addr:
        return None
    a = token_or_addr.strip().lower()
    resp = sb.table("token_aliases").select("canonical_address").eq("alias", a).limit(1).execute()
    rows = getattr(resp, "data", None) or []
    return rows[0]["canonical_address"] if rows else None

def fetch_open_row_by_address(canon_addr: str):
    resp = (sb.table("open_trades")
              .select("token_symbol, token_address, trade_id_onchain, amount, avg_entry_price")
              .eq("token_address", canon_addr)
              .limit(1).execute())
    data = getattr(resp, "data", None) or []
    return data[0] if data else None

def fetch_open_rows_by_symbol(symbol: str):
    sym = (symbol or "").strip().upper()
    resp = (sb.table("open_trades")
              .select("token_symbol, token_address, trade_id_onchain, amount, avg_entry_price")
              .eq("token_symbol", sym).execute())
    return getattr(resp, "data", None) or []




ALLOWED_TF = {"1m","5m","10m","15m","30m","1h","3h","4h","6h","12h","1d","3d"}

def norm_pair_to_usdt(sym: str) -> str:
    """
    Accepts: 'trx', 'TRX', 'TRX/USDT', 'TRXUSDT', 'tusdt'
    Returns: 'TRXUSDT'
    """
    s = (sym or "").upper().strip().replace("-", "").replace(" ", "")
    if "/" in s:
        left, right = s.split("/", 1)
        return f"{left}USDT" if right == "USDT" else s.replace("/", "")
    if s.endswith("USDT"):
        return s
    # Let users pass the stable directly (TUSDT), we still store a pair name
    if s == "USDT" or s == "TUSDT":
        return "TRXUSDT"  # default demo pair; change if you want a different base
    return f"{s}USDT"


# --- TRON base58 -> hex (lowercased '41...') ---
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


async def fetch_open_row_by_symbol(symbol: str):
    resp = (sb.table("open_trades")
              .select("token_symbol, token_address, trade_id_onchain, amount, avg_entry_price")
              .eq("token_symbol", symbol.upper())
              .limit(1)
              .execute())
    data = getattr(resp, "data", None) or []
    return data[0] if data else None


def token_decimals(symbol: str) -> int:
    return int(DECIMALS_MAP.get(symbol.upper(), DEC_DEFAULT))

def normalize_tron_addr(a: str) -> str:
    """
    Accepts base58 T..., hex 41..., or bare 20-byte hex.
    Returns a TRON-valid string (T... unchanged; 41... unchanged; 20-byte -> 41 + 20bytes).
    """
    if not a:
        return a
    s = a.strip()
    if s[0] in ("T", "t"):
        return s                           # base58 OK
    h = s.lower().removeprefix("0x")
    if h.startswith("41") and len(h) == 42:
        return h                           # 21 bytes hex OK
    if len(h) == 40:
        return "41" + h                    # fix bare 20-byte hex
    return h


# --- Signals: formatting helpers ---
from datetime import datetime, timezone
from decimal import Decimal

def fmt_price(x):
    try:
        return f"{Decimal(str(x)):.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)

def format_signal(sig: dict) -> str:
    # sig has pair, signal, price, crossed_at, fast, slow, timeframe
    when = sig.get("crossed_at")
    if isinstance(when, str):
        ts = when.replace("Z","")
    else:
        ts = datetime.now(timezone.utc).isoformat()
    return (
        f"⚡ SMA crossover\n"
        f"Pair: {sig['pair']}  TF: {sig['timeframe']}\n"
        f"Setup: SMA{sig['fast']}/{sig['slow']}\n"
        f"Signal: {sig['signal']} @ {fmt_price(sig['price'])}\n"
        f"Time: {ts}"
    )

def find_evm_alias_for_canonical(canonical: str) -> str | None:
    """
    Return a 0x... alias that points to this canonical TRON address,
    tolerating old rows saved without the '41' prefix.
    """
    if not sb or not canonical:
        return None
    c = normalize_tron_addr(canonical) or ""
    cand = [c]
    if c.startswith("41") and len(c) == 42:
        cand.append(c[2:])  # also try bare 20-byte
    try:
        resp = (sb.table("token_aliases")
                  .select("alias")
                  .in_("canonical_address", cand)
                  .limit(50)
                  .execute())
        rows = getattr(resp, "data", None) or []
    except Exception:
        rows = []
    for r in rows:
        a = (r.get("alias") or "").strip()
        if a.lower().startswith("0x") and len(a) == 42:
            return a
    return None


def looks_synthetic_hex41(addr: str) -> bool:
    """
    Heuristic: your synthetic 41.. addresses start with 4199...
    """
    if not addr: return False
    h = addr.lower().removeprefix("0x")
    return h.startswith("4199") and len(h) == 42


# ---------- dispatcher & handlers ----------
dp = Dispatcher()

# ──────────────────────────────────────────────────────────────────────────────
# UI LAYER — PART 1 (Home, Buy/Sell quick flows, Positions+Refresh, Signals+AI hubs)
# Drop this right after:  dp = Dispatcher()
# ──────────────────────────────────────────────────────────────────────────────
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import html
from decimal import Decimal
import math
import asyncio
import re

# ------- small utils -------
def _mk(builder_rows):
    b = InlineKeyboardBuilder()
    for row in builder_rows:
        for (text, data) in row:
            b.button(text=text, callback_data=data)
    # Force 2 per row (tweak 2→3 if you want 3 per row)
    b.adjust(2)
    return b.as_markup()


def _money(x):
    try:
        f = float(Decimal(str(x)))
        return f"${f:,.2f}"
    except Exception:
        return str(x)

def _pct(p):
    try:
        f = float(Decimal(str(p)))
        sign = "" if f < 0 else "+"
        return f"{sign}{f:.2f}%"
    except Exception:
        return str(p)

# ========= UI: HOME =========
HOME_KB = _mk([
    [("🟢 Buy", "ui:buy"), ("🔴 Sell", "ui:sell")],
    [("📊 Positions", "ui:positions"), ("📡 Signals", "ui:signals")],
    [("🧠 AI", "ui:ai"), ("🧰 Rebuild", "ui:rebuild")],
    [("⚙️ Settings", "ui:settings"), ("❓ Help", "ui:help")],
])

@dp.message(Command(commands=["start"]))
async def ui_start(m: types.Message):
    await m.answer(
        "Welcome to **TRON Algo AI Bot**\n\n"
        "Tap a section below to get started.",
        reply_markup=HOME_KB, parse_mode="Markdown"
    )

@dp.callback_query(F.data == "ui:home")
async def ui_home(c: CallbackQuery):
    await c.message.edit_text(
        "Home — pick an action:",
        reply_markup=HOME_KB
    )
    await c.answer()

# ========= UI: BUY =========
BUY_KB = _mk([
    [("💵 $100", "buy:$100"), ("💵 $200", "buy:$200")],
    [("💵 $500", "buy:$500"), ("💵 $1000", "buy:$1000")],
    [("🔢 Token amount", "buy:units"), ("💲 $ amount", "buy:usd")],
    [("⬅️ Back", "ui:home")]
])

@dp.callback_query(F.data == "ui:buy")
async def ui_buy(c: CallbackQuery):
    await c.message.edit_text(
        "Buy — choose a preset or input mode.\n\n"
        "• Presets: I’ll ask which token next.\n"
        "• Token amount: send like `TRX 123.45`\n"
        "• $ amount: send like `TRX $200`",
        reply_markup=BUY_KB, parse_mode="Markdown"
    )
    await c.answer()

# ========= UI: SELL =========
SELL_KB = _mk([
    [("💵 $100", "sell:$100"), ("💵 $200", "sell:$200")],
    [("💵 $500", "sell:$500"), ("💵 $1000", "sell:$1000")],
    [("🔢 Token amount", "sell:units"), ("💲 $ amount", "sell:usd")],
    [("📉 % of position", "sell:pct")],
    [("⬅️ Back", "ui:home")]
])

@dp.callback_query(F.data == "ui:sell")
async def ui_sell(c: CallbackQuery):
    await c.message.edit_text(
        "Sell — choose a preset or input mode.\n\n"
        "• Presets: I’ll ask which token next.\n"
        "• Token amount: `TRX 10`\n"
        "• $ amount: `TRX $500`\n"
        "• Percent: `TRX 50%`",
        reply_markup=SELL_KB, parse_mode="Markdown"
    )
    await c.answer()

# ========= State (lightweight, no FSM) =========
_UI_STATE = {}  # chat_id -> dict

def _set_state(chat_id: int, **kw):
    _UI_STATE[chat_id] = {"t": asyncio.get_event_loop().time(), **kw}

def _pop_state(chat_id: int):
    return _UI_STATE.pop(chat_id, None)

def _has_state(chat_id: int, kind: str):
    s = _UI_STATE.get(chat_id)
    return s and s.get("kind") == kind

# ====== Quick Buy/Sell presets: ask token next, then dispatch to /buy or /sell ======
_PRESET_RE = re.compile(r"^(buy|sell):\$(\d+)$")

@dp.callback_query(F.data.regexp(_PRESET_RE))
async def ui_preset_amount(c: CallbackQuery):
    m = _PRESET_RE.match(c.data)
    mode, usd = m.group(1), int(m.group(2))
    _set_state(c.message.chat.id, kind=f"{mode}_preset", usd=usd)
    await c.message.edit_text(
        f"{'Buy' if mode=='buy' else 'Sell'} preset selected: ${usd}\n"
        "Now send the token (symbol or address), e.g. `TRX` or `41...`.",
        reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        parse_mode="Markdown"
    )
    await c.answer()

@dp.callback_query(F.data == "buy:units")
async def ui_buy_units(c: CallbackQuery):
    _set_state(c.message.chat.id, kind="buy_units")
    await c.message.edit_text(
        "Send **token and units** to buy, e.g. `TRX 123.45`",
        reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        parse_mode="Markdown"
    ); await c.answer()

@dp.callback_query(F.data == "buy:usd")
async def ui_buy_usd(c: CallbackQuery):
    _set_state(c.message.chat.id, kind="buy_usd")
    await c.message.edit_text(
        "Send **token and $ amount**, e.g. `TRX $200`",
        reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        parse_mode="Markdown"
    ); await c.answer()

@dp.callback_query(F.data == "sell:units")
async def ui_sell_units(c: CallbackQuery):
    _set_state(c.message.chat.id, kind="sell_units")
    await c.message.edit_text(
        "Send **token and units** to sell, e.g. `TRX 10`",
        reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        parse_mode="Markdown"
    ); await c.answer()

@dp.callback_query(F.data == "sell:usd")
async def ui_sell_usd(c: CallbackQuery):
    _set_state(c.message.chat.id, kind="sell_usd")
    await c.message.edit_text(
        "Send **token and $ amount**, e.g. `TRX $500`",
        reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        parse_mode="Markdown"
    ); await c.answer()

@dp.callback_query(F.data == "sell:pct")
async def ui_sell_pct(c: CallbackQuery):
    _set_state(c.message.chat.id, kind="sell_pct")
    await c.message.edit_text(
        "Send **token and percent**, e.g. `TRX 50%` or `TRX 50`",
        reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        parse_mode="Markdown"
    ); await c.answer()


# after
@dp.message(F.text & ~F.text.startswith("/"))
async def ui_free_text_router(m: types.Message):
    ...

    # Let real commands be handled by their own handlers
    if (m.text or "").startswith("/"):
        return

    st = _UI_STATE.get(m.chat.id)

        # --- Signals wizard states ---
    wiz = _wiz_get(m.chat.id)
    if wiz:
        step = wiz["step"]
        txt  = (m.text or "").strip()

        # Step 1: ticker / address
        if step == "ticker":
            token_like = txt
            data = {"token_like": token_like}
            # If it's an address, try to guess network for later
            net = None
            if is_token_address(token_like):
                try:
                    net = guess_network_for_address(token_like)
                except Exception:
                    net = None
                data["network"] = net
            _wiz_set(m.chat.id, "strategy", **data)
            kb = _mk([
                [("SMA", "wiz:strategy:sma"), ("RSI", "wiz:strategy:rsi")],
                [("Blank", "wiz:strategy:blank")],
                [("❌ Cancel", "ui:cancel")]
            ])
            await m.reply("Step 2/5 — Choose strategy:", reply_markup=kb)
            return

        # Step 3: fast/slow (we accept "10 30" or just "10,30")
        if step == "fastslow":
            numbers = re.findall(r"\d+", txt)
            if len(numbers) < 2:
                await m.reply("Send two integers like `10 30`.")
                return
            a, b = int(numbers[0]), int(numbers[1])
            fast, slow = (a, b) if a <= b else (b, a)
            data = {**wiz["data"], "fast": fast, "slow": slow}
            _wiz_set(m.chat.id, "timeframe", **data)
            # timeframes as buttons
            b = InlineKeyboardBuilder()
            for tf in TF_CHOICES:
                b.button(text=tf, callback_data=f"wiz:tf:{tf}")
            b.button(text="❌ Cancel", callback_data="ui:cancel")
            b.adjust(4, 4, 4)
            await m.reply("Step 4/5 — Pick timeframe:", reply_markup=b.as_markup())

            return
        
                # Step 3 (RSI): period only
        if step == "rsi_period":
            nums = re.findall(r"\d+", txt)
            if not nums:
                await m.reply("Send one integer period, e.g. `14`"); return
            period = int(nums[0])
            data = {**wiz["data"], "fast": period, "slow": 0}  # store period in fast, slow=0
            _wiz_set(m.chat.id, "timeframe", **data)
            # timeframes as buttons
            b = InlineKeyboardBuilder()
            for tf in TF_CHOICES:
                b.button(text=tf, callback_data=f"wiz:tf:{tf}")
            b.button(text="❌ Cancel", callback_data="ui:cancel")
            b.adjust(4, 4, 4)
            await m.reply("Step 4/5 — Pick timeframe:", reply_markup=b.as_markup())
            return

            

        # Step 5 (optional): network free-text (if still missing)
        if step == "network":
            net = txt.lower()
            if net not in NET_CHOICES:
                await m.reply(f"Unsupported network. Try one of: {', '.join(NET_CHOICES)}")
                return
            data = {**wiz["data"], "network": net}
            await _wiz_confirm_and_save(m, data)
            return


    if not st:
        return
    kind = st.get("kind")

    # --- Buy/Sell preset: user just sends the token symbol/address ---
    if kind in ("buy_preset", "sell_preset"):
        raw = (m.text or "").strip()
        if not raw:
            await m.reply("Please send a token symbol or address.")
            return
        usd = Decimal(st["usd"])
        if kind == "buy_preset":
            cmd = f"/buy {raw} ${usd}"
            m2 = m.model_copy(update={"text": cmd})
            await handle_buy(m2)
        else:
            cmd = f"/sell {raw} ${usd}"
            m2 = m.model_copy(update={"text": cmd})
            await handle_sell(m2)
        _pop_state(m.chat.id)
        return

    # --- Buy units: "TRX 123.45" ---
    elif kind == "buy_units":
        parts = (m.text or "").split()
        if len(parts) != 2:
            await m.reply("Format: `SYMBOL UNITS` e.g. `TRX 123.45`", parse_mode="Markdown")
            return
        sym, units = parts
        cmd = f"/buy {sym} {units}"
        await handle_buy(m.model_copy(update={"text": cmd}))
        _pop_state(m.chat.id)
        return

    # --- Buy USD: "TRX $200" ---
    elif kind == "buy_usd":
        compact = (m.text or "").replace(" ", "").upper()
        mt = re.match(r"^([A-Z0-9/_.:-]+)\$?([\d.]+)$", compact)
        if not mt:
            await m.reply("Format: `SYMBOL $AMOUNT` e.g. `TRX $200`")
            return
        sym, usd = mt.group(1), mt.group(2)
        cmd = f"/buy {sym} ${usd}"
        await handle_buy(m.model_copy(update={"text": cmd}))
        _pop_state(m.chat.id)
        return

    # --- Sell units: "TRX 10" ---
    elif kind == "sell_units":
        parts = (m.text or "").split()
        if len(parts) != 2:
            await m.reply("Format: `SYMBOL UNITS` e.g. `TRX 10`", parse_mode="Markdown")
            return
        sym, units = parts
        cmd = f"/sell {sym} {units}"
        await handle_sell(m.model_copy(update={"text": cmd}))
        _pop_state(m.chat.id)
        return

    # --- Sell USD: "TRX $500" ---
    elif kind == "sell_usd":
        compact = (m.text or "").replace(" ", "").upper()
        mt = re.match(r"^([A-Z0-9/_.:-]+)\$?([\d.]+)$", compact)
        if not mt:
            await m.reply("Format: `SYMBOL $AMOUNT` e.g. `TRX $500`")
            return
        sym, usd = mt.group(1), mt.group(2)
        cmd = f"/sell {sym} ${usd}"
        await handle_sell(m.model_copy(update={"text": cmd}))
        _pop_state(m.chat.id)
        return

    # --- Sell percent: accepts `SYMBOL 50` or `SYMBOL 50%` with flexible spaces ---
    elif kind == "sell_pct":
        txt = (m.text or "")
        mobj = re.match(r"^\s*([A-Za-z0-9/_.:-]+)\s+(\d+(?:\.\d+)?)\s*%?\s*$", txt)
        if not mobj:
            await m.reply("Format: `SYMBOL PERCENT%` e.g. `TRX 50%`", parse_mode="Markdown")
            return
        sym = mobj.group(1)
        pct = Decimal(mobj.group(2))
        if pct <= 0 or pct > 100:
            await m.reply("Percent must be between 0 and 100.")
            return
        cmd = f"/sell {sym} {pct}%"
        await handle_sell(m.model_copy(update={"text": cmd}))
        _pop_state(m.chat.id)
        return


# -------- Part 2: Signals wizard state --------
_WIZ = {}  # chat_id -> {"step": str, "data": dict, "ts": float}

def _wiz_set(chat_id: int, step: str, **data):
    _WIZ[chat_id] = {"step": step, "data": {**data}, "ts": asyncio.get_event_loop().time()}

def _wiz_get(chat_id: int):
    return _WIZ.get(chat_id)

def _wiz_pop(chat_id: int):
    return _WIZ.pop(chat_id, None)

TF_CHOICES = ["1m","5m","10m","15m","30m","1h","3h","4h","6h","12h","1d","3d"]
STRAT_CHOICES = ["sma","rsi","blank"]  # we persist 'sma' now; rsi/blank reserved
NET_CHOICES = ["eth","bsc","polygon","arbitrum","optimism","base","avax","fantom","tron","solana"]



# ========= UI: POSITIONS (styled card + refresh) =========
def _positions_kb():
    return _mk([
        [("🔄 Refresh", "pos:refresh"), ("⬅️ Back", "ui:home")]
    ])

async def _load_positions():
    try:
        resp = (sb.table("open_trades")
                  .select("token_symbol, token_address, amount, avg_entry_price")
                  .limit(20).execute())
        rows = getattr(resp, "data", None) or []
        # join with latest prices if available
        px = (sb.table("prices_latest")
                .select("token_address, last_price")
                .in_("token_address", [r["token_address"] for r in rows] or [""])
                .execute()).data or []
        px_map = {p["token_address"]: Decimal(str(p["last_price"])) for p in px}
        for r in rows:
            r["_last_price"] = px_map.get(r["token_address"])
        return rows
    except Exception:
        return []

def _fmt_position_row(r):
    sym = (r.get("token_symbol") or "UNKNOWN").upper()
    addr = r.get("token_address") or ""
    amt  = Decimal(str(r.get("amount") or 0))
    avg  = Decimal(str(r.get("avg_entry_price") or 0))
    last = r.get("_last_price")
    line1 = f"{sym} — {addr}"
    if last and last > 0:
        value = last * amt
        pnl_pct = (last - avg) / avg * 100 if avg and avg > 0 else Decimal(0)
        pnl_val = (last - avg) * amt
        pnl_emoji = "🟩" if pnl_pct >= 0 else "🟥"
        details = (
            f"• 💰 Price: {_money(last)}\n"
            f"• 📊 Avg Entry: {_money(avg)}\n"
            f"• 🔢 Balance: {amt}\n"
            f"• 💵 Balance Value: {_money(value)}\n"
            f"• 📈 PnL Value: {_money(pnl_val)}\n"
            f"• 📉 PnL: {_pct(pnl_pct)} {pnl_emoji}"
        )
    else:
        details = (
            f"• 📊 Avg Entry: {_money(avg)}\n"
            f"• 🔢 Balance: {amt}\n"
            f"(No live price yet — tap Refresh)"
        )
    return line1 + "\n" + details

@dp.callback_query(F.data.startswith("wiz:strategy:"))
async def wiz_pick_strategy(c: CallbackQuery):
    strat = c.data.split(":")[-1].lower()  # "sma" | "rsi" | "blank"
    data = (_wiz_get(c.message.chat.id) or {}).get("data", {})

    # Ensure your choices list is lowercase: STRAT_CHOICES = ["sma","rsi","blank"]
    if strat not in STRAT_CHOICES:
        await c.answer("Unknown strategy"); 
        return

    # Remember chosen strategy for this chat (SMA/RSI/MANUAL)
    set_strategy_for_chat(c.message.chat.id, strat.upper())

    if strat == "rsi":
        # RSI needs a single period (e.g., 14)
        _wiz_set(c.message.chat.id, "rsi_period", **{**data, "strategy": "rsi"})
        await c.message.edit_text(
            "Step 3/5 — Send **period** (integer), e.g. `14`",
            reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
            parse_mode="Markdown",
        )
    elif strat == "sma":
        # SMA needs fast/slow (e.g., 10 30)
        _wiz_set(c.message.chat.id, "fastslow", **{**data, "strategy": "sma"})
        await c.message.edit_text(
            "Step 3/5 — Send `fast slow` integers.\nExample: `10 30`",
            reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        )
    else:
        # Placeholder for future strategy "blank"
        _wiz_set(c.message.chat.id, "fastslow", **{**data, "strategy": "blank"})
        await c.message.edit_text(
            "Step 3/5 — Send parameters (TBD). For now, use SMA or RSI.",
            reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        )

    await c.answer()



@dp.callback_query(F.data.startswith("wiz:tf:"))
async def wiz_pick_timeframe(c: CallbackQuery):
    tf = c.data.split(":")[-1]
    if tf not in TF_CHOICES:
        await c.answer("Bad timeframe"); return
    wiz = _wiz_get(c.message.chat.id)
    if not wiz:
        await c.answer("Session expired"); return
    data = {**wiz["data"], "timeframe": tf}

    # If token_like is address and network missing, ask for it
    if is_token_address(data["token_like"]) and not data.get("network"):
        _wiz_set(c.message.chat.id, "network", **data)
        b = InlineKeyboardBuilder()
        for n in NET_CHOICES:
            b.button(text=n, callback_data=f"wiz:net:{n}")
        b.button(text="❌ Cancel", callback_data="ui:cancel")
        b.adjust(4, 4, 4)
        await c.message.edit_text("Step 5/5 — Pick network (or type one):", reply_markup=b.as_markup())
        await c.answer(); return

    # else save now
    await _wiz_confirm_and_save(c.message, data)
    await c.answer()

@dp.callback_query(F.data.startswith("wiz:net:"))
async def wiz_pick_network(c: CallbackQuery):
    net = c.data.split(":")[-1]
    wiz = _wiz_get(c.message.chat.id)
    if not wiz:
        await c.answer("Session expired"); return
    data = {**wiz["data"], "network": net}
    await _wiz_confirm_and_save(c.message, data)
    await c.answer()

async def _wiz_confirm_and_save(msg: types.Message, data: dict):
    token_like = data["token_like"].strip()
    strat = (data.get("strategy") or "sma").lower()
    fast, slow = int(data.get("fast") or 0), int(data.get("slow") or 0)
    tf = data["timeframe"]
    chat_id = str(msg.chat.id)

    token_symbol = None
    ds_address   = None
    network      = data.get("network")

    if is_token_address(token_like):
        ds_address = token_like
        try:
            _, scraped, _ = fetch_onchain_price_and_meta(ds_address)
            token_symbol = (scraped or "UNKNOWN").upper()
        except Exception:
            token_symbol = "UNKNOWN"
        if not network:
            await msg.reply("Please provide a network slug (e.g. eth, bsc, tron, base).")
            _wiz_set(msg.chat.id, "network", **data)
            return
        on_conf = "ds_address,network,fast,slow,timeframe,tg_chat_id"
    else:
        token_symbol = token_like.upper()
        on_conf = "token_symbol,fast,slow,timeframe,tg_chat_id"

    # Normalize fields per strategy
    if strat == "rsi":
        # fast=<period>, slow=0
        slow = 0
    else:
        strat = "sma"  # default for unknown

    row = {
        "token_symbol": token_symbol,
        "ds_address": ds_address,
        "network": network,
        "fast": fast, "slow": slow,
        "timeframe": tf,
        "is_enabled": True,
        "tg_chat_id": chat_id,
        "strategy": strat,
    }
    try:
        # If your table lacks 'strategy', fall back without it
        sb.table("signal_subscriptions").upsert(row, on_conflict=on_conf).execute()
    except Exception:
        row.pop("strategy", None)
        sb.table("signal_subscriptions").upsert(row, on_conflict=on_conf).execute()

    _wiz_pop(msg.chat.id)
    label = ds_address or token_symbol
    tail  = f" {network}" if ds_address else ""
    if strat == "rsi":
        await msg.reply(f"✅ Subscribed: {label}{tail} RSI{fast} {tf}")
    else:
        await msg.reply(f"✅ Subscribed: {label}{tail} SMA{fast}/{slow} {tf}")



@dp.callback_query(F.data == "ui:positions")
async def ui_positions(c: CallbackQuery):
    rows = await _load_positions()
    if not rows:
        await c.message.edit_text("No open positions.", reply_markup=_mk([[("⬅️ Back", "ui:home")]]))
        await c.answer(); return
    card = "Open Positions:\n\n" + "\n\n".join(_fmt_position_row(r) for r in rows)
    await c.message.edit_text(card, reply_markup=_positions_kb())
    await c.answer()

@dp.callback_query(F.data == "pos:refresh")
async def ui_positions_refresh(c: CallbackQuery):
    # trigger your refresher script (reuses existing /refresh_prices path) 
    try:
        m2 = c.message.model_copy(update={"text": "/refresh_prices"})
        await refresh_prices_cmd(m2)

    except Exception:
        pass
    # Re-render
    rows = await _load_positions()
    if not rows:
        await c.message.edit_text("No open positions.", reply_markup=_mk([[("⬅️ Back", "ui:home")]]))
    else:
        card = "Open Positions:\n\n" + "\n\n".join(_fmt_position_row(r) for r in rows)
        await c.message.edit_text(card, reply_markup=_positions_kb())
    await c.answer("Prices refreshed")

# ========= UI: SIGNALS HUB (Part 1 shell) =========
SIGNALS_KB = _mk([
    [("🧾 Subscriptions", "sig:subs"), ("➕ Make signal", "sig:make")],
    [("🔔 Alerts (today)", "sig:alerts")],
    [("⬅️ Back", "ui:home")]
])

@dp.callback_query(F.data == "ui:cancel")
async def ui_cancel(c: CallbackQuery):
    _pop_state(c.message.chat.id)
    _wiz_pop(c.message.chat.id)
    await c.message.edit_text("Cancelled. Back to Home.", reply_markup=HOME_KB)
    await c.answer()


@dp.callback_query(F.data == "ui:signals")
async def ui_signals(c: CallbackQuery):
    await c.message.edit_text(
        "Signals — choose an option.",
        reply_markup=SIGNALS_KB
    ); await c.answer()

@dp.callback_query(F.data == "sig:subs")
async def ui_sig_subs(c: CallbackQuery):
    # Always fetch id so delete buttons work
    try:
        resp = (
            sb.table("signal_subscriptions")
              .select("id, token_symbol, ds_address, fast, slow, timeframe, network, strategy")
              .eq("is_enabled", True)
              .eq("tg_chat_id", str(c.message.chat.id))
              .order("token_symbol", desc=False)
              .limit(100)
              .execute()
        )
        rows = getattr(resp, "data", None) or []
    except Exception:
        rows = []

    if not rows:
        txt = "No active subscriptions."
        kb = _mk([[("⬅️ Back", "ui:signals")]])
    else:
        lines = []
        b = InlineKeyboardBuilder()
        for r in rows:
            label = r.get("ds_address") or (r.get("token_symbol") or "").upper()
            net   = f" {r.get('network')}" if r.get("network") else ""
            # If strategy missing, infer: slow!=0 -> SMA, else RSI
            strat = (r.get("strategy") or ("sma" if (r.get("slow") or 0) != 0 else "rsi")).lower()
            if strat == "rsi":
                lines.append(f"• {label}{net} — RSI{r['fast']} {r['timeframe']}")
            else:
                lines.append(f"• {label}{net} — SMA{r['fast']}/{r['slow']} {r['timeframe']}")
            rid = r.get("id")
            if rid is not None:
                b.button(text=f"🗑️ {label}", callback_data=f"sig:rm:{rid}")
        b.button(text="⬅️ Back", callback_data="ui:signals")
        b.adjust(1)
        kb = b.as_markup()
        txt = "Subscriptions:\n" + "\n".join(lines)

    await c.message.edit_text(txt, reply_markup=kb)
    await c.answer()


@dp.callback_query(F.data.startswith("sig:rm:"))
async def ui_sig_rm(c: CallbackQuery):
    sub_id = c.data.split(":")[-1]
    try:
        sb.table("signal_subscriptions").update({"is_enabled": False}).eq("id", int(sub_id)).execute()
        await c.answer("Removed.", show_alert=False)
    except Exception as e:
        await c.answer(f"Remove failed: {e}", show_alert=True)
    # Refresh list
    await ui_sig_subs(c)

@dp.callback_query(F.data == "sig:make")
async def ui_sig_make(c: CallbackQuery):
    _wiz_set(c.message.chat.id, "ticker")
    await c.message.edit_text(
        "Make Signal — Step 1/5\n\n"
        "Send token **symbol or on‑chain address**.\n"
        "Examples:\n"
        "• `ADA`\n"
        "• `0x...` (EVM)\n"
        "• `T...` or `41...` (TRON)\n",
        reply_markup=_mk([[("❌ Cancel", "ui:cancel")]]),
        parse_mode="Markdown"
    )
    await c.answer()


def _today_bounds_utc():
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999000)
    return start.isoformat(), end.isoformat()

@dp.callback_query(F.data == "sig:alerts")
async def ui_sig_alerts(c: CallbackQuery):
    start_iso, end_iso = _today_bounds_utc()
    try:
        res = (sb.table("signal_alerts")
                 .select("ds_address, token_address, token_symbol, fast, slow, timeframe, signal, price, crossed_at, sent_at")
                 .eq("tg_chat_id", str(c.message.chat.id))
                 .gte("sent_at", start_iso)
                 .lte("sent_at", end_iso)
                 .order("sent_at", desc=True)
                 .limit(50)
                 .execute())
        rows = getattr(res, "data", None) or []
    except Exception:
        rows = []
    if not rows:
        txt = "No alerts today."
    else:
        lines = []
        for r in rows:
            label = r.get("ds_address") or r.get("token_address") or (r.get("token_symbol") or "").upper()
            lines.append(f"• {r['signal']} — {label} SMA{r['fast']}/{r['slow']} {r['timeframe']} @ {r['price']} ({r['sent_at']})")
        txt = "Alerts (today):\n" + "\n".join(lines[:50])

    kb = _mk([[("🔄 Refresh", "sig:alerts"), ("⬅️ Back", "ui:signals")]])
    await c.message.edit_text(txt, reply_markup=kb)
    await c.answer()


# ========= UI: AI HUB =========
AI_KB = _mk([
    [("💬 Ask (SQL agent)", "ai:ask"), ("🔎 Search (web)", "ai:search")],
    [("⬅️ Back", "ui:home")]
])

@dp.callback_query(F.data == "ui:ai")
async def ui_ai(c: CallbackQuery):
    await c.message.edit_text(
        "AI — choose:\n"
        "• Ask: query PnL/positions via SQL agent.\n"
        "• Search: web research summaries.",
        reply_markup=AI_KB
    ); await c.answer()

@dp.callback_query(F.data == "ai:ask")
async def ui_ai_ask(c: CallbackQuery):
    await c.message.edit_text(
        "Send as: `/ask <question>`\n"
        "Examples:\n"
        "• `/ask Last 5 trades`\n"
        "• `/ask open positions`",
        reply_markup=_mk([[("⬅️ Back", "ui:ai")]]),
        parse_mode="Markdown"
    ); await c.answer()

@dp.callback_query(F.data == "ai:search")
async def ui_ai_search(c: CallbackQuery):
    await c.message.edit_text(
        "Send as: `/search <query>`\n"
        "Examples:\n"
        "• `/search price TRX`\n"
        "• `/search what is rsi`\n"
        "• `/search strategies for sma/rsi`",
        reply_markup=_mk([[("⬅️ Back", "ui:ai")]]),
        parse_mode="Markdown"
    ); await c.answer()

# ========= UI: REBUILD =========
@dp.callback_query(F.data == "ui:rebuild")
async def ui_rebuild(c: CallbackQuery):
    await c.message.edit_text(
        "This will wipe cached tables and rebuild from on‑chain events.\n\n"
        "Tap to proceed:",
        reply_markup=_mk([
            [("⚠️ Confirm Rebuild", "rebuild:go")],
            [("⬅️ Back", "ui:home")]
        ])
    ); await c.answer()

@dp.callback_query(F.data == "rebuild:go")
async def ui_rebuild_go(c: CallbackQuery):
    # Prime the confirmation set for this chat, then reuse the confirm path
    PENDING_REBUILD.add(str(c.message.chat.id))
    m2 = c.message.model_copy(update={"text": "/rebuild_confirm"})
    await rebuild_confirm_cmd(m2)
    await c.answer()


# ========= UI: HELP / SETTINGS =========
@dp.callback_query(F.data == "ui:help")
async def ui_help(c: CallbackQuery):
    await c.message.edit_text(
        "Help:\n"
        "• Use the home buttons for quick actions.\n"
        "• Or try commands:\n"
        "  /buy  /sell  /positions  /refresh_prices  /cs  /ls  /rm  /ask  /search",
        reply_markup=_mk([[("⬅️ Back", "ui:home")]])
    ); await c.answer()

@dp.callback_query(F.data == "ui:settings")
async def ui_settings(c: CallbackQuery):
    await c.message.edit_text(
        "Settings (coming soon).",
        reply_markup=_mk([[("⬅️ Back", "ui:home")]])
    ); await c.answer()


@dp.callback_query(F.data.startswith("deep:/") | F.data.startswith("deep:/buy") | F.data.startswith("deep:/sell") | F.data.startswith("deep:/"))
async def deep_router(c: CallbackQuery):
    # Example callback_data: "deep:/buy TRX $100"
    cmd = c.data[len("deep:"):]
    await c.answer()
    # Create a faux message for handlers that expect Message.text
    m2 = c.message.model_copy(update={"text": cmd})
    if cmd.startswith("/buy"):
        await handle_buy(m2)
    elif cmd.startswith("/sell"):
        await handle_sell(m2)
    else:
        # Fallback: just echo command text so we see it
        await c.message.answer(cmd)



@dp.message(Command(commands=["start", "help"]))
async def show_help(m: types.Message):
    await m.reply(
        "Hi! I can emit trades on TRON (Nile).\n\n"
        "Commands:\n"
        "  /buy  TUSDT 0.001 @ 0.123456\n"
        "  /sell TUSDT 0.001 @ 0.130000\n\n"
        "I’ll confirm before sending the on-chain event."
    )

@dp.message(Command("ping"))
async def ping(m: types.Message):
    await m.reply("pong")

_BUY_USAGE = (
    "Usage:\n"
    "• /buy TRX 100            (buy 100 tokens @ market)\n"
    "• /buy TRX $100           (spend $100 @ market)\n"
    "• /buy TRX 100 @ 0.125    (manual price)\n"
    "• /buy TRX $100 @ market  (explicit market)\n"
)

@dp.message(Command("buy"))
async def handle_buy(m: Message):
    try:
        parts = m.text.strip().split()
        if len(parts) < 3:
            await m.reply(_BUY_USAGE); return

        # Parse basic pieces
        # patterns supported:
        # /buy SYMBOL AMOUNT
        # /buy SYMBOL $DOLLARS
        # /buy SYMBOL AMOUNT @ PRICE|market
        # /buy SYMBOL $DOLLARS @ PRICE|market


        # --- parse size (either token UNITS or $DOLLARS) ---
        # Supported:
        #   /buy SYMBOL 100
        #   /buy SYMBOL $100
        chunk = parts[2].replace(",", "")
        try:
            if chunk.startswith("$"):
                spend_usd = Decimal(chunk[1:])
                if spend_usd <= 0:
                    await m.reply("Dollar amount must be > 0"); return
                amt_token = None
            else:
                amt_token = Decimal(chunk)
                if amt_token <= 0:
                    await m.reply("Token amount must be > 0"); return
                spend_usd = None
        except Exception:
            await m.reply("Bad amount. Use a number like 100 or $100"); return
        # --- end size parser --


        # Resolve address vs ticker and pick the price source
        raw = parts[1].strip()

        if is_token_address(raw):
            # On-chain path: Dexscreener → Ave.ai fallback
            px, scraped_symbol, norm_addr = fetch_onchain_price_and_meta(raw)
            symbol = scraped_symbol           # e.g., 'USDT'
            addr   = norm_addr                # normalized token address (41.. or base58)

            # --- INSERT SYNTHETIC FALLBACK RIGHT HERE ---
            # If user passed a non-TRON address (0x...) and you want to still emit on TRON,
            # synthesize a 41... address for logging when SYNTHETIC_FOR_NONTRON=1
            non_tron = not (raw.startswith("T") or raw[:2].lower() == "41")
            if non_tron:
                addr = make_synth_hex41(symbol) 
                # store original `raw` somewhere if you want
            # --- END INSERT ---

        else:
            # Ticker path: CCXT
            symbol = raw
            if not token_address_for_symbol(symbol):
                await m.reply(f"Unknown symbol '{symbol}'. Add it to TOKEN_SYMBOLS_MAP in .env.")
                return
            addr = token_address_for_symbol(symbol)
            px   = await get_market_price(symbol)

        # Save aliases so future /sell works by anything the user typed
              # exactly what user typed (e.g., 0x..., T..., 41...)


        # Optional @ price override (keep whatever px we already chose unless user overrides)
        if "@" in parts:
            at_idx = parts.index("@")
            if at_idx + 1 >= len(parts):
                await m.reply(_BUY_USAGE); return
            price_arg = parts[at_idx + 1].lower()
            if price_arg in ("mkt", "market"):
                pass  # keep px from above
            else:
                px = Decimal(price_arg)
        else:
            pass  # keep px from above

        # Do NOT overwrite addr later; now get decimals using the resolved addr
        addr = normalize_tron_addr(addr)
        save_alias(symbol.upper(), addr)           # ticker → canonical
        save_alias(addr, addr)                     # canonical → canonical
        save_alias(parts[1].strip(), addr)  
        try:
            if addr and addr.startswith("41") and len(addr) == 42:
                # auto-migrate old alias rows pointing to bare 20-byte
                sb.table("token_aliases") \
                .update({"canonical_address": addr}) \
                .eq("canonical_address", addr[2:]) \
                .execute()
                # ensure canonical→canonical exists
                save_alias(addr, addr)
        except Exception:
            pass


        decs = token_decimals_for_address(addr)
        if not (addr.startswith("T") or (addr.startswith("41") and len(addr) == 42)):
            await m.reply("Bad token address (not TRON base58 or 41.. hex)."); return


        # derive token units from either $ or units
        if spend_usd is not None:
            if px <= 0:
                await m.reply("Market price <= 0?"); return
            units = quant_amount(spend_usd / px, decs)
        else:
            units = quant_amount(amt_token, decs)

        if units <= 0:
            await m.reply("Amount rounds to zero; increase size."); return

        # scale to ints for emitter
        entry_price_int = scale_price(px)
        amount_int      = scale_amount(units, decs)

        pretty = (
            f"Confirm BUY\n"
            f"• Symbol: {symbol}\n"
            f"• Price:  {px} ({entry_price_int})\n"
            f"• Amount: {units} ({amount_int})\n"
        )
        await m.reply(pretty)

        # call emitter (blocking in thread so we don't freeze the loop)
        def _emit_open():
                strategy = strategy_for_chat(m.chat.id)
                return subprocess.run(
                    [PY, EMITTER_PATH, "open",
                    "--token-address", addr,         # real or synthetic 41…
                    "--token-symbol", symbol.upper(),# <-- NEW: pass ticker
                    "--strategy", strategy,
                    "--action", "BUY",
                    "--entry-price", str(entry_price_int),
                    "--amount", str(amount_int)],
                    check=True, capture_output=True, text=True
            )

        res = await asyncio.to_thread(_emit_open)

        # try to surface tx id
        txid = None
        for tok in (res.stdout, res.stderr):
            if tok:
                m_ = re.search(r"\b([0-9a-f]{64})\b", tok, re.I)
                if m_:
                    txid = m_.group(1); break
        await m.answer(f"✅ Submitted.\nTX: {txid or '(see logs)'}")

    except Exception as e:
        await m.reply(f"❌ BUY error: {type(e).__name__}: {e}")    

_SELL_USAGE = (
    "Usage:\n"
    "• /sell TRX 50%          (sell 50% @ market)\n"
    "• /sell TRX 10           (sell 10 tokens @ market)\n"
    "• /sell 25% TRX @ 0.14   (percent first also ok; manual price)\n"
    "• /sell TRX 100% @ market\n"
)

def parse_sell_args(words: list[str]):
    """
    Understands:
      /sell TRX 50%
      /sell 25% TRX @ 0.14
      /sell TRX 10
      /sell TRX $500
      /sell $100 TRX @ market
    Returns: (symbol:str, mode:str in {'percent','units','dollars'}, value:Decimal, manual_px:Decimal|None)
    """
    if len(words) < 3:
        raise ValueError("not enough args")

    # find optional "@ price"
    manual_px = None
    if "@" in words:
        i = words.index("@")
        if i + 1 >= len(words):
            raise ValueError("missing price after @")
        p = words[i + 1].lower()
        manual_px = None if p in ("mkt", "market") else Decimal(p)

    # body without command and '@' parts
    body = [w for w in words[1:] if w != "@" and w.lower() not in ("market", "mkt")]
    if len(body) < 2:
        raise ValueError("need symbol and amount/percent/$")

    # choose symbol + chunk (order-agnostic)
    s0, s1 = body[0], body[1]
    cand0 = s0.upper()
    cand1 = s1.upper()
    if cand0 in SYMBOLS_MAP:
        symbol, chunk = s0, s1
    elif cand1 in SYMBOLS_MAP:
        symbol, chunk = s1, s0
    else:
        # assume first is symbol; second is chunk
        symbol, chunk = s0, s1

    c = chunk.strip().upper()

    # percent?
    if c.endswith("%"):
        pct = Decimal(c[:-1])
        if pct <= 0 or pct > 100:
            raise ValueError("percent must be 0-100")
        return symbol, "percent", (pct / Decimal(100)), manual_px

    # dollars? ($100)
    if c.startswith("$"):
        usd = Decimal(c[1:])
        if usd <= 0:
            raise ValueError("dollar amount must be > 0")
        return symbol, "dollars", usd, manual_px

    # else token units
    units = Decimal(c)
    if units <= 0:
        raise ValueError("units must be > 0")
    return symbol, "units", units, manual_px


from price_sources import is_token_address, fetch_onchain_price_and_meta

@dp.message(Command("sell"))
async def handle_sell(m: Message):
    try:
        parts = m.text.strip().split()
        if len(parts) < 3:
            await m.reply(_SELL_USAGE); return

        token_like, mode, val, manual_px = parse_sell_args(parts)
        raw = token_like.strip()
        is_evm  = raw.lower().startswith("0x") and len(raw) == 42
        is_tron = (raw[0] in ("T","t")) or (raw.lower().startswith("41") and len(raw) == 42)

        # We will set these in exactly one path, then fall through to SHARED SELL LOGIC
        row = None; addr = None; symbol_for_emit = None; px = None

        # ---------------- EVM ADDRESS PATH ----------------
        if is_evm:

            # ----- EVM-required SELL path -----
            evm_addr = raw  # already validated

            # 1) Price + symbol from DEX
            try:
                px, scraped_symbol, _ = fetch_onchain_price_and_meta(evm_addr)
            except Exception:
                if manual_px is None:
                    await m.reply("Couldn’t fetch DEX price for that 0x address. Add @ <price> to your /sell."); return
                px = None  # will use manual below
                scraped_symbol = None

            # 2) Match to open_trades by symbol (must be unique)
            if not scraped_symbol:
                await m.reply("Couldn’t resolve symbol for that 0x address. Add @ <price> or sell by TRON address."); return

            symbol_for_emit = scraped_symbol.upper()
            # fetch rows by symbol
            candidates = fetch_open_rows_by_symbol(symbol_for_emit)
            if len(candidates) == 0:
                await m.reply(f"No open position for {symbol_for_emit}."); return
            if len(candidates) > 1:
                # safer to force disambiguation than guess the row
                lines = "\n".join(f"- {r['token_address']} (amount {r['amount']})" for r in candidates[:5])
                await m.reply(
                    "Multiple open positions for that symbol.\n"
                    "Sell by TRON address instead, e.g. /sell 41... 50%\n"
                    f"Candidates:\n{lines}"
                )
                return

            row  = candidates[0]
            addr = normalize_tron_addr(row["token_address"]) 

            # 3) Price selection
            if manual_px is not None:
                px = manual_px
            if px is None or px <= 0:
                await m.reply("Market price <= 0. Add @ <price> to your /sell."); return
        # ---------------- TRON ADDRESS PATH ----------------
        elif is_tron:
            addr = normalize_tron_addr(raw)
            row = fetch_open_row_by_address(addr)
            if not row:
                await m.reply("No open position for that TRON address."); return
            symbol_for_emit = row["token_symbol"]
            if manual_px is None:
                try:
                    px, _, _ = fetch_onchain_price_and_meta(addr)
                except Exception:
                    await m.reply("Couldn’t fetch market price on-chain. Add @ <price> to your /sell."); return
            else:
                px = manual_px

        # ---------------- TICKER PATH (smart confirm) ----------------
        else:

            # --- TICKER PATH: try CCXT; if it fails and no @ price, require 0x confirmation ---
            symbol = raw.upper()

            # Try alias→address or env map first
            addr = resolve_alias(symbol) or SYMBOLS_MAP.get(symbol)
            if addr:
                row = fetch_open_row_by_address(addr)

            if not row:
                # Fallback: look up open_trades by symbol
                candidates = fetch_open_rows_by_symbol(symbol)
                if len(candidates) == 1:
                    row = candidates[0]
                    addr = row["token_address"]
                    save_alias(symbol, addr)  # seed for next time
                elif len(candidates) > 1:
                    await m.reply(
                        f"Multiple open positions for {symbol}. "
                        "Sell by address (T…/41…) or use /sell 0x…"
                    )
                    return
                else:
                    await m.reply(f"No open position for {symbol}."); return

            symbol_for_emit = symbol
            addr = normalize_tron_addr(addr)

            # Pull core fields now (we may stage or sell immediately)
            open_amt  = Decimal(str(row["amount"]))
            avg_entry = Decimal(str(row["avg_entry_price"]))
            trade_id  = int(row["trade_id_onchain"])
            decs      = token_decimals_for_address(addr)

            # If user gave @ price, proceed immediately
            if manual_px is not None:
                px = manual_px
            else:
                # Try CCXT market price
                try:
                    px = await get_market_price(symbol)
                except Exception:
                    px = None

            # If CCXT failed, try on-chain price by TRON address before asking for confirm
            if not ((px is not None) and (px > 0)):
                try:
                    px_tron, _, _ = fetch_onchain_price_and_meta(addr)  # TRON base58 or 41.. works
                    if px_tron and px_tron > 0:
                        px = px_tron  # fall through to shared SELL logic and emit now
                except Exception:
                    px = None

            # If still no price → stage & ask user to confirm an address
            if not ((px is not None) and (px > 0)):
                # CCXT failed AND no @ price → stage sell and ask for 0x address
                set_pending_sell(m.chat.id, {
                    "addr": addr,                    # canonical TRON address to emit with
                    "symbol": symbol_for_emit,       # must match the 0x symbol later
                    "trade_id": trade_id,
                    "mode": mode,                    # "percent" | "dollars" | "units"
                    "val": str(val),                 # Decimal->str
                    "open_amt": str(open_amt),
                    "avg_entry": str(avg_entry),
                    "decs": int(decs),
                    "manual_px": None,               # no price yet; will price from confirmed addr
                })

                await m.reply(
                    "I can't get a market price for this ticker.\n"
                    "Please confirm the token address (0x… or T…/41…):\n"
                    f"/confirmaddr <address>\n\n"
                    f"Symbol: {symbol_for_emit}\n"
                    f"TradeId: {trade_id}\n"
                    f"(expires in {SELL_CONFIRM_TIMEOUT_S}s)"
                )
                return

        # ---------- SHARED SELL LOGIC ----------
        open_amt  = Decimal(str(row["amount"]))
        avg_entry = Decimal(str(row["avg_entry_price"]))
        trade_id  = int(row["trade_id_onchain"])
        decs      = token_decimals_for_address(addr)

        # Compute sell units from mode
        if mode == "percent":
            sell_units = quant_amount(open_amt * val, decs)
        elif mode == "dollars":
            if px is None or px <= 0:
                await m.reply("Market price <= 0. Add @ <price>."); return
            sell_units = quant_amount(val / px, decs)
        else:
            sell_units = quant_amount(val, decs)

        if sell_units > open_amt:
            sell_units = quant_amount(open_amt, decs)
        if sell_units <= 0:
            await m.reply("Sell size rounds to zero; increase percentage/amount."); return

        # PnL & scaling
        realized       = (px - avg_entry) * sell_units
        exit_price_int = scale_price(px)
        sell_amount_int= scale_amount(sell_units, decs)
        pnl_int        = int((realized * PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))

        # Preview
        await m.reply(
            "Confirm SELL (TradeClosed)\n"
            f"• Symbol: {symbol_for_emit}\n"
            f"• TradeId: {trade_id}\n"
            f"• Price:  {px} ({exit_price_int})\n"
            f"• Amount: {sell_units} ({sell_amount_int})\n"
        )

        # Emit on-chain
        def _emit_close():
            return subprocess.run(
                [PY, EMITTER_PATH, "close",
                "--trade-id", str(trade_id),
                "--token-address", addr,
                "--token-symbol", symbol_for_emit,
                "--exit-price", str(exit_price_int),
                "--pnl", str(pnl_int),
                "--sell-amount", str(sell_amount_int)],
                check=True, capture_output=True, text=True
            )

        res = await asyncio.to_thread(_emit_close)

        # Surface TX id (same as BUY)
        txid = None
        for tok in (res.stdout, res.stderr):
            if tok:
                m_ = re.search(r"\b([0-9a-f]{64})\b", tok, re.I)
                if m_:
                    txid = m_.group(1); break
        await m.answer(f"✅ Submitted.\nTX: {txid or '(see logs)'}")

    except subprocess.CalledProcessError as e:
        await m.reply(f"❌ SELL error: {e.returncode}\n{e.stderr or e.stdout}")
    except Exception as e:
        await m.reply(f"❌ SELL error: {type(e).__name__}: {e}")

@dp.message(Command("asktest"))
async def asktest_cmd(m: types.Message):
    await m.reply("asktest OK")


@dp.message(Command("ask"))
async def ask_cmd(m: types.Message):
    print("[/ask] handler entered for chat", m.chat.id, "text=", (m.text or ""))
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await m.reply("Usage: /ask <question>\nExample: /ask Which day had the highest total PnL?")
        return

    question = parts[1].strip()
    await m.reply("🧠 Thinking…")

    # keep the UI responsive: show typing every 4s until done
    stop = asyncio.Event()
    async def _typing():
        try:
            while not stop.is_set():
                await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
                await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass

    typer = asyncio.create_task(_typing())

    try:
        # import here to surface config errors as messages, not crashes
        from agent import ask_db  # uses OPENAI_API_KEY + DB_URI under the hood. :contentReference[oaicite:1]{index=1}

        def _run():
            return ask_db(question, str(m.chat.id))

        # make sure we don’t hang forever if the LLM/db is slow
        answer = await asyncio.wait_for(asyncio.to_thread(_run), timeout=45)
        if answer.strip().startswith("```"):
            await m.reply(answer[:4096], parse_mode="Markdown")
        else:
            await m.reply(answer[:4096])


    except asyncio.TimeoutError:
        await m.reply("⏳ The query is taking a while. Try a narrower question like:\n`/ask last 5 trades` or `/ask open positions`", parse_mode="Markdown")
    except Exception as e:
        await m.reply(f"❌ Query error: {type(e).__name__}: {e}")
    finally:
        stop.set()
        try:
            await typer
        except Exception:
            pass


@dp.message(Command("search"))
async def search_cmd(m: types.Message):
    qtxt = ((m.text or "").split(maxsplit=1) + [""])[1].strip()
    if not qtxt:
        await m.reply(
            "Usage:\n"
            "/search price BTC\n"
            "/search what is rsi\n"
            "/search what strategies could I implement\n"
            "/search tron chain volume\n"
            "/search <anything else>"
        )
        return

    qlow = qtxt.lower().strip()

    # 1) Price
    if qlow.startswith("price "):
        token = qtxt.split(maxsplit=1)[1].strip()
        try:
            res = await get_token_price(token)
            if res.get("ok"):
                sym, px, src = res["symbol"], res["price"], res["source"]
                await m.reply(f"Price — {sym}: ${float(px):,.6f} (source: {src})")
            else:
                await m.reply("Couldn’t fetch price. If it’s on‑chain only, send the token address (0x… or T…/41…).")
        except Exception as e:
            await m.reply(f"❌ Price error: {e}")
        return

    # 2) Indicator explainers
    if any(qlow.startswith(f"what is {t}") for t in ("rsi","sma","ema","macd")):
        term = qlow.split()[-1]
        await m.reply(explain_indicator(term)); return

    # 3) TRON volume
    if "tron" in qlow and ("volume" in qlow or "dex" in qlow):
        data = tron_dex_volume_24h()
        if data.get("ok"):
            await m.reply(f"TRON DEX 24h Volume: ${data['usd_24h']:,.0f} (source: {data['source']})")
        else:
            await m.reply("Couldn’t fetch TRON volume right now.")
        return

# inside /search handler:
    if "strategy" in qlow or "strategies" in qlow:
        try:
            ans = research_strategies(qtxt)
        except Exception as e:
            ans = research_strategies()  # fallback template
        await m.reply(ans[:4096]); return

    # general fallback:
    try:
        ans = research_general(qtxt)
    except Exception:
        ans = "Task: Answer the user’s question concisely\nI couldn’t fetch web results right now."
    await m.reply(ans[:4096])



@dp.message(Command("refresh_prices"))
async def refresh_prices_cmd(m: types.Message):
    await m.reply("⏳ Refreshing prices…")
    def _run():
        # run the same interpreter so venv/site‑packages are consistent
        return subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "price_refresher.py")],
                      check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      env={**os.environ, "PYTHONIOENCODING":"utf-8"})

    try:
        res = await asyncio.to_thread(_run)
        out = (res.stdout or "").strip()
        tail = "\n".join(out.splitlines()[-10:]) if out else "Done."
        await m.reply(f"✅ Prices refreshed.\n{tail}")
    except subprocess.CalledProcessError as e:
        await m.reply(f"❌ Refresh failed:\n{e.stderr or e.stdout or str(e)}")


@dp.message(Command("rebuild"))
async def rebuild_cmd(m: types.Message):
    """
    /rebuild -> ask for confirmation (dangerous op)
    """
    chat_id = str(m.chat.id)
    PENDING_REBUILD.add(chat_id)
    await m.reply(
        "⚠️ This will WIPE `open_trades` and `trade_history`, then backfill from on-chain events.\n\n"
        "If you're sure, run:\n/rebuild_confirm"
    )

@dp.message(Command("rebuild_confirm"))
async def rebuild_confirm_cmd(m: types.Message):
    """
    /rebuild_confirm -> purge + run tron_listener3.py once
    """
    chat_id = str(m.chat.id)
    if chat_id not in PENDING_REBUILD:
        await m.reply("Nothing to confirm. Use /rebuild first."); return

    try:
        await m.reply("⏳ Rebuilding tables from events… this may take a minute.")

        # 1) Purge tables
        # If your Supabase requires filters on DELETE, we pass a harmless wide-true predicate.
        _safe_delete_all("open_trades",    filter_col="token_address")
        _safe_delete_all("trade_history",  filter_col="event_uid")

        # 2) Run listener once
        py = _py_exe()
        listener = _listener_path()

        def _run_once():
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"   # ensure UTF-8 stdout/stderr
            return subprocess.run(
                [py, listener, "once"],
                check=True, capture_output=True, text=True, env=env
            )


        res = await asyncio.to_thread(_run_once)

        # 3) Report back the tail of stdout
        out = (res.stdout or "").strip()
        # find a friendly line (tron_listener3.py ends with a ✅ summary)
        lines = out.splitlines()[-10:] if out else []
        msg = "\n".join(lines) if lines else "Done."
        await m.reply(f"✅ Rebuild complete.\n\n{msg}")

    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e)).strip()
        await m.reply(f"❌ Rebuild failed:\n{err}")
    except APIError as e:
        await m.reply(f"❌ DB error during purge: {e}")
    except Exception as e:
        await m.reply(f"❌ Unexpected error: {type(e).__name__}: {e}")
    finally:
        PENDING_REBUILD.discard(chat_id)


@dp.message(Command("confirmaddr"))
async def confirm_addr_cmd(m: Message):
    try:
        parts = (m.text or "").split()
        if len(parts) != 2:
            await m.reply("Usage: /confirmaddr <0x… or T…/41…>"); return

        provided = parts[1].strip()
        # accept 0x, T..., or 41...
        if not is_token_address(provided):
            await m.reply("Not a valid token address. Use 0x… or T…/41…"); return

        pending = pop_pending_sell(m.chat.id)
        if not pending:
            await m.reply("No pending sell or it expired. Start again with /sell SYMBOL …"); return

        # fetch price + symbol from the provided address (works for 0x or TRON)
        try:
            px, scraped_symbol, _ = fetch_onchain_price_and_meta(provided)
        except Exception:
            await m.reply("Couldn’t fetch market price for that address. Use @ <price> in /sell."); return

        staged_symbol = pending["symbol"].upper()
        if (scraped_symbol or "").upper() != staged_symbol:
            await m.reply(
                f"Address resolves to '{scraped_symbol}', but staged symbol is '{staged_symbol}'. "
                "Start again with /sell SYMBOL …"
            ); return

        # Unpack staged fields
        addr       = normalize_tron_addr(pending["addr"])     # canonical TRON addr to emit with
        trade_id   = int(pending["trade_id"])
        mode       = pending["mode"]
        val        = Decimal(pending["val"])
        open_amt   = Decimal(pending["open_amt"])
        avg_entry  = Decimal(pending["avg_entry"])
        decs       = int(pending["decs"])

        # Units
        if mode == "percent":
            sell_units = quant_amount(open_amt * val, decs)
        elif mode == "dollars":
            sell_units = quant_amount(val / px, decs)
        else:
            sell_units = quant_amount(val, decs)

        if sell_units > open_amt:
            sell_units = quant_amount(open_amt, decs)
        if sell_units <= 0:
            await m.reply("Sell size rounds to zero; increase percentage/amount."); return

        # PnL & scaling
        realized       = (px - avg_entry) * sell_units
        exit_price_int = scale_price(px)
        sell_amount_int= scale_amount(sell_units, decs)
        pnl_int        = int((realized * PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))

        # Preview and emit
        await m.reply(
            "Confirm SELL (TradeClosed)\n"
            f"• Symbol: {staged_symbol}\n"
            f"• TradeId: {trade_id}\n"
            f"• Price:  {px} ({exit_price_int})\n"
            f"• Amount: {sell_units} ({sell_amount_int})\n"
        )

        def _emit_close():
            return subprocess.run(
                [PY, EMITTER_PATH, "close",
                 "--trade-id", str(trade_id),
                 "--token-address", addr,
                 "--token-symbol", staged_symbol,
                 "--exit-price", str(exit_price_int),
                 "--pnl", str(pnl_int),
                 "--sell-amount", str(sell_amount_int)],
                check=True, capture_output=True, text=True
            )

        res = await asyncio.to_thread(_emit_close)
        txid = None
        for tok in (res.stdout, res.stderr):
            if tok:
                m_ = re.search(r"\b([0-9a-f]{64})\b", tok, re.I)
                if m_:
                    txid = m_.group(1); break
        await m.answer(f"✅ Submitted.\nTX: {txid or '(see logs)'}")

    except subprocess.CalledProcessError as e:
        await m.reply(f"❌ SELL error: {e.returncode}\n{e.stderr or e.stdout}")
    except Exception as e:
        await m.reply(f"❌ SELL error: {type(e).__name__}: {e}")


@dp.message(Command("confirm0x"))
async def confirm0x_cmd(m: Message):
    m2 = m.model_copy(update={"text": (m.text or "").replace("/confirm0x", "/confirmaddr", 1)})
    return await confirm_addr_cmd(m2)



@dp.message(Command("positions"))
async def positions_cmd(m: Message):
    parts = (m.text or "").split()
    symbol = parts[1].upper() if len(parts) > 1 else None

    q = sb.table("open_trades").select(
        "token_symbol, token_address, trade_id_onchain, amount, avg_entry_price, strategy, trader, last_tx_id"
    )
    if symbol:
        q = q.eq("token_symbol", symbol)

    resp = q.limit(20).execute()
    rows = getattr(resp, "data", None) or []
    if not rows:
        await m.reply(f"No open position for {symbol}." if symbol else "No open positions found.")
        return

    lines = []
    for r in rows:
        lines.append(
            f"- {r.get('token_symbol')} ({r.get('token_address')})\n"
            f"  trade_id_onchain: {r.get('trade_id_onchain')}\n"
            f"  amount: {r.get('amount')}\n"
            f"  avg_entry_price: {r.get('avg_entry_price')}\n"
            f"  strategy: {r.get('strategy')}\n"
            f"  trader: {r.get('trader')}\n"
            f"  last_tx_id: {r.get('last_tx_id')}"
        )
    await m.reply("Open positions:\n\n" + "\n".join(lines))

@dp.message(Command("crsi"))
async def create_rsi_sub(m: types.Message):
    """
    /crsi <SYMBOL|ADDRESS> <period> <timeframe> [network]
    Example: /crsi TRX 14 5m
             /crsi TXL6rJbvmjD46ze... 14 1m tron
    """
    parts = (m.text or "").split()
    if len(parts) < 4:
        await m.reply("Usage: /crsi <SYMBOL|ADDRESS> <period> <timeframe> [network]"); return

    token_like = parts[1]
    period = int(parts[2])
    tf = parts[3].lower()
    network = parts[4].lower() if len(parts) >= 5 else None

    # Resolve routing fields
    ds_addr = None
    tron_addr = None
    sym = None
    if is_token_address(token_like):
        tron_addr = normalize_tron_addr(token_like)
    else:
        sym = token_like.upper()

    row = {
        "tg_chat_id": str(m.chat.id),
        "token_symbol": sym,
        "token_address": tron_addr,
        "ds_address": ds_addr,
        "fast": period,
        "slow": 0,
        "timeframe": tf,
        "is_enabled": True,
        "strategy": "rsi",
        "network": network,
    }
    try:
        sb.table("signal_subscriptions").insert(row).execute()
    except Exception:
        # if strategy col not yet present, try without it
        row.pop("strategy", None)
        sb.table("signal_subscriptions").insert(row).execute()

    await m.reply(f"✅ Subscribed: {sym or tron_addr} RSI{period} {tf}")

@dp.message(Command("rmrsi"))
async def remove_rsi_sub(m: types.Message):
    parts = (m.text or "").split()
    if len(parts) < 4:
        await m.reply("Usage: /rmrsi <SYMBOL|ADDRESS> <period> <timeframe>"); return

    token_like = parts[1]
    period = int(parts[2])
    tf = parts[3].lower()

    if is_token_address(token_like):
        addr = normalize_tron_addr(token_like)
        q = (sb.table("signal_subscriptions")
               .delete()
               .eq("token_address", addr).eq("fast", period).eq("slow", 0).eq("timeframe", tf))
    else:
        sym = token_like.upper()
        q = (sb.table("signal_subscriptions")
               .delete()
               .eq("token_symbol", sym).eq("fast", period).eq("slow", 0).eq("timeframe", tf))

    try:
        q.eq("strategy", "rsi").execute()
    except Exception:
        q.execute()  # fallback if no strategy column
    await m.reply(f"🗑️ Removed: {token_like} RSI{period} {tf}")


@dp.message(Command("cs"))
async def create_signal_cmd(m: types.Message):
    """
    /cs <TOKEN_OR_ADDR> sma <fast> <slow> <tf> [network]
    - If TOKEN_OR_ADDR is an address (0x… / T… / 41…), we save it in ds_address and
      store a GeckoTerminal network slug (either provided or auto-guessed).
    - If it's a ticker (ADA), we store token_symbol and use CCXT later.
    """
    if not sb:
        await m.reply("Supabase not configured"); return

    parts = (m.text or "").split()
    if len(parts) not in (6, 7) or parts[2].lower() != "sma":
        await m.reply("Usage: /cs <TOKEN_OR_ADDR> sma <fast> <slow> <timeframe> [network]\n"
                      "Examples:\n"
                      "  /cs ADA sma 10 30 1h\n"
                      "  /cs 0xabc... sma 10 30 1h eth"); return

    _, token_like, _, a, b, tf, *maybe_net = parts
    try:
        n1, n2 = int(a), int(b)
    except ValueError:
        await m.reply("Fast/slow must be integers, e.g. 10 30"); return
    fast, slow = (n1, n2) if n1 <= n2 else (n2, n1)
    tf = (tf or "").lower().strip()
    network = (maybe_net[0].lower().strip() if maybe_net else None)

    token_like = token_like.strip()
    token_symbol = None
    ds_address   = None

    if is_token_address(token_like):
        ds_address = token_like
        # Optional display symbol; non-fatal
        try:
            _, scraped_symbol, _ = fetch_onchain_price_and_meta(ds_address)
            token_symbol = (scraped_symbol or "UNKNOWN").upper()
        except Exception:
            token_symbol = "UNKNOWN"
        # If network wasn't provided, try to guess it (Dexscreener search → chainId → slug)
        if not network:
            try:
                network = guess_network_for_address(ds_address)  # returns e.g. 'eth','bsc','tron','base','arb','op','polygon','avax'
            except Exception:
                network = None
        if not network:
            await m.reply("Please add a network slug at the end (e.g. eth, bsc, tron, base, arbitrum, optimism, polygon, avax).")
            return
        on_conf = "ds_address,network,fast,slow,timeframe,tg_chat_id"
    else:
        token_symbol = token_like.upper()
        on_conf = "token_symbol,fast,slow,timeframe,tg_chat_id"

    row = {
        "token_symbol": token_symbol,
        "ds_address": ds_address,
        "network": network,
        "fast": fast, "slow": slow, "timeframe": tf,
        "is_enabled": True,
        "tg_chat_id": str(m.chat.id),
    }

    try:
        sb.table("signal_subscriptions").upsert(row, on_conflict=on_conf).execute()
    except APIError as e:
        await m.reply("DB missing unique index for this subscription.\n"
                      "Ensure sigsubs_ds_unique_idx includes (ds_address, network, fast, slow, timeframe, tg_chat_id).\n"
                      f"Details: {e}")
        return

    label = ds_address or token_symbol
    tail  = f" {network}" if ds_address else ""
    await m.reply(f"✅ Subscribed: {label}{tail} SMA{fast}/{slow} {tf}")


    



@dp.message(Command("ls"))
async def list_signals_cmd(m: types.Message):
    if not sb:
        await m.reply("Supabase not configured"); return
    resp = (sb.table("signal_subscriptions")
              .select("token_symbol, ds_address, fast, slow, timeframe")
              .eq("is_enabled", True)
              .eq("tg_chat_id", str(m.chat.id)).execute())
    rows = getattr(resp, "data", None) or []
    if not rows:
        await m.reply("No active subscriptions."); return
    lines = []
    for r in rows:
        label = r.get("ds_address") or (r.get("token_symbol") or "").upper()
        lines.append(f"- {label} SMA{r['fast']}/{r['slow']} {r['timeframe']}")
    await m.reply("Subscriptions:\n" + "\n".join(lines))


@dp.message(Command("rm"))
async def remove_signal_cmd(m: types.Message):
    """
    /rm <TOKEN> sma <fast> <slow> <timeframe>
    If TOKEN looks like an address => delete by ds_address.
    Else => delete by token_symbol.
    """
    if not sb:
        await m.reply("Supabase not configured"); return

    parts = (m.text or "").split()
    if len(parts) != 6 or parts[2].lower() != "sma":
        await m.reply("Usage: /rm <TOKEN> sma <fast> <slow> <timeframe>"); return

    _, token_like, _, a, b, tf = parts
    try:
        n1, n2 = int(a), int(b)
    except ValueError:
        await m.reply("Fast/slow must be integers, e.g. 10 30"); return
    fast, slow = (n1, n2) if n1 <= n2 else (n2, n1)
    tf = tf.lower()
    token_like = token_like.strip()
    chat_id = str(m.chat.id)

    if is_token_address(token_like):
        (sb.table("signal_subscriptions").delete()
           .eq("ds_address", token_like)
           .eq("fast", fast).eq("slow", slow)
           .eq("timeframe", tf).eq("tg_chat_id", chat_id)
           .execute())
        await m.reply(f"🗑️ Removed: {token_like} SMA{fast}/{slow} {tf}")
    else:
        sym = token_like.upper()
        (sb.table("signal_subscriptions").delete()
           .eq("token_symbol", sym)
           .eq("fast", fast).eq("slow", slow)
           .eq("timeframe", tf).eq("tg_chat_id", chat_id)
           .execute())
        await m.reply(f"🗑️ Removed: {sym} SMA{fast}/{slow} {tf}")


@dp.message(Command("rmconfirm"))
async def rm_confirm_cmd(m: types.Message):
    parts = (m.text or "").split()
    if len(parts) != 2 or not is_token_address(parts[1]):
        await m.reply("Usage: /rmconfirm <0x… or T…/41…>"); return

    pend = pop_pending_rm(m.chat.id)
    if not pend:
        await m.reply("No pending remove, start with /rm …"); return

    addr = normalize_tron_addr(parts[1])
    (sb.table("signal_subscriptions")
       .delete()
       .eq("token_address", addr)
       .eq("fast", pend["fast"]).eq("slow", pend["slow"])
       .eq("timeframe", pend["tf"])
       .eq("tg_chat_id", pend["tg"])
       .execute())
    await m.reply(f"🗑️ Removed: {addr} SMA{pend['fast']}/{pend['slow']} {pend['tf']}")


def _row_chat_id_tg(r) -> int | None:
    v = (r or {}).get("tg_chat_id")
    if v is None:
        return None
    try:
        return int(str(v))
    except Exception:
        return None

# --- signals watcher (replace your existing one) ---
# --- keep your other imports ---
 # <- make sure this is imported near the top

# ... (other code above) ...


async def signals_watcher(bot: Bot, interval: int = 15):
    """
    Periodically scans the `signals` table and sends alerts to subscribers.
    Supports SMA and RSI via the `source` column on `signals`.
    Dedupe per (chat_id, sig_key) using upsert into `signal_alerts`.
    """
    print(f"[signals] watcher running every {interval}s")
    last_id = 0

    while True:
        try:
            # 1) fetch new signals since last_id
            q = sb.table("signals").select(
                "id, token_symbol, token_address, ds_address, fast, slow, timeframe, "
                "signal, price, crossed_at, dedupe_key, source"
            )
            if last_id:
                q = q.gt("id", last_id)
            res = q.order("id", desc=False).limit(200).execute()
            rows = getattr(res, "data", None) or []

            for sig in rows:
                sid = sig["id"]

                core_ds   = (sig.get("ds_address") or "").strip()
                core_tron = (sig.get("token_address") or "").strip()
                core_sym  = (sig.get("token_symbol") or "").strip().upper()

                fast = sig.get("fast")
                slow = sig.get("slow")
                tf   = sig.get("timeframe")
                price = sig.get("price")
                crossed_at = sig.get("crossed_at")
                signal_str = sig.get("signal")

                src = (sig.get("source") or "sma").lower()
                src_label = "SMA" if src == "sma" else "RSI"

                # Build/normalize dedupe key
                sig_key = sig.get("dedupe_key")
                if not sig_key:
                    core = core_ds or core_tron or core_sym
                    crossed_iso = crossed_at if isinstance(crossed_at, str) else (
                        crossed_at.isoformat() if crossed_at else ""
                    )
                    sig_key = f"{core}|{tf}|{fast}|{slow}|{crossed_iso}"

                # 2) find subscribers (priority: ds > tron > symbol)
                if core_ds:
                    subs = (sb.table("signal_subscriptions")
                              .select("tg_chat_id")
                              .eq("ds_address", core_ds)
                              .eq("fast", fast).eq("slow", slow).eq("timeframe", tf)
                              .eq("is_enabled", True)
                              .execute())
                elif core_tron:
                    subs = (sb.table("signal_subscriptions")
                              .select("tg_chat_id")
                              .eq("token_address", core_tron)
                              .eq("fast", fast).eq("slow", slow).eq("timeframe", tf)
                              .eq("is_enabled", True)
                              .execute())
                else:
                    subs = (sb.table("signal_subscriptions")
                              .select("tg_chat_id")
                              .eq("token_symbol", core_sym)
                              .eq("fast", fast).eq("slow", slow).eq("timeframe", tf)
                              .eq("is_enabled", True)
                              .execute())
                subs_rows = getattr(subs, "data", None) or []

                # 3) notify subscribers
                label = core_ds or core_tron or core_sym
                for s in subs_rows:
                    chat_id = str(s["tg_chat_id"])

                    # 3a) record alert once per (chat_id, sig_key)
                    alert_row = {
                        "tg_chat_id": chat_id,
                        "sig_key": sig_key,
                        "ds_address": core_ds or None,
                        "token_address": core_tron or None,
                        "token_symbol": core_sym or None,
                        "fast": fast, "slow": slow, "timeframe": tf,
                        "signal": signal_str, "price": price,
                        "crossed_at": crossed_at,
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "strategy": src_label,  # store displayed strategy label
                    }
                    try:
                        (sb.table("signal_alerts")
                           .upsert(alert_row, on_conflict="tg_chat_id,sig_key")
                           .execute())
                    except Exception:
                        # If 'strategy' column does not exist yet
                        try:
                            alert_row.pop("strategy", None)
                            (sb.table("signal_alerts")
                               .upsert(alert_row, on_conflict="tg_chat_id,sig_key")
                               .execute())
                        except Exception:
                            pass

                    # 3b) compose message + button
                    txt = (
                        f"📈 {signal_str} — {label}\n"
                        f"{src_label}{fast}/{(slow if src == 'sma' else '')} {tf} @ {price}\n"
                        f"{crossed_at}"
                    ).replace("//", "/").replace("  ", " ")

                    base_cmd = "/buy" if signal_str == "BUY" else "/sell"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text=("Buy $100" if signal_str == "BUY" else "Sell $100"),
                            callback_data=f"deep:{base_cmd} {label} $100"
                        )
                    ]])

                    await bot.send_message(int(chat_id), txt, reply_markup=kb)

                last_id = max(last_id, sid)

        except Exception as e:
            print("[signals] watcher error:", type(e).__name__, e)

        await asyncio.sleep(interval)






# ---------- main ----------
async def main():
    bot = Bot(BOT_TOKEN)  # default session is fine & robust in 3.21

    # start the watcher once
    asyncio.create_task(signals_watcher(bot))

    print("Bot up. Press Ctrl+C to stop.")
    while True:
        try:
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
            )
        except Exception as e:
            print(f"[polling] restart after error: {type(e).__name__}: {e}")
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
