
import os
import re
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent
from langchain.agents import AgentType
from langchain_openai import ChatOpenAI
from decimal import Decimal

# ------------------ CONFIG ------------------
INCLUDE = [
    "v_positions", "v_trades", "v_pnl_daily", "v_subscriptions", "v_last_signal", "v_pnl_now",
    # (optional) let the agent see base tables too:
    "trade_history", "open_trades", "signals", "signal_subscriptions", "signal_alerts",
]

load_dotenv()
DB_URI = os.getenv("DB_URI")
RAW_ENGINE = create_engine(
    DB_URI,
    pool_pre_ping=True,
    connect_args={"sslmode": "require"},  # pooler/TLS safe
    isolation_level="AUTOCOMMIT",         # PgBouncer-friendly
)
print("[agent] SQLAlchemy engine created (raw).")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("AGENT_MODEL", "gpt-4o-mini")

if not DB_URI:
    raise RuntimeError("DB_URI missing (.env)")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing (.env)")

ENGINE_ARGS = {
    "pool_pre_ping": True,
    "connect_args": {"sslmode": "require"},  
    "isolation_level": "AUTOCOMMIT",         
}

# ------------------ UTILS ------------------


NUM = lambda x: f"{Decimal(str(x)):.6f}".rstrip("0").rstrip(".")

def _mk_table(rows, headers):
    # very small monospace table for Telegram
    colw = [max(len(h), *(len(str(r.get(h, ""))) for r in rows)) for h in headers]
    def line(vals): return " | ".join(str(v).ljust(w) for v, w in zip(vals, colw))
    head = line(headers)
    bar  = "-+-".join("-" * w for w in colw)
    body = "\n".join(line([r.get(h, "") for h in headers]) for r in rows)
    return f"```\n{head}\n{bar}\n{body}\n```"

def _q(conn, sql, **kw):
    res = conn.execute(text(sql), kw)
    cols = list(res.keys())
    return [dict(zip(cols, row)) for row in res.fetchall()]

def _intent_last_n_trades(conn, n=5):
    rows = _q(conn, """
        select token_symbol, action, amount, price, "timestamp"
        from v_trades
        order by "timestamp" desc
        limit :n;
    """, n=n)
    if not rows:
        return "No trades found."
    # normalize numbers
    for r in rows:
        r["amount"] = NUM(r["amount"])
        r["price"]  = NUM(r["price"])
    return _mk_table(rows, ["token_symbol","action","amount","price","timestamp"])

def _intent_pnl_today(conn):
    """
    Returns portfolio PnL for *today* in USD.
    Order of fallbacks:
      1) v_pnl_daily(day = current_date)
      2) SUM(trade_history.realized_pnl_usd) WHERE timestamp::date = today
      3) SUM(v_trades.pnl) WHERE action='SELL' AND timestamp::date = today  (works in your schema)
    Always returns a numeric value (0 if no rows) with green/red square.
    """
    from decimal import Decimal

    
    try:
        rows = _q(conn, """
            select pnl_usd
            from v_pnl_daily
            where day = current_date
            limit 1;
        """)
        if rows:
            v = Decimal(str(rows[0]["pnl_usd"] or 0))
            emoji = "🟩" if v >= 0 else "🟥"
            return f"Ticker: ALL\nMetric: PnL Today\nValue: ${NUM(v)} {emoji}"
    except Exception:
        pass

    # 2) trade_history realized pnl for today
        rows = _q(conn, """
            select coalesce(sum(realized_pnl_usd), 0) as pnl_usd
            from trade_history
            where ("timestamp" at time zone 'UTC')::date = (now() at time zone 'UTC')::date;
        """)
        v = Decimal(str(rows[0]["pnl_usd"] or 0)) if rows else Decimal(0)
        emoji = "🟩" if v >= 0 else "🟥"
        return f"Ticker: ALL\nMetric: PnL Today\nValue: ${NUM(v)} {emoji}"
    except Exception:
        pass

    # 3) Fallback that works with current views
    try:
        rows = _q(conn, """
            select coalesce(sum(pnl), 0) as pnl_usd
            from v_trades
            where action = 'SELL'
              and ("timestamp" at time zone 'UTC')::date = (now() at time zone 'UTC')::date;
        """)
        v = Decimal(str(rows[0]["pnl_usd"] or 0)) if rows else Decimal(0)
        emoji = "🟩" if v >= 0 else "🟥"
        return f"Ticker: ALL\nMetric: PnL Today\nValue: ${NUM(v)} {emoji}"
    except Exception:
        return "Ticker: ALL\nMetric: PnL Today\nValue: $0 🟩"


