#!/usr/bin/env python3
"""
printer_agent.py - the software that runs on ONE center's printer controller.

It does not depend on the board server for authorization. It watches the chain, and only when
the key has been released for ITS center does it fetch the encrypted blob, unwrap the AES key
with its own private key, decrypt in memory and print. Its keys live in its own folder
(devices/<center>/), never in the board's configuration.

  python printer_agent.py init --center center_001
      Creates this device's two key pairs and an enrollment file, and prints the public-key
      fingerprint. Upload the enrollment file on the board page; whoever enrolls the printer
      checks, in person, that the fingerprint the board shows matches the one printed here.

  python printer_agent.py run --center center_001 [--print]
      Waits (no manual step) until the center is registered and the key is released, then
      prints the copies still outstanding, in batches. Before each batch the new cumulative
      count is reported on-chain, so the ledger is never behind the paper. Exits when all
      expected copies are printed. Restarting is safe: it resumes from the on-chain count.

  python printer_agent.py fund-local --center center_001
      Hardhat only (chain id 31337): sends 0.1 test ETH to this device from the node's account 0.

Printing modes for `run`:
  (default)         SIMULATED: decrypts in memory and "prints" to the console; counts are recorded.
  --print           real printing to the default printer: `lp` (CUPS) on Linux / Raspberry Pi, or
                    SumatraPDF on Windows (set SUMATRA_PDF_EXE in printer-agent/.env).
  --print-cmd CMD   a custom command. {copies} becomes the batch size. Without {file} the PDF is
                    piped to stdin; with {file} it is written to a temp file only while the
                    command runs, then overwritten and deleted.
  --dry-run         verify the blob hash, unwrap and decrypt in memory, then stop: nothing is
                    printed and nothing is recorded on-chain.

The PDF is never written to disk, except for the temporary file Windows/SumatraPDF needs while
the print command runs. Print spoolers may keep their own copy; this agent cannot control that.

pip install web3 cryptography requests
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account
from web3 import Web3

HERE = Path(__file__).parent.resolve()
DEFAULT_DEPLOYMENT = HERE.parent / "backend" / "contract_deployment.json"


def _load_dotenv(path: Path) -> None:
    """This agent's own settings (printer-agent/.env); real environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv(HERE / ".env")


# ---------------------------------------------------------------------
# Crypto helpers (must match backend/keywrap.py)
# ---------------------------------------------------------------------

def oaep():
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def fingerprint(public_key) -> str:
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashlib.sha256(der).hexdigest()[:20].upper()
    return " ".join(h[i:i + 4] for i in range(0, 20, 4))


def raw_tx_bytes(signed):
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raise AttributeError("Signed transaction has no raw transaction bytes.")
    return raw


def private_file(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)  # ignored on Windows
    except OSError:
        pass


# ---------------------------------------------------------------------
# init / fund-local
# ---------------------------------------------------------------------

def device_dir(args) -> Path:
    return Path(args.home).resolve() / args.center if args.home else HERE / "devices" / args.center


