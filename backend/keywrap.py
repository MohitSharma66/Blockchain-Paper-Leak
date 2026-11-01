"""
keywrap.py

Wrapping an AES key to a printer's RSA public key (RSA-OAEP, SHA-256), and the
short fingerprint a human compares during enrollment.

Only the printer's private key can unwrap the result, so the wrapped key can sit on a
public chain: every other center, and anyone reading the chain, just sees ciphertext.
"""

import hashlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

MIN_RSA_BITS = 2048


def _oaep():
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def load_public_key(pem) -> rsa.RSAPublicKey:
    """Parse and validate a printer public key. Raises ValueError with a readable message."""
    if isinstance(pem, str):
        pem = pem.encode()
    try:
        key = serialization.load_pem_public_key(pem)
    except Exception:
        raise ValueError("Public key is not valid PEM.")
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("Printer public key must be an RSA key.")
    if key.key_size < MIN_RSA_BITS:
        raise ValueError(f"RSA key must be at least {MIN_RSA_BITS} bits.")
    return key


def fingerprint(pem) -> str:
    """20 hex chars of SHA-256 over the DER public key, grouped like 'A1B2 C3D4 E5F6 0718 92AB'."""
    der = load_public_key(pem).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    h = hashlib.sha256(der).hexdigest()[:20].upper()
    return " ".join(h[i:i + 4] for i in range(0, 20, 4))


def wrap_key(pem, aes_key: bytes) -> bytes:
    return load_public_key(pem).encrypt(aes_key, _oaep())
