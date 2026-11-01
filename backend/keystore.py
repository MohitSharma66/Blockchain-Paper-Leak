"""
keystore.py

A separate SQLite database (keys.db, NOT board.db) holding the secrets:

  aes_keys      one AES-256 key per center, stored ONLY as ciphertext
  answer_keys   the correct answers per variant, also ciphertext

Both are encrypted with AES-GCM under a master key that lives outside every
database and the bucket: the BOARD_MASTER_KEY environment variable (64 hex
characters). Stealing the bucket, board.db or keys.db is therefore not enough:
an attacker also needs the master key. In production the master key would sit
in a KMS/HSM instead of an environment variable.

Each row is bound to its owner (center_id / variant_id) as AES-GCM associated
data, so a ciphertext can't be copied onto another row.

Destroying a key (destroy_key) erases the ciphertext and keeps a tombstone
row with the time it was destroyed.

Generate a master key once, keep it safe and back it up offline:
    python -c "import secrets; print(secrets.token_hex(32))"
Losing it makes every stored key unreadable.
"""

import json
import os
import sqlite3
import time
from contextlib import closing

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import config

KEYS_DB_PATH = config.DATA_DIR / "keys.db"


class KeyStoreError(Exception):
    """Master key missing/wrong, or a key is absent/destroyed."""


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(KEYS_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS aes_keys (
                center_id     TEXT PRIMARY KEY,
                encrypted_key BLOB,
                created_at    INTEGER NOT NULL,
                destroyed_at  INTEGER
            );
            CREATE TABLE IF NOT EXISTS answer_keys (
                variant_id        TEXT PRIMARY KEY,
                encrypted_answers BLOB NOT NULL,
                created_at        INTEGER NOT NULL
            );
            """
        )
    try:
        os.chmod(KEYS_DB_PATH, 0o600)  # best effort; ignored on Windows
    except OSError:
        pass


_init()


# ---------------------------------------------------------------------
# Master key + sealing
# ---------------------------------------------------------------------

def master_key() -> bytes:
    raw = config.env("BOARD_MASTER_KEY")
    if not raw:
        raise KeyStoreError(
            "BOARD_MASTER_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"` "
            "and put it in backend/.env."
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise KeyStoreError("BOARD_MASTER_KEY must be 64 hexadecimal characters.")
    if len(key) != 32:
        raise KeyStoreError("BOARD_MASTER_KEY must be exactly 32 bytes (64 hex characters).")
    return key


def _seal(plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(master_key()).encrypt(nonce, plaintext, aad)


def _open(sealed: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(master_key()).decrypt(sealed[:12], sealed[12:], aad)
    except InvalidTag:
        raise KeyStoreError(
            "Could not decrypt a stored secret: wrong BOARD_MASTER_KEY, or the stored row was modified."
        )


def _aes_aad(center_id: str) -> bytes:
    return b"aes-key:" + center_id.encode()


def _answers_aad(variant_id: str) -> bytes:
    return b"answers:" + variant_id.encode()


# ---------------------------------------------------------------------
# AES keys (one per center)
# ---------------------------------------------------------------------

def store_key(center_id: str, aes_key: bytes) -> None:
    sealed = _seal(aes_key, _aes_aad(center_id))
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO aes_keys (center_id, encrypted_key, created_at) VALUES (?, ?, ?)",
                (center_id, sealed, int(time.time())),
            )
    except sqlite3.IntegrityError:
        raise KeyStoreError(f"A key for {center_id} already exists; refusing to overwrite it.")


def get_key(center_id: str) -> bytes:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT encrypted_key, destroyed_at FROM aes_keys WHERE center_id = ?", (center_id,)
        ).fetchone()
    if row is None:
        raise KeyStoreError(f"No key stored for {center_id}.")
    if row["encrypted_key"] is None:
        raise KeyStoreError(f"The key for {center_id} was destroyed at {row['destroyed_at']}.")
    return _open(row["encrypted_key"], _aes_aad(center_id))


def centers_with_keys() -> set:
    """Centers whose key still exists (not destroyed). Needs no master key."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT center_id FROM aes_keys WHERE encrypted_key IS NOT NULL").fetchall()
    return {r["center_id"] for r in rows}


def destroy_key(center_id: str) -> bool:
    """Erase the ciphertext, keep a tombstone. Returns True if a live key was destroyed."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE aes_keys SET encrypted_key = NULL, destroyed_at = ? "
            "WHERE center_id = ? AND encrypted_key IS NOT NULL",
            (int(time.time()), center_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------
# Answer keys (one per variant)
# ---------------------------------------------------------------------

def store_answers(variant_id: str, answers: list) -> None:
    sealed = _seal(json.dumps(answers).encode(), _answers_aad(variant_id))
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO answer_keys (variant_id, encrypted_answers, created_at) VALUES (?, ?, ?)",
                (variant_id, sealed, int(time.time())),
            )
    except sqlite3.IntegrityError:
        raise KeyStoreError(f"Answers for {variant_id} already exist; refusing to overwrite them.")


def get_answers(variant_id: str) -> list:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT encrypted_answers FROM answer_keys WHERE variant_id = ?", (variant_id,)
        ).fetchone()
    if row is None:
        raise KeyStoreError(f"No answers stored for {variant_id}.")
    return json.loads(_open(row["encrypted_answers"], _answers_aad(variant_id)))


# ---------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------

def purge(center_id: str, variant_id: str) -> None:
    """Remove rows entirely (used to roll back a half-finished generate batch)."""
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM aes_keys WHERE center_id = ?", (center_id,))
        conn.execute("DELETE FROM answer_keys WHERE variant_id = ?", (variant_id,))


def require_master_key() -> None:
    master_key()


def status() -> dict:
    try:
        master_key()
        configured, error = True, None
    except KeyStoreError as e:
        configured, error = False, str(e)
    with closing(_connect()) as conn:
        live = conn.execute("SELECT COUNT(*) FROM aes_keys WHERE encrypted_key IS NOT NULL").fetchone()[0]
        destroyed = conn.execute("SELECT COUNT(*) FROM aes_keys WHERE encrypted_key IS NULL").fetchone()[0]
        sample = conn.execute(
            "SELECT center_id, encrypted_key FROM aes_keys WHERE encrypted_key IS NOT NULL LIMIT 1"
        ).fetchone()

    # Does the configured master key actually open what is already stored?
    # None = nothing to test against yet (no keys stored).
    matches = None
    if configured and sample is not None:
        try:
            _open(sample["encrypted_key"], _aes_aad(sample["center_id"]))
            matches = True
        except KeyStoreError:
            matches = False
            error = "BOARD_MASTER_KEY does not match the keys already stored (wrong key?)."

    return {"master_key_configured": configured, "master_key_matches_stored": matches, "error": error,
            "database": str(KEYS_DB_PATH), "live_keys": live, "destroyed_keys": destroyed}
