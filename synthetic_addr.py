from hashlib import sha3_256

DEFAULT_NAMESPACE = "tron-algo-demo-v1"
MAGIC = 0x99

def make_synth_hex41(symbol: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        raise ValueError("symbol required")
    if len(s) > 15:
        s = s[:15]
    payload = bytearray(20)
    payload[0] = MAGIC
    payload[1] = len(s)
    payload[2:2+len(s)] = s.encode("ascii")
    h = sha3_256((namespace + "|" + s).encode()).digest()
    fill_from = 2 + len(s)
    payload[fill_from:] = h[:(20 - fill_from)]
    return "41" + payload.hex()
