import os
from dotenv import load_dotenv
from tronpy import Tron
from tronpy.keys import PrivateKey
import json

TRC20_ABI = json.loads("""
[
  {"inputs":[],"stateMutability":"nonpayable","type":"constructor"},
  {"anonymous":false,"inputs":[
    {"indexed":true,"internalType":"address","name":"owner","type":"address"},
    {"indexed":true,"internalType":"address","name":"spender","type":"address"},
    {"indexed":false,"internalType":"uint256","name":"value","type":"uint256"}
  ],"name":"Approval","type":"event"},
  {"anonymous":false,"inputs":[
    {"indexed":true,"internalType":"address","name":"from","type":"address"},
    {"indexed":true,"internalType":"address","name":"to","type":"address"},
    {"indexed":false,"internalType":"uint256","name":"value","type":"uint256"}
  ],"name":"Transfer","type":"event"},
  {"inputs":[
    {"internalType":"address","name":"owner","type":"address"},
    {"internalType":"address","name":"spender","type":"address"}
  ],"name":"allowance","outputs":[
    {"internalType":"uint256","name":"","type":"uint256"}
  ],"stateMutability":"view","type":"function"},
  {"inputs":[
    {"internalType":"address","name":"spender","type":"address"},
    {"internalType":"uint256","name":"amount","type":"uint256"}
  ],"name":"approve","outputs":[
    {"internalType":"bool","name":"","type":"bool"}
  ],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[
    {"internalType":"address","name":"account","type":"address"}
  ],"name":"balanceOf","outputs":[
    {"internalType":"uint256","name":"","type":"uint256"}
  ],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"decimals","outputs":[
    {"internalType":"uint8","name":"","type":"uint8"}
  ],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"name","outputs":[
    {"internalType":"string","name":"","type":"string"}
  ],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"symbol","outputs":[
    {"internalType":"string","name":"","type":"string"}
  ],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"totalSupply","outputs":[
    {"internalType":"uint256","name":"","type":"uint256"}
  ],"stateMutability":"view","type":"function"},
  {"inputs":[
    {"internalType":"address","name":"recipient","type":"address"},
    {"internalType":"uint256","name":"amount","type":"uint256"}
  ],"name":"transfer","outputs":[
    {"internalType":"bool","name":"","type":"bool"}
  ],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[
    {"internalType":"address","name":"sender","type":"address"},
    {"internalType":"address","name":"recipient","type":"address"},
    {"internalType":"uint256","name":"amount","type":"uint256"}
  ],"name":"transferFrom","outputs":[
    {"internalType":"bool","name":"","type":"bool"}
  ],"stateMutability":"nonpayable","type":"function"}
]
""")



# Loading environment variables
load_dotenv()

PRIVATE_KEY = os.getenv("TRON_PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("TRON_CONTRACT_ADDRESS")
NETWORK = os.getenv("TRON_NETWORK", "nile")  

print("PRIVATE_KEY from .env:", PRIVATE_KEY)
print("Length:", len(PRIVATE_KEY))
print("Using network:", NETWORK)
print("Contract address:", CONTRACT_ADDRESS)


# Connecting to Tron
client = Tron(network=NETWORK)

# Loading wallet
priv_key = PrivateKey(bytes.fromhex(PRIVATE_KEY))
wallet_address = priv_key.public_key.to_base58check_address()
print(f"Connected Wallet: {wallet_address}")

# Load deployed contract
contract = client.get_contract(CONTRACT_ADDRESS)
contract.abi = TRC20_ABI

# Checking balance"
balance = contract.functions.balanceOf(wallet_address)
print(f"Your token balance: {balance}")

# Transferring tokens to receiver as a test
recipient = "TTpp31DebKj4KR7u5SYP1qSgHkz9d9Wd2a"
amount = 1_000_000  # 1 token because decimals = 6

txn = (
    contract.functions.transfer(recipient, amount)
    .with_owner(wallet_address)
    .fee_limit(10_000_000)
    .build()
    .sign(priv_key)
    .broadcast()
)

print(f"Transaction sent: {txn['txid']}")
