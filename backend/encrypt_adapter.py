"""
encrypt_adapter.py

Small self-contained copy of the decrypt logic from encryption/encrypt.py.
Kept separate (rather than importing across the ../encryption/ folder) so
the backend doesn't depend on sys.path tricks to reach a sibling directory --
simpler to deploy as its own service later.
"""

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def decrypt_pdf(blob: bytes, key: bytes) -> bytes:
    nonce = blob[:12]
    ciphertext = blob[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)  # raises InvalidTag if tampered/wrong key