def _intent_most_profitable_trade(conn, period: str | None):
    # Period may be "week", "month" etc.; use it to filter by timestamp if present
    date_filter = ""
    if period == "week":
        date_filter = "and \"timestamp\" >= date_trunc('week', now())"
    elif period == "month":
        date_filter = "and \"timestamp\" >= date_trunc('month', now())"

    # Try to find realized winners in trade_history (close events)
    try:
        rows = _q(conn, f"""
            select token_symbol, realized_pnl_usd as pnl, "timestamp"
            from trade_history
            where realized_pnl_usd is not null {date_filter}
            order by realized_pnl_usd desc
            limit 1;
        """)
        if rows:
            sym = rows[0]["token_symbol"] or "UNKNOWN"
            v   = Decimal(str(rows[0]["pnl"] or 0))
            emoji = "🟩" if v >= 0 else "🟥"
            return f"Ticker: {sym}\nMetric: Best Realized Trade{' (this '+period+')' if period else ''}\nValue: ${NUM(v)} {emoji}"
    except Exception:
        pass

    # Fallback to last 1 winner by difference (requires views with entry/exit or similar)
    rows = _q(conn, """
        select token_symbol, action, amount, price, "timestamp"
        from v_trades
        order by "timestamp" desc
        limit 1;
    """)
    if rows:
        sym = rows[0]["token_symbol"] or "UNKNOWN"
        return f"Ticker: {sym}\nMetric: Best Trade\nValue: (insufficient fields to compute realized PnL)"
    return "No trades found."


def _mask_db_uri(uri: str) -> str:
    # masking password for debug prints
    return re.sub(r'(:\/\/[^:]+:)([^@]+)(@)', r'\1***\3', uri or "")

def _fmt_money(v: Any) -> str:
    try:
        return f"${float(v):,.4f}"
    except Exception:
        return str(v)

def _fmt_units(v: Any) -> str:
    try:
        f = float(v)
        return f"{f:,.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v)

def _fmt_ts(ts: Any) -> str:
    try:
        if isinstance(ts, str):
            return ts.replace("T", " ").replace("+00:00", " UTC")
        return str(ts)
    except Exception:
        return str(ts)

# ------------------ DB (GLOBAL) ------------------
print("[agent] initializing DB connection…")
print(f"[agent] DB_URI: {_mask_db_uri(DB_URI)}")
print(f"[agent] include tables: {INCLUDE}")

try:
    db: SQLDatabase = SQLDatabase.from_uri(DB_URI, include_tables=INCLUDE, engine_args=ENGINE_ARGS)
    print("[agent] SQLDatabase constructed with include_tables.")
except ValueError as e:
    print(f"[agent] include_tables failed ({e}); falling back to full schema reflect.")
    db = SQLDatabase.from_uri(DB_URI, engine_args=ENGINE_ARGS)

# ------------------ LLM AGENT (fallback for open-ended) ------------------
_llm = ChatOpenAI(model=MODEL, temperature=0)
_sql_agent = create_sql_agent(
    llm=_llm,
    db=db,
    agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
    verbose=False,
)

# ------------------ LOW-LEVEL DB HELPERS ------------------
def q(sql: str) -> List[Dict[str, Any]]:
    """Run a SELECT via raw SQLAlchemy so we always get dict rows."""
    print(f"[agent.q] running SQL (raw):\n{sql.strip()}")
    try:
        with RAW_ENGINE.connect() as conn:
            res = conn.execute(text(sql))
            rows = [dict(r._mapping) for r in res]
            print(f"[agent.q] rows={len(rows)}")
            if rows[:1]:
                print(f"[agent.q] sample row: {rows[0]}")
            return rows
    except Exception as e:
        print(f"[agent.q] ERROR: {type(e).__name__}: {e}")
        return []


