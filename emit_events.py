import argparse, json, os, sys, inspect
from dotenv import load_dotenv
from tronpy import Tron
from tronpy.providers import HTTPProvider
from tronpy.keys import PrivateKey
import time

def load_env():
    load_dotenv()
    node_url = os.getenv("TRON_NODE_URL", "https://api.nileex.io")
    pk = os.getenv("TRON_PRIVATE_KEY")
    addr = os.getenv("NILE_CONTRACT_ADDRESS")
    abi_str = os.getenv("NILE_TRC20_ABI")  # optional; only needed for wrapper path

    if not pk:
        print("[ERR] TRON_PRIVATE_KEY missing in .env", file=sys.stderr); sys.exit(1)
    if not addr:
        print("[ERR] NILE_CONTRACT_ADDRESS missing in .env", file=sys.stderr); sys.exit(1)

    abi = None
    if abi_str:
        try:
            abi = json.loads(abi_str)
        except Exception as e:
            print(f"[WARN] NILE_TRC20_ABI present but not valid JSON: {e}")

    return node_url, pk, addr, abi

def get_contract_any(client: Tron, address: str, abi):
    """Try all known wrappers; return None if unsupported by this tronpy build."""
    try:
        sig = inspect.signature(client.get_contract)
        if "abi" in sig.parameters and abi is not None:
            return client.get_contract(address, abi=abi)
    except Exception:
        pass
    if hasattr(client, "get_contract_from_abi") and abi is not None:
        try:
            return client.get_contract_from_abi(address, abi)
        except Exception:
            pass
    try:
        # works only if the contract is verified on-chain
        return client.get_contract(address)
    except Exception:
        return None

def send_function_tx(client: Tron, priv_hex: str, contract_addr: str, selector: str, params: list):
    """Universal path: call by function signature string (no ABI needed)."""
    priv = PrivateKey(bytes.fromhex(priv_hex.replace("0x", "")))
    owner = priv.public_key.to_base58check_address()
    tb = client.trx.trigger_smart_contract(
        contract_addr,
        selector,      # e.g. "logTradeOpen(address,string,string,string,uint256,uint256)"
        params,        # param list must match selector types
        owner_address=owner,
    )
    tx = tb.fee_limit(5_000_000).build().sign(priv)
    res = tx.broadcast().wait()
    print("TX:", res.get("id") if isinstance(res, dict) else res)
    return res

from tronpy.keys import PrivateKey

# emit_events.py
import time
from tronpy.keys import PrivateKey

def _txid_hex(tx):
    tid = getattr(tx, "txid", None)
    if tid is None:
        return None
    try:
        return tid.hex()
    except Exception:
        return str(tid)

def submit_tx(client, tx_builder, privkey_hex, fee_limit=100_000_000, timeout=90, interval=2):
    """Sign->broadcast->poll for confirmation (for tronpy builds without tx.wait())."""
    pk_hex = privkey_hex.replace("0x", "").strip()
    priv = PrivateKey(bytes.fromhex(pk_hex))
    owner_addr = priv.public_key.to_base58check_address()

    b = tx_builder
    try:
        if hasattr(b, "with_owner"): b = b.with_owner(owner_addr)
    except Exception: pass
    try:
        if hasattr(b, "fee_limit"):  b = b.fee_limit(fee_limit)
    except Exception: pass

    tx = b.build().sign(priv)
    # fallback fee_limit on built tx
    try:
        if getattr(tx, "fee_limit", None) in (None, 0): setattr(tx, "fee_limit", fee_limit)
    except Exception: pass

    br = tx.broadcast()
    txid = _txid_hex(tx)
    print("TX (broadcast):", txid or br)

    # ---- poll for confirmation ----
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            info = client.get_transaction_info(txid)
            # when confirmed, Tron node returns a dict with receipt/result
            if info and isinstance(info, dict) and info.get("receipt"):
                result = info["receipt"].get("result", "")
                print("TX (confirmed):", txid, result or "")
                return {"id": txid, "info": info}
        except Exception as e:
            last_err = e
        time.sleep(interval)
    raise RuntimeError(f"tx {txid} not confirmed in {timeout}s; last_err={last_err}")




def main():
    node_url, private_key, contract_address, abi = load_env()
    client = Tron(HTTPProvider(node_url))
    c = get_contract_any(client, contract_address, abi)  # may be None on some tronpy versions

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open", help="Emit TradeOpen via logTradeOpen")
    p_open.add_argument("--token-address", required=True, help="T... base58")
    p_open.add_argument("--token-symbol", required=True, help="Ticker like TRX/LINK/ADA")
    p_open.add_argument("--strategy", required=True)
    p_open.add_argument("--action", required=True, choices=["BUY","SELL"])
    p_open.add_argument("--entry-price", required=True, type=int)
    p_open.add_argument("--amount", required=True, type=int)

    p_close = sub.add_parser("close", help="Emit TradeClosed via logTradeClosed")
    p_close.add_argument("--trade-id", required=True, type=int)
    p_close.add_argument("--token-address", required=True)
    p_close.add_argument("--token-symbol", required=True)
    p_close.add_argument("--exit-price", required=True, type=int)
    p_close.add_argument("--pnl", required=True, type=int)
    p_close.add_argument("--sell-amount", required=True, type=int)  # supports partial closes

    args = parser.parse_args()

    if args.cmd == "open":
        if c is not None and hasattr(c, "functions"):
            txb = c.functions.logTradeOpen(
                args.token_address,
                args.token_symbol, 
                args.strategy, 
                args.action, 
                args.entry_price, 
                args.amount
            )
            submit_tx(client, txb, private_key)

        else:
            send_function_tx(
                client, private_key, contract_address,
                "logTradeOpen(address,string,string,string,uint256,uint256)",
                [args.token_address,
                 args.token_symbol, 
                 args.strategy, 
                 args.action, 
                 args.entry_price, 
                 args.amount],
            )

    elif args.cmd == "close":
        if c is not None and hasattr(c, "functions"):
            txb = c.functions.logTradeClosed(
                args.trade_id, 
                args.token_address,
                args.token_symbol, 
                args.exit_price, 
                args.pnl, 
                args.sell_amount
            )
            submit_tx(client, txb, private_key)
        else:
            send_function_tx(
                client, private_key, contract_address,
                "logTradeClosed(uint256,address,string,uint256,int256,uint256)",
                [args.trade_id, 
                 args.token_address, 
                 args.token_symbol,
                 args.exit_price, 
                 args.pnl, 
                 args.sell_amount],
            )

if __name__ == "__main__":
    main()