def cmd_init(args):
    d = device_dir(args)
    if d.exists() and any(d.iterdir()) and not args.force:
        sys.exit(f"{d} already holds a device identity. Use --force to replace it "
                 f"(a replaced printer must be enrolled again).")
    d.mkdir(parents=True, exist_ok=True)

    account = Account.create()
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    private_file(d / "chain_key.hex", ("0x" + account.key.hex().removeprefix("0x")).encode())
    private_file(d / "wrap_private.pem", rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    pub = rsa_key.public_key()
    pub_pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    enrollment = {
        "center_id": args.center,
        "address": account.address,
        "public_key_pem": pub_pem,
        "fingerprint": fingerprint(pub),
    }
    path = d / f"enrollment_{args.center}.json"
    path.write_text(json.dumps(enrollment, indent=2), encoding="utf-8")

    print(f"Device identity created for {args.center}")
    print(f"  chain address  : {account.address}")
    print(f"  key fingerprint: {enrollment['fingerprint']}")
    print(f"  enrollment file: {path}")
    print()
    print("Upload the enrollment file on the board page (Enroll printers). The board shows a")
    print("fingerprint for this printer: check, in person, that it matches the one above before")
    print("registering the center. The private keys stay in this folder and never leave the device.")


def load_device(args) -> dict:
    d = device_dir(args)
    try:
        account = Account.from_key((d / "chain_key.hex").read_text().strip())
        rsa_key = serialization.load_pem_private_key((d / "wrap_private.pem").read_bytes(), password=None)
    except FileNotFoundError:
        sys.exit(f"No device identity in {d}. Run: python printer_agent.py init --center {args.center}")
    return {"account": account, "rsa": rsa_key}


def connect(args):
    deployment = Path(args.deployment)
    if not deployment.exists():
        sys.exit(f"Deployment file not found: {deployment} (use --deployment)")
    info = json.loads(deployment.read_text(encoding="utf-8"))
    rpc = args.rpc_url
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        sys.exit(f"Cannot reach the chain at {rpc}")
    contract = w3.eth.contract(address=Web3.to_checksum_address(info["address"]), abi=info["abi"])
    return w3, contract


def cmd_fund_local(args):
    dev = load_device(args)
    w3, _ = connect(args)
    if w3.eth.chain_id != 31337:
        sys.exit("Safety stop: fund-local is allowed only on chain ID 31337 (Hardhat).")
    accounts = w3.eth.accounts
    if not accounts:
        sys.exit("The node has no unlocked accounts to fund from.")
    tx = w3.eth.send_transaction({"from": accounts[0], "to": dev["account"].address,
                                  "value": Web3.to_wei(0.1, "ether")})
    if w3.eth.wait_for_transaction_receipt(tx).status != 1:
        sys.exit("Funding transaction failed.")
    print(f"Sent 0.1 test ETH to {dev['account'].address}")


# ---------------------------------------------------------------------
# chain access
# ---------------------------------------------------------------------

def get_state(contract, center_id: str) -> dict:
    registered, released, printer, expected, printed, start = contract.functions.getCenterState(center_id).call()
    return {"registered": registered, "released": released, "printer": printer,
            "expected": expected, "printed": printed, "start": start}


def send(w3, account, fn) -> str:
    """Sign locally and send. build_transaction estimates gas first, so a revert shows its reason."""
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
        **fee_fields(w3),
    })
    signed = w3.eth.account.sign_transaction(tx, account.key)
    receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw_tx_bytes(signed)), timeout=240)
    if receipt.status != 1:
        raise RuntimeError("The transaction was mined but failed on-chain.")
    return receipt.transactionHash.hex()


def fee_fields(w3) -> dict:
    base = w3.eth.get_block("latest").get("baseFeePerGas")
    if base is None:
        return {"gasPrice": w3.eth.gas_price}
    tip = Web3.to_wei(1, "gwei")
    return {"maxFeePerGas": int(base) * 2 + tip, "maxPriorityFeePerGas": tip}


# ---------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------

def run_with_temp_pdf(pdf: bytes, build_command):
    """Windows/SumatraPDF and {file} templates need a file: it exists only while the command runs."""
    fd, path = tempfile.mkstemp(suffix=".pdf")  # created with owner-only permissions
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf)
        command = build_command(path)
        # A custom template is a shell string; the SumatraPDF command is an argument list.
        return subprocess.run(command, shell=isinstance(command, str), capture_output=True, text=True, timeout=120)
    finally:
        try:
            with open(path, "r+b") as f:  # overwrite before deleting
                f.write(b"\0" * len(pdf))
            os.remove(path)
        except OSError:
            pass


def check_result(completed) -> None:
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no error output").strip()
        raise RuntimeError(f"Print command failed (exit {completed.returncode}): {detail}")


