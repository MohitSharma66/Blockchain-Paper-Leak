"""
encrypt.py

Encrypts each variant PDF into a per-center blob using AES-GCM.

- Each center gets its own randomly generated key.
- AES-GCM bundles an authentication tag, so any tampering with the blob
  causes decryption to fail loudly rather than silently returning garbage.
- The nonce (a random value required by AES-GCM) is prepended to the
  ciphertext so it travels with the blob -- it's not secret, just needed.

Usage:
    python encrypt.py --variant variants/variant_1.pdf --center center_001
"""

import os
import json
import argparse
import hashlib
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BLOBS_DIR = Path(__file__).parent / "blobs"
KEYS_DIR = Path(__file__).parent / "keys_store"


def generate_key() -> bytes:
    """AES-256 key -- 32 random bytes."""
    return AESGCM.generate_key(bit_length=256)


def encrypt_pdf(pdf_bytes: bytes, key: bytes) -> bytes:
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce, standard for GCM
    ciphertext = aesgcm.encrypt(nonce, pdf_bytes, None)
    return nonce + ciphertext  # nonce prepended so it travels with the blob


def decrypt_pdf(blob: bytes, key: bytes) -> bytes:
    nonce = blob[:12]
    ciphertext = blob[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)  # raises if tampered/wrong key


def compute_blob_hash(blob: bytes) -> str:
    """This is what gets registered on-chain via registerPaper()."""
    return hashlib.sha256(blob).hexdigest()


def encrypt_for_center(variant_pdf_path: Path, center_id: str) -> dict:
    """
    Encrypts one variant PDF for one center. Writes:
      - blobs/{center_id}.enc          the encrypted blob (stored off-chain)
      - keys_store/{center_id}.key     the raw key (kept separate from blobs
                                        -- see note below on real deployments)
    Returns metadata you'd feed into registerPaper() on the smart contract.
    """
    BLOBS_DIR.mkdir(exist_ok=True)
    KEYS_DIR.mkdir(exist_ok=True)

    pdf_bytes = variant_pdf_path.read_bytes()
    key = generate_key()
    blob = encrypt_pdf(pdf_bytes, key)
    blob_hash = compute_blob_hash(blob)

    blob_path = BLOBS_DIR / f"{center_id}.enc"
    key_path = KEYS_DIR / f"{center_id}.key"

    blob_path.write_bytes(blob)
    key_path.write_bytes(key)

    metadata = {
        "center_id": center_id,
        "source_pdf": str(variant_pdf_path),
        "blob_path": str(blob_path),
        "blob_hash": blob_hash,
        # key_path is NOT something you'd put on-chain in a real deployment --
        # only the key itself (or a wrapped/derived version of it) goes into
        # registerPaper(). Kept here so this demo can locate it later.
        "key_path": str(key_path),
    }

    return metadata


def verify_roundtrip(center_id: str) -> bytes:
    """
    Sanity check: decrypt what we just encrypted and confirm we get the
    exact original PDF bytes back. Used to prove the "will it come out
    unscrambled" guarantee, not a leap of faith.
    """
    blob_path = BLOBS_DIR / f"{center_id}.enc"
    key_path = KEYS_DIR / f"{center_id}.key"

    blob = blob_path.read_bytes()
    key = key_path.read_bytes()

    decrypted = decrypt_pdf(blob, key)
    return decrypted  # caller compares this against the original file


def main():
    parser = argparse.ArgumentParser(description="Encrypt a variant PDF for a specific center.")
    parser.add_argument("--variant", required=True, help="Path to the rendered variant PDF")
    parser.add_argument("--center", required=True, help="Center ID, e.g. center_001")
    args = parser.parse_args()

    metadata = encrypt_for_center(Path(args.variant), args.center)

    print(f"Encrypted {args.variant} for {args.center}")
    print(f"  blob:      {metadata['blob_path']}")
    print(f"  blob_hash: {metadata['blob_hash']}")
    print(f"  key:       {metadata['key_path']}  (kept separate -- not on-chain as-is)")

    # Save registration metadata so it's easy to feed into registerPaper() later
    registry_path = BLOBS_DIR / "registration_metadata.jsonl"
    with open(registry_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(metadata) + "\n")


if __name__ == "__main__":
    main()
