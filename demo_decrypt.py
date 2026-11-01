"""
demo_decrypt.py  --  TEST TOOL ONLY. Not part of the app; do not deploy it.

Decrypts a center's encrypted paper so you can see what the printer would print.
It writes the PLAINTEXT PDF to disk, which the real system never does, so delete the output afterwards.

Run it from inside backend/ (it uses the same .env, databases and blob store as the server):

  1. Before the key is released  (the board still holds the key, locked under BOARD_MASTER_KEY):
         python demo_decrypt.py center_001
         python demo_decrypt.py center_001 --preview        # also renders each page to a PNG

  2. After release  (the board destroyed its copy; only the printer's private key can open the wrapped key
     that is on the chain, exactly like the printer agent does):
         python demo_decrypt.py center_001 --private-key printer_private.pem

     No chain running? Give the wrapped key yourself:
         python demo_decrypt.py center_001 --private-key printer_private.pem --wrapped-hex <hex>

Options:  --out FILE.pdf   where to write the PDF (default: <center_id>.pdf)
          --preview        render pages to PNG files in <center_id>_pages/ (needs pypdfium2)
          --answers        (board mode only) also print the answer key for this variant
"""
import argparse
import hashlib
import sys
from pathlib import Path



def _find_backend():
    """The folder holding db.py / keystore.py: next to this script, in a 'backend' folder beside or below it,
    or in the folder you ran the command from."""
    here = Path(__file__).resolve().parent
    for base in (here, Path.cwd()):
        for cand in (base, base / "backend", base.parent / "backend"):
            if (cand / "keystore.py").exists() and (cand / "db.py").exists():
                return cand
    return None


_BACKEND = _find_backend()
if _BACKEND is None:
    sys.exit("ERROR: could not find the backend folder (the one with db.py and keystore.py). "
             "Put demo_decrypt.py inside it, or run the command from the project folder that contains it.")
sys.path.insert(0, str(_BACKEND))

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def die(msg: str):
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(1)


def unwrap_key(private_pem: bytes, wrapped: bytes, passphrase=None) -> bytes:
    """Reverse of keywrap.wrap_key: RSA-OAEP with SHA-256."""
    try:
        priv = serialization.load_pem_private_key(private_pem, password=passphrase)
        return priv.decrypt(
            wrapped,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
    except Exception as exc:
        die(f"Could not unwrap the key with that private key ({type(exc).__name__}). "
            "Is it the private key of the printer enrolled for this center?")


def main():
    ap = argparse.ArgumentParser(description="TEST ONLY: decrypt a center's paper blob to a PDF.")
    ap.add_argument("center_id")
    ap.add_argument("--out")
    ap.add_argument("--private-key", help="printer RSA private key (PEM) for a key that was already released")
    ap.add_argument("--passphrase", help="passphrase of the private key, if it has one")
    ap.add_argument("--wrapped-hex", help="the wrapped key as hex (skips reading it from the chain)")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--answers", action="store_true")
    a = ap.parse_args()

    import db
    import keystore
    from storage import get_blob_store

    import config
    print(f"backend: {_BACKEND}\ndata:    {config.DATA_DIR}")
    record = db.get_center_record(a.center_id)
    if record is None:
        die(f"Unknown center: {a.center_id}. Known: {', '.join(sorted(db.list_all_centers())) or 'none'}")

    blob = get_blob_store().get(record["blob_object_key"])
    if hashlib.sha256(blob).hexdigest() != record["blob_hash"]:
        die("The stored blob does not match its recorded hash: it was modified.")
    print(f"blob: {len(blob):,} bytes, SHA-256 matches the record ({record['blob_hash'][:16]}...)")

    # ---- get the AES key ------------------------------------------------------------------------
    if a.private_key:
        wrapped = None
        if a.wrapped_hex:
            wrapped = bytes.fromhex(a.wrapped_hex.strip())
        else:
            try:
                from web3_client import PaperCustodyClient
                client = PaperCustodyClient()
                onchain = client.get_blob_hash(a.center_id).hex()
                if onchain != record["blob_hash"]:
                    die("The blob hash on the chain differs from the stored blob's hash.")
                print("on-chain blob hash matches")
                wrapped = client.get_key(a.center_id)
            except SystemExit:
                raise
            except Exception as exc:
                die(f"Could not read the wrapped key from the chain ({exc}). Is the node running and the key "
                    "released? Or pass --wrapped-hex.")
        if not wrapped:
            die("The chain has no wrapped key for this center yet (not released).")
        key = unwrap_key(Path(a.private_key).read_bytes(), wrapped,
                         a.passphrase.encode() if a.passphrase else None)
        print("AES key unwrapped with the printer's private key (as the printer agent does)")
    else:
        if a.center_id not in keystore.centers_with_keys():
            die("The board no longer holds this center's key (it is destroyed once the key is released). "
                "Use --private-key <printer private key> to open the key that is on the chain instead.")
        try:
            key = keystore.get_key(a.center_id)
        except keystore.KeyStoreError as exc:
            die(str(exc))
        print("AES key read from the board's key store (BOARD_MASTER_KEY)")

    # ---- decrypt --------------------------------------------------------------------------------
    try:
        pdf = AESGCM(key).decrypt(blob[:12], blob[12:], None)
    except InvalidTag:
        die("Decryption failed: wrong key, or the blob was tampered with.")
    if not pdf.startswith(b"%PDF"):
        die("Decrypted, but the result is not a PDF.")

    out = Path(a.out or f"{a.center_id}.pdf")
    out.write_bytes(pdf)
    print(f"wrote {out} ({len(pdf):,} bytes), variant {record['variant_id']}  <- PLAINTEXT, delete when done")

    pages = None
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf)
        pages = len(doc)
        print(f"{pages} pages")
        if a.preview:
            folder = Path(f"{a.center_id}_pages")
            folder.mkdir(exist_ok=True)
            for i in range(pages):
                doc[i].render(scale=1.3).to_pil().save(folder / f"page_{i + 1:03d}.png")
            print(f"wrote {pages} page images to {folder}/")
    except ImportError:
        print("(install pypdfium2 to count pages and use --preview)")

    if a.answers:
        if a.private_key:
            print("(answers live in the board's key store; run without --private-key to show them)")
        else:
            try:
                answers = keystore.get_answers(record["variant_id"])
                print("answer key (question id -> option number):",
                      ", ".join(f"{x['id']}:{x['correct_index'] + 1}" for x in answers[:30]),
                      "..." if len(answers) > 30 else "")
            except keystore.KeyStoreError as exc:
                print("answers unavailable:", exc)


if __name__ == "__main__":
    main()