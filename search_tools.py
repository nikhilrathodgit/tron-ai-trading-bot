# search_tools.py
# ──────────────────────────────────────────────────────────────────────────────
# Web search (SerpAPI) + LLM summarizer for /search, plus crypto utilities
# * Sources removed for cleaner output
# ──────────────────────────────────────────────────────────────────────────────

import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import asyncio
from decimal import Decimal
from typing import Dict, List, Optional
import requests
import ccxt
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage
from serpapi import GoogleSearch
from price_sources import is_token_address, fetch_onchain_price_and_meta

# ──────────────────────────────────────────────────────────────────────────────
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
SEARCH_MODEL    = os.getenv("SEARCH_MODEL", "gpt-4o-mini")
EXCHANGE_NAME   = os.getenv("MARKET_EXCHANGE", "binance")
_EX             = getattr(ccxt, EXCHANGE_NAME)({"enableRateLimit": True, "timeout": 20000})
_MARKETS_LOADED = False
_COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "ADA": "cardano",
    "TRX": "tron", "XRP": "ripple", "SOL": "solana",
    "DOGE": "dogecoin", "DOT": "polkadot", "LINK": "chainlink",
    "MATIC": "polygon-pos",
}

# ──────────────────────────────────────────────────────────────────────────────
def _ensure_markets():
    global _MARKETS_LOADED
    if _MARKETS_LOADED:
        return
    try:
        _EX.load_markets()
    finally:
        _MARKETS_LOADED = True

def _ccxt_pair(sym: str) -> str:
    return f"{sym.upper()}/USDT"

async def _ccxt_last_price(sym: str) -> Optional[Decimal]:
    def _fetch():
        _ensure_markets()
        t = _EX.fetch_ticker(_ccxt_pair(sym))
        return Decimal(str(t["last"]))
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return None

def _coingecko_simple_price(sym: str) -> Optional[Decimal]:
    cid = _COINGECKO_IDS.get(sym.upper())
    if not cid:
        return None
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cid, "vs_currencies": "usd"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        px = data.get(cid, {}).get("usd")
        return Decimal(str(px)) if px else None
    except Exception:
        return None

# ──────────────────────────────────────────────────────────────────────────────
async def get_token_price(token_or_addr: str) -> Dict:
    raw = (token_or_addr or "").strip()
    if is_token_address(raw):
        try:
            px, symbol, _ = fetch_onchain_price_and_meta(raw)
            if px and px > 0:
                return {"ok": True, "symbol": (symbol or "TOKEN").upper(), "price": Decimal(str(px)), "source": "dex"}
        except Exception:
            pass
        return {"ok": False, "error": "onchain_price_not_found"}
    sym = raw.upper()
    px = await _ccxt_last_price(sym)
    if px and px > 0:
        return {"ok": True, "symbol": sym, "price": px, "source": EXCHANGE_NAME}
    px2 = _coingecko_simple_price(sym)
    if px2 and px2 > 0:
        return {"ok": True, "symbol": sym, "price": px2, "source": "coingecko"}
    return {"ok": False, "error": "ticker_not_found"}

def explain_indicator(term: str) -> str:
    t = (term or "").lower().strip()
    return {
        "rsi": "RSI: momentum oscillator (0–100). Look for buy <30 and sell >70, preferably in trending conditions.",
        "sma": "SMA: simple moving average. Watch faster moving median crossing a slower one for trend signals.",
        "ema": "EMA: exponential moving average, gives more weight to recent data—reacts faster than SMA.",
        "macd": "MACD: difference between EMA12 & EMA26, with EMA9 signal. Watch crossovers and zero-line direction.",
    }.get(t, "Try: RSI, SMA, EMA, MACD.")

def tron_dex_volume_24h() -> Dict:
    endpoints = [
        "https://api.llama.fi/overview/dexs/tradingVolume/chain/24h?chain=Tron",
        "https://api.llama.fi/overview/dexs/tradingVolume/24h",
    ]
    try:
        r = requests.get(endpoints[0], timeout=10)
        if r.ok:
            data = r.json() or {}
            for it in data.get("chains", []):
                if it.get("name", "").lower() == "tron":
                    return {"ok": True, "usd_24h": Decimal(str(it.get("volume", 0))), "source": "defillama"}
        r2 = requests.get(endpoints[1], timeout=10)
        if r2.ok:
            for it in r2.json() or []:
                if it.get("name", "").lower() == "tron":
                    return {"ok": True, "usd_24h": Decimal(str(it.get("volume", 0))), "source": "defillama"}
    except Exception:
        pass
    return {"ok": False}

# ──────────────────────────────────────────────────────────────────────────────
def serp_search(query: str, num: int = 6) -> List[Dict]:
    key = os.getenv("SERPAPI_API_KEY")
    if not key:
        raise RuntimeError("SERPAPI_API_KEY missing")
    params = {"engine": "google", "q": query, "num": num if 3 <= num <= 10 else 6, "api_key": key, "hl": "en", "safe": "off"}
    res = GoogleSearch(params).get_dict()
    if "error" in res:
        raise RuntimeError(f"SerpAPI error: {res['error']}")
    org = res.get("organic_results") or []
    return [{"title": (r.get("title") or "").strip(), "link": (r.get("link") or "").strip(), "snippet": (r.get("snippet") or "").strip()} for r in org[:num]]

def _llm_task_summary(task_line: str, question: str, bullets: List[Dict], system_hint: str = "") -> str:
    if not OPENAI_API_KEY:
        return f"Task: {task_line}\nCouldn’t use language model; please try again later."
    llm = ChatOpenAI(model=SEARCH_MODEL, temperature=0.2)
    context = "\n\n".join(f"{b['title']}\n{b['link']}\n{b['snippet']}" for b in bullets)
    prompt = (
        "You are a concise assistant for a Telegram trading bot.\n"
        "Return PLAIN TEXT only. Structure as:\n"
        f"Task: {task_line}\n"
        "\n\n"
        "<one coherent paragraph, max 3 sentences>\n"
        "- up to 3 actionable short bullets (optional)\n\n"
        "Question: {question}\n"
        "Source snippets:\n"
        f"{context}\n\n"
        f"{system_hint}\n"
    )
    msg = llm([HumanMessage(content=prompt)])
    return msg.content.strip()

def research_strategies(question: Optional[str] = None) -> str:
    q = (question or "crypto strategies using SMA/RSI/MACD").strip()
    try:
        hits = serp_search(q, 6)
    except Exception:
        hits = []
    system = "The bot supports: /cs, /buy, /sell, /ask. Include example commands."
    return _llm_task_summary("Suggest strategies to run with this bot", q, hits, system_hint=system)

def research_general(query: str) -> str:
    try:
        hits = serp_search(query, 6)
    except Exception:
        hits = []
    task_line = "Summarise cryptocurrencies to beginner" if "crypto" in query.lower() else "Answer the user's question concisely"
    return _llm_task_summary(task_line, query, hits, system_hint="Be neutral, factual, concise.")