def print_batch(args, pdf: bytes, copies: int) -> None:
    if args.print_cmd:
        cmd = args.print_cmd.replace("{copies}", str(copies))
        if "{file}" in cmd:
            check_result(run_with_temp_pdf(pdf, lambda p: cmd.replace("{file}", f'"{p}"')))
        else:
            check_result(subprocess.run(cmd, shell=True, input=pdf, capture_output=True, timeout=120))
        return

    if args.print:
        if os.name == "nt":
            sumatra = os.environ.get("SUMATRA_PDF_EXE", "").strip()
            if not sumatra:
                raise RuntimeError("Windows printing needs SumatraPDF. Set SUMATRA_PDF_EXE in "
                                   "printer-agent/.env to the full path of SumatraPDF.exe.")
            exe = Path(sumatra).expanduser()
            if not exe.is_file():
                raise RuntimeError(f"SUMATRA_PDF_EXE does not point to a file: {exe}")
            target = ["-print-to", args.printer] if args.printer else ["-print-to-default"]
            check_result(run_with_temp_pdf(
                pdf, lambda p: [str(exe), *target, "-print-settings", f"{copies}x", p]))
        else:
            lp = shutil.which("lp")
            if not lp:
                raise RuntimeError("Printing needs the `lp` command (CUPS). Install/configure CUPS first.")
            cmd = [lp, "-n", str(copies)] + (["-d", args.printer] if args.printer else [])
            check_result(subprocess.run(cmd, input=pdf, capture_output=True, timeout=120))  # PDF via stdin
        print("   the print command was accepted by the local spooler "
              "(that does not itself prove sheets were produced)")
        return

    print(f"   [SIMULATED printer] {copies} copies of a {len(pdf)}-byte document (memory only)")
    time.sleep(args.delay)


# ---------------------------------------------------------------------
# the job
# ---------------------------------------------------------------------

def fetch_and_decrypt(args, dev, contract) -> bytes:
    center = args.center

    # 1. Encrypted blob from the board's server, checked against the hash registered ON-CHAIN.
    try:
        resp = requests.get(f"{args.api_url}/blob/{center}", timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        sys.exit(f"Could not download the blob: {e}")
    blob = resp.content
    onchain_hash = bytes(contract.functions.getBlobHash(center).call()).hex()
    if hashlib.sha256(blob).hexdigest() != onchain_hash:
        sys.exit("ABORT: the downloaded blob does not match the hash registered on-chain. Nothing was printed.")
    print("   blob hash matches the on-chain record")

    # 2. Unwrap the AES key with THIS device's private key. Another center's key won't open.
    wrapped = bytes(contract.functions.getKey(center).call())
    try:
        aes_key = dev["rsa"].decrypt(wrapped, oaep())
    except ValueError:
        sys.exit("ABORT: the wrapped key was not made for this device's key. Nothing was printed.")

    # 3. Decrypt in memory (AES-GCM also authenticates the blob).
    try:
        pdf = AESGCM(aes_key).decrypt(blob[:12], blob[12:], None)
    except Exception:
        sys.exit("ABORT: decryption failed (blob tampered with or wrong key). Nothing was printed.")
    finally:
        del aes_key
    if not pdf.startswith(b"%PDF-"):
        sys.exit("ABORT: the decrypted data is not a PDF. Nothing was printed.")
    print(f"   decrypted {len(pdf)} bytes in memory")
    return pdf


def run_job(args, dev, w3, contract, st) -> None:
    center = args.center
    remaining = st["expected"] - st["printed"]
    if remaining <= 0:
        print(f"Nothing to do: {st['printed']}/{st['expected']} copies already recorded on-chain.")
        return

    print(f"Key released. {st['printed']}/{st['expected']} copies recorded so far; {remaining} to print.")
    pdf = fetch_and_decrypt(args, dev, contract)

    if args.dry_run:
        print("Dry run: nothing printed, nothing recorded on-chain.")
        return

    count = st["printed"]
    while remaining > 0:
        n = min(args.batch, remaining)
        count += n
        # Ledger first, then paper: the on-chain count can never be behind what comes out.
        tx = send(w3, dev["account"], contract.functions.reportPrintedCount(center, count))
        print(f"   recorded {count}/{st['expected']} on-chain (tx {tx[:12]}...)")
        try:
            print_batch(args, pdf, n)
        except Exception as e:
            sys.exit(f"PRINT FAILED after recording {count}/{st['expected']} on-chain: {e}\n"
                     f"The ledger now counts {n} copies that may not exist. "
                     f"Reconcile with the printer's page counter.")
        remaining -= n

    del pdf
    final = get_state(contract, center)
    print(f"Done: {final['printed']}/{final['expected']} copies recorded on-chain.")


def cmd_run(args):
    dev = load_device(args)
    w3, contract = connect(args)
    me = dev["account"].address
    balance = w3.eth.get_balance(me)
    mode = ("DRY RUN" if args.dry_run else "REAL PRINTING" if (args.print or args.print_cmd) else "SIMULATED printing")
    print(f"Printer agent for {args.center}  [{mode}]")
    print(f"  address {me}  (balance {Web3.from_wei(balance, 'ether')} ETH)  chain {args.rpc_url}")
    if balance == 0 and not args.dry_run:
        print("  WARNING: no funds, so this printer cannot record prints. "
              "Ask the board to fund it (or use fund-local on Hardhat).")

    last = None
    while True:
        try:
            st = get_state(contract, args.center)
        except Exception as e:
            msg = f"chain not reachable ({e}); retrying"
        else:
            if not st["registered"]:
                msg = "waiting: this center is not registered yet"
            elif st["printer"].lower() != me.lower():
                sys.exit(f"ABORT: the printer registered for {args.center} is {st['printer']}, not this device.")
            elif not st["released"]:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["start"]))
                msg = f"waiting for key release (exam start {when}, chain time)"
            else:
                break
        if msg != last:
            print(msg)
            last = msg
        time.sleep(args.poll)

    run_job(args, dev, w3, contract, st)