def q_scalar(sql: str, default=None):
    rows = q(sql)
    if not rows:
        return default
    # return first col of first row
    first = rows[0]
    if not isinstance(first, dict):
        return first
    return next(iter(first.values()))

# ------------------ DIAGNOSTIC PROBE ------------------
def debug_probe():
    print("\n[agent.debug] === schema visibility ===")
    # What views exist?
    vnames = "', '".join([v for v in INCLUDE if v.startswith("v_")])
    rows = q(f"""
        select table_name
        from information_schema.views
        where table_schema='public'
          and table_name in ('{vnames}');
    """)
    print("[agent.debug] views visible:", [r["table_name"] for r in rows] if rows else "none")

    # Row counts
    for t in ["v_trades", "v_positions", "v_pnl_daily", "v_subscriptions", "v_last_signal", "v_pnl_now"]:
        try:
            c = q_scalar(f"select count(*) as c from {t};", default=None)
            print(f"[agent.debug] count({t}) = {c}")
        except Exception as e:
            print(f"[agent.debug] count({t}) failed: {e}")

    # Show last 5 trade rows verbatim
    print("\n[agent.debug] last 5 from v_trades (raw):")
    rows = q("select * from v_trades limit 5;")
    for i, r in enumerate(rows, 1):
        print(f"  {i}. {r}")

# ------------------ INTENT HANDLERS (crisp formatting) ------------------
def _last_5_trades() -> str:
    sql = """
    select token_symbol, action, amount, price, "timestamp" as timestamp
    from v_trades
    limit 5;
    """
    rows = q(sql)
    if not rows:
        return "No trades found."
    lines = ["Last 5 trades:"]
    for r in rows:
        sym = (r.get("token_symbol") or "UNKNOWN").upper()
        act = (r.get("action") or "").upper()
        amt = _fmt_units(r.get("amount"))
        px  = _fmt_money(r.get("price"))
        ts  = _fmt_ts(r.get("timestamp"))
        lines.append(f"{act} {sym} {amt} @ {px} ({ts})")
    return "\n".join(lines)

def _subs_for_chat(chat_id: str) -> str:
    sql = f"""
    select token_symbol, addr_label, network, fast, slow, timeframe, is_enabled
    from v_subscriptions
    where tg_chat_id = '{chat_id}'
    order by token_symbol nulls last, addr_label nulls last, timeframe;
    """
    rows = q(sql)
    if not rows:
        return "No active subscriptions."
    lines = ["Subscriptions:"]
    for r in rows:
        label = (r.get("token_symbol") or "").upper() or (r.get("addr_label") or "")
        strat = "SMA"
        lines += [
            f"Token: {label}",
            f"Signal: {strat}",
            f"Fast: {r.get('fast')}",
            f"Slow: {r.get('slow')}",
            f"Timeframe: {r.get('timeframe')}",
            (f"Network: {r.get('network')}" if r.get("addr_label") else ""),
            ""
        ]
    return "\n".join(lines).rstrip()

def _last_signal() -> str:
    sql = """
    select token_symbol, ds_address, signal, timeframe, fast, slow, crossed_at
    from v_last_signal
    order by crossed_at desc nulls last
    limit 1;
    """
    rows = q(sql)
    if not rows:
        return "No signals found."
    r = rows[0]
    label = (r.get("token_symbol") or "").upper() or (r.get("ds_address") or "UNKNOWN")
    strat = "SMA"
    return (
        f"Last signal: {str(r.get('signal'))} — {label}\n"
        f"Strategy: {strat} {r.get('fast')}/{r.get('slow')} {r.get('timeframe')}\n"
        f"At: {_fmt_ts(r.get('crossed_at'))}"
    )

