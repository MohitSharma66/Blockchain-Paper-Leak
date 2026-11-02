"""
db.py

The records database (board.db, SQLite). One row per center describing what
exists and where it is. Nothing secret lives here: the blob lives in the bucket
(blob_object_key points at it) and the AES key lives in keys.db (keystore.py).

centers
    center_id        primary key, e.g. center_001
    variant_id       unique, e.g. variant_1
    fingerprint      SHA-256 of the variant's option ordering (leak attribution)
    blob_hash        SHA-256 of the encrypted blob (also registered on-chain)
    blob_object_key  where the blob sits in R2 / the local blob folder
    exam_start_time  unix time, filled in when the center is registered
    printer_address  chain address of the center's printer (set at enrollment)
    printer_public_key / printer_fingerprint   its RSA public key + short fingerprint
    created_at

Plain SQL on purpose: moving to Postgres later is mostly a connection change.
"""

import sqlite3
import time
from contextlib import closing
from typing import Optional

import config

DB_PATH = config.DATA_DIR / "board.db"

_UPDATABLE = {"exam_start_time", "printer_address", "printer_public_key", "printer_fingerprint"}

# Columns added after the first release; added to older databases on startup.
_EXTRA_COLUMNS = {
    "printer_address": "TEXT",
    "printer_public_key": "TEXT",
    "printer_fingerprint": "TEXT",
    "online_blob_hash": "TEXT",      # encrypted question payload for online exams (same AES key as the PDF)
    "online_object_key": "TEXT",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS centers (
                center_id       TEXT PRIMARY KEY,
                variant_id      TEXT NOT NULL UNIQUE,
                fingerprint     TEXT NOT NULL,
                blob_hash       TEXT NOT NULL,
                blob_object_key TEXT NOT NULL,
                exam_start_time INTEGER,
                created_at      INTEGER NOT NULL
            )
            """
        )


def _migrate() -> None:
    with closing(_connect()) as conn, conn:
        have = {row["name"] for row in conn.execute("PRAGMA table_info(centers)")}
        for name, sql_type in _EXTRA_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE centers ADD COLUMN {name} {sql_type}")


_init()
_migrate()


def upsert_center_record(
    center_id: str,
    variant_id: str,
    fingerprint: str,
    blob_hash: str,
    blob_object_key: str,
    exam_start_time: Optional[int] = None,
    online_blob_hash: Optional[str] = None,
    online_object_key: Optional[str] = None,
) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO centers
                (center_id, variant_id, fingerprint, blob_hash, blob_object_key, exam_start_time, created_at,
                 online_blob_hash, online_object_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(center_id) DO UPDATE SET
                variant_id = excluded.variant_id,
                fingerprint = excluded.fingerprint,
                blob_hash = excluded.blob_hash,
                blob_object_key = excluded.blob_object_key,
                exam_start_time = excluded.exam_start_time,
                online_blob_hash = excluded.online_blob_hash,
                online_object_key = excluded.online_object_key
            """,
            (center_id, variant_id, fingerprint, blob_hash, blob_object_key, exam_start_time, int(time.time()),
             online_blob_hash, online_object_key),
        )


def update_center_record(center_id: str, **fields) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Cannot update field(s): {', '.join(sorted(bad))}")
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with closing(_connect()) as conn, conn:
        conn.execute(f"UPDATE centers SET {assignments} WHERE center_id = ?", (*fields.values(), center_id))


def get_center_record(center_id: str) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM centers WHERE center_id = ?", (center_id,)).fetchone()
    return dict(row) if row else None


def get_center_id_by_variant(variant_id: str) -> Optional[str]:
    """Reverse lookup for leak detection: matched variant -> which center got it."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT center_id FROM centers WHERE variant_id = ?", (variant_id,)).fetchone()
    return row["center_id"] if row else None


def get_variant_by_fingerprint(fingerprint: str) -> Optional[dict]:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT center_id, variant_id FROM centers WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
    return dict(row) if row else None


def list_all_centers() -> dict:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM centers ORDER BY center_id").fetchall()
    return {r["center_id"]: dict(r) for r in rows}


def delete_center(center_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM centers WHERE center_id = ?", (center_id,))
