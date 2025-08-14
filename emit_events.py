import argparse, json, os, sys
from dotenv import load_dotenv
from tronpy import Tron
from tronpy.providers import HTTPProvider
from tronpy.keys import PrivateKey

def load_env():
    load_dotenv()
    node_url = os.getenv("TRON_NODE_URL", "https://api.nileex.io")
    pk = os.getenv("TRON_PRIVATE_KEY")
    addr = os.getenv("NILE_CONTRACT_ADDRESS")
    if not pk:
        print("[ERR] TRON_PRIVATE_KEY missing in .env", file=sys.stderr); sys.exit(1)
    if not addr:
        print("[ERR] NILE_CONTRACT_ADDRESS missing in .env", file=sys.stderr); sys.exit(1)
    abi_str = os.getenv("NILE_TRC20_ABI")  # get string from .env
    abi = json.loads(abi_str) 
    return node_url, pk, addr, abi

def get_contract(client, address, abi):
    return client.get_contract(address)


def submit_tx(tx_builder, privkey_hex):
    # derive address from your private key
    priv = PrivateKey(bytes.fromhex(privkey_hex.replace("0x","")))
    from_addr = priv.public_key.to_base58check_address()

    # set the tx owner to YOU, then build, sign, broadcast, wait
    tx = (
        tx_builder
        .with_owner(from_addr)     # <-- IMPORTANT
        .fee_limit(5_000_000)      # reasonable on Nile; adjust if needed
        # .permission_id(2)        # only if you use a non-default active permission
        .build()
        .sign(priv)
    )
    result = tx.broadcast().wait()
    print("✅ TX:", result["id"])
    return result


def main():
    node_url, private_key, contract_address, abi = load_env()
    client = Tron(HTTPProvider(node_url))
    c = get_contract(client, contract_address, abi)

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open", help="Emit TradeOpen via logTradeOpen")
    p_open.add_argument("--token-address", required=True, help="T... base58")
    p_open.add_argument("--strategy", required=True)
    p_open.add_argument("--action", required=True, choices=["BUY","SELL"])
    p_open.add_argument("--entry-price", required=True, type=int)
    p_open.add_argument("--amount", required=True, type=int)

    p_close = sub.add_parser("close", help="Emit TradeClosed via logTradeClosed")
    p_close.add_argument("--trade-id", required=True, type=int)
    p_close.add_argument("--token-address", required=True)
    p_close.add_argument("--exit-price", required=True, type=int)
    p_close.add_argument("--pnl", required=True, type=int)

    args = parser.parse_args()

    if args.cmd == "open":
        txb = c.functions.logTradeOpen(
            args.token_address, args.strategy, args.action, args.entry_price, args.amount
        )
        submit_tx(txb, private_key)
    elif args.cmd == "close":
        txb = c.functions.logTradeClosed(
            args.trade_id, args.token_address, args.exit_price, args.pnl
        )
        submit_tx(txb, private_key)

if __name__ == "__main__":
    main()