# ---------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="NEET Integrity Chain printer agent")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--center", required=True, help="center id, e.g. center_001")
        sp.add_argument("--home", help="folder holding device identities (default: ./devices)")

    def chain(sp):
        # Empty settings count as unset, so a blank line in .env falls through to the next option.
        sp.add_argument("--rpc-url",
                        default=(os.environ.get("SEPOLIA_RPC_URL") or os.environ.get("RPC_URL")
                                 or "http://127.0.0.1:8545"),
                        help="chain RPC URL (default $SEPOLIA_RPC_URL, then $RPC_URL, then local Hardhat)")
        sp.add_argument("--deployment", default=str(DEFAULT_DEPLOYMENT), help="contract_deployment.json")

    i = sub.add_parser("init", help="create this device's keys + enrollment file")
    common(i)
    i.add_argument("--force", action="store_true", help="replace an existing identity")
    i.set_defaults(func=cmd_init)

    f = sub.add_parser("fund-local", help="Hardhat only: send 0.1 test ETH to this device")
    common(f)
    chain(f)
    f.set_defaults(func=cmd_fund_local)

    r = sub.add_parser("run", help="wait for release, then decrypt and print")
    common(r)
    chain(r)
    r.add_argument("--api-url", default=(os.environ.get("BACKEND_API_URL") or "http://127.0.0.1:8000").rstrip("/"),
                   help="board server, only used to download the encrypted blob")
    r.add_argument("--batch", type=int, default=25, help="copies per batch (the count is recorded before each batch)")
    r.add_argument("--poll", type=float, default=3.0, help="seconds between chain checks")
    r.add_argument("--delay", type=float, default=0.5, help="seconds per simulated batch")
    r.add_argument("--print", action="store_true", help="real printing: lp (Linux/Pi) or SumatraPDF (Windows)")
    r.add_argument("--printer", help="printer name for --print (default: the system default printer)")
    r.add_argument("--print-cmd", help="custom print command (see the header of this file)")
    r.add_argument("--dry-run", action="store_true", help="verify and decrypt in memory only; print and record nothing")
    r.set_defaults(func=cmd_run)

    args = p.parse_args()
    if args.command == "run":
        if args.batch < 1:
            sys.exit("--batch must be at least 1")
        if args.print and args.print_cmd:
            sys.exit("Use either --print or --print-cmd, not both.")
    args.func(args)


if __name__ == "__main__":
    main()
