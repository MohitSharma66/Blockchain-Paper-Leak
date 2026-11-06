"""
Device enrollment fingerprint.

A center device creates two things that identify it: a chain address (the only account allowed to log events for the
center, and the one that signs its sheet batches) and an RSA key (what the exam's AES key gets wrapped to).
The fingerprint is a short digest over BOTH, written in groups of four so a person can read it aloud:

    sha256( DER(RSA public key) | "|" | lower-case address )  ->  first 20 hex characters

Swapping either the key or the address in an enrollment file changes it. The board never shows it until the
enrollment is confirmed: the operator has to type the value the DEVICE shows, so it has to come from a second
channel and cannot be copied from the board screen.

fingerprint() must stay identical to the one in printer-agent/printer_agent.py.
"""
import hashlib
import hmac

from cryptography.hazmat.primitives import serialization

import keywrap


def fingerprint(public_key_pem, address: str) -> str:
    """Raises ValueError for an invalid / too small / non-RSA key (same checks as key wrapping)."""
    der = keywrap.load_public_key(public_key_pem).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashlib.sha256(der + b"|" + address.lower().encode()).hexdigest()[:20].upper()
    return " ".join(h[i:i + 4] for i in range(0, 20, 4))


def normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


def matches(typed: str, expected: str) -> bool:
    return hmac.compare_digest(normalize(typed), normalize(expected))