def _pnl_now() -> str:
    rows = q("select total_pnl_now from v_pnl_now limit 1;")
    if rows:
        return f"Total PnL (realized + unrealized): {_fmt_money(rows[0].get('total_pnl_now'))}"
    r2 = q("select sum(pnl) as pnl from v_trades where action='SELL';")
    v2 = r2[0].get("pnl") if r2 else 0
    return f"Total realized PnL: {_fmt_money(v2)}"

def _most_profitable_day() -> str:
    rows = q("""
    select d, pnl from v_pnl_daily
    order by pnl desc nulls last
    limit 1;
    """)
    if not rows:
        return "No realized PnL yet."
    r = rows[0]
    return f"Most profitable day: {r['d']} with PnL {_fmt_money(r['pnl'])}"

def _biggest_loser_trade() -> str:
    rows = q("""
    select token_symbol, action, amount, price, "timestamp" as timestamp, pnl
    from v_trades
    where pnl is not null
    order by pnl asc
    limit 1;
    """)
    if not rows:
        return "No losing trade found."
    r = rows[0]
    sym = (r.get("token_symbol") or "UNKNOWN").upper()
    act = (r.get("action") or "").upper()
    amt = _fmt_units(r.get("amount")); px = _fmt_money(r.get("price")); ts = _fmt_ts(r.get("timestamp"))
    return f"Worst trade: {act} {sym} {amt} @ {px} ({ts}) — Loss {_fmt_money(r.get('pnl'))}"

def _open_positions_now() -> str:
    rows = q("""
    select token_symbol, token_address, avg_entry_price, amount, strategy
    from v_positions
    order by token_symbol nulls last, token_address;
    """)
    if not rows:
        return "No open positions."
    lines = ["Open positions:"]
    for r in rows:
        sym = (r.get("token_symbol") or "UNKNOWN").upper()
        amt = _fmt_units(r.get("amount")); px = _fmt_money(r.get("avg_entry_price"))
        strat = r.get("strategy") or "UNKNOWN"
        lines.append(f"{sym} — {amt} @ {px} (strategy: {strat})")
    return "\n".join(lines)

# ------------------ ROUTER ------------------
def _ask_db_formatted(question: str, chat_id: Optional[str]) -> str:
    q = (question or "").strip()
    print("[agent.router] question=%r chat_id=%r" % (q, chat_id))
    try:
        with RAW_ENGINE.connect() as conn:
            ql = q.lower()

            #  1) last N trades 
            m = re.search(r"(last|recent)\s+(\d+)\s+trades", ql)
            if m:
                n = max(1, min(50, int(m.group(2))))
                return _intent_last_n_trades(conn, n)

            if "last 5 trades" in ql or "last five trades" in ql:
                return _intent_last_n_trades(conn, 5)

            #  2) pnl today / this week / this month 
            if "pnl today" in ql or "today's pnl" in ql:
                return _intent_pnl_today(conn)

            if "most profitable trade" in ql or "best trade" in ql:
                period = "week" if "week" in ql else ("month" if "month" in ql else None)
                return _intent_most_profitable_trade(conn, period)

            

        # --- Fallback: existing open-ended SQL agent ---
        print("[agent.router] falling back to open-ended SQL agent")
        return _sql_agent.run(q)
    except Exception as e:
        return f"Query failed: {type(e).__name__}: {e}"

# ------------------ PUBLIC ENTRY ------------------
def ask_db(question: str, chat_id: Optional[str] = None) -> str:
    return _ask_db_formatted(question, chat_id)

# ------------------ STANDALONE DEBUG ------------------
if __name__ == "__main__":
    print("[agent.__main__] Starting debug probe…")
    debug_probe()
    print("\n[agent.__main__] Demo: last 5 trades")
    print(_last_5_trades())
    print("\n[agent.__main__] Demo: subscriptions for fake chat_id=TEST")
    print(_subs_for_chat("TEST"))
    print("\n[agent.__main__] Demo: last signal")
    print(_last_signal())
    print("\n[agent.__main__] Demo: open positions")
    print(_open_positions_now())
    print("\n[agent.__main__] Demo: pnl now")
    print(_pnl_now())
