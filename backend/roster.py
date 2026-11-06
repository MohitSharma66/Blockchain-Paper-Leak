"""
Candidate rosters for online exams.

The board (not the center) decides who may sit an online exam. For each center it creates a roster:
candidate IDs and one-time access codes. The plaintext codes are returned ONCE (for admit cards) and are not
stored anywhere; the board and the centers keep only salted, slow hashes of them.

The roster file that goes to the center holds {candidate id: code hash}. Its SHA-256 is written on-chain by the
board (custody stage ROSTER) at registration, so a center cannot swap in its own candidate list: its exam server
checks the downloaded file against that anchored hash before accepting a single login.

code_hash() and roster_hash() must stay identical to the copies in exam-server/exam_server.py.
"""
import hashlib
import json
import re
import secrets

KDF_ITERATIONS = 20_000          # PBKDF2: ~6 ms per login; makes offline guessing of a stolen roster impractical
KDF_NAME = f"pbkdf2-sha256-{KDF_ITERATIONS}"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I
CODE_LENGTH = 8
MAX_CANDIDATES = 20_000
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def code_hash(salt: str, candidate_id: str, code: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", f"{candidate_id}|{code.strip().upper()}".encode(),
                               salt.encode(), KDF_ITERATIONS).hex()


def roster_file(center_id: str, salt: str, entries: dict) -> dict:
    return {"center": center_id, "kdf": KDF_NAME, "salt": salt, "candidates": dict(sorted(entries.items()))}


def roster_hash(rf: dict) -> str:
    return hashlib.sha256(json.dumps(rf, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(center_id: str, count: int | None, candidates: list[dict] | None) -> tuple[dict, list[dict]]:
    """
    Returns (roster file, slips). `slips` = [{candidate_id, name, code}] with the plaintext codes.
    Give either `count` (IDs like 013-0001) or `candidates` ([{candidate_id, name}], e.g. application numbers).
    """
    if candidates:
        people = []
        seen = set()
        for c in candidates:
            cid = str(c.get("candidate_id", "")).strip()
            if not _ID_RE.match(cid):
                raise ValueError(f"Invalid candidate ID {cid!r}: use 1-32 letters, digits, '.', '_' or '-'.")
            if cid in seen:
                raise ValueError(f"Candidate ID {cid} appears twice.")
            seen.add(cid)
            people.append((cid, str(c.get("name") or "").strip()))
    elif count:
        num = center_id.split("_")[-1]
        people = [(f"{num}-{i:04d}", "") for i in range(1, count + 1)]
    else:
        raise ValueError("Give a number of candidates or a list of candidates.")
    if not 1 <= len(people) <= MAX_CANDIDATES:
        raise ValueError(f"A roster needs between 1 and {MAX_CANDIDATES} candidates.")

    salt = secrets.token_hex(8)
    entries, slips = {}, []
    for cid, name in people:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        entries[cid] = code_hash(salt, cid, code)
        slips.append({"candidate_id": cid, "name": name, "code": code})
    return roster_file(center_id, salt, entries), slips
