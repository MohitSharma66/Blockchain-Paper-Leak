
import os
from collections import defaultdict
from web3 import Web3
from dotenv import load_dotenv

load_dotenv(".env")
load_dotenv("backend/.env")

rpc = os.getenv("SEPOLIA_RPC_URL") or os.getenv("RPC_URL")
w3 = Web3(Web3.HTTPProvider(rpc))

hashes = input("Paste transaction hashes separated by commas: ").split(",")

names = {
    "0x6cb2d5b9": "reportPrintedCount",
    "0x2324fbf9": "releaseKey",
    "0xab610a07": "registerPaper",
}

totals = defaultdict(list)
total_fee = 0

print("\nFUNCTION | GAS USED | GAS PRICE (GWEI) | FEE (ETH)")

for h in hashes:
    h = h.strip()
    if not h.startswith("0x"):
        h = "0x" + h

    try:
        tx = w3.eth.get_transaction(h)
        receipt = w3.eth.get_transaction_receipt(h)

        gas = receipt["gasUsed"]
        price = receipt.get("effectiveGasPrice", tx.get("gasPrice", 0))
        fee = gas * price / 10**18
        function = names.get(tx["input"][:10], tx["input"][:10])

        totals[function].append((gas, fee))
        total_fee += fee

        print(
            f"{function} | {gas} | {price / 1e9:.4f} | {fee:.8f}"
        )
    except Exception as e:
        print(f"{h[:14]}... | ERROR: {e}")

print("\nAVERAGES BY FUNCTION")
for function, rows in totals.items():
    avg_gas = sum(x[0] for x in rows) / len(rows)
    avg_fee = sum(x[1] for x in rows) / len(rows)
    print(
        f"{function}: {len(rows)} transactions, "
        f"average gas {avg_gas:.0f}, average fee {avg_fee:.8f} ETH"
    )

print(f"\nTotal sample fees: {total_fee:.8f} ETH")
