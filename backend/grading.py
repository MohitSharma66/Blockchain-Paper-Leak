"""
Grading of online answer sheets.

A center's exam server sends its submitted sheets to the board, signed with the center device's key.
The board then
  1. checks the signature belongs to the device enrolled for that center,
  2. recomputes every sheet's SHA-256 and checks that it is part of the Merkle root the device wrote on-chain
     after the exam (custody stage ANSWERS_ROOT:<number of sheets>), using the sheet's Merkle proof. The root is
     one transaction for the whole center; the proof shows a single sheet is inside it. (Older exams anchored one
     hash per candidate, stage ANSWERS:<candidate>; that is still understood.)
  3. scores only sheets that match, against the answer key the board has held all along.
A sheet that is missing on-chain or whose hash differs is stored as UNVERIFIED and gets no score.

Marking: +4 correct, -1 wrong, 0 unanswered (the NEET scheme); override with GRADE_CORRECT / GRADE_WRONG.
"""
import hashlib
import json

from eth_account import Account
from eth_account.messages import encode_defunct

import config

ANSWER_STAGE = "ANSWERS:"
ROOT_STAGE = "ANSWERS_ROOT:"


def marks() -> tuple[int, int]:
    def num(name, default):
        raw = config.env(name)
        try:
            return int(raw) if raw else default
        except ValueError:
            return default
    return num("GRADE_CORRECT", 4), num("GRADE_WRONG", -1)


def sheet_hash(sheet: dict) -> str:
    canonical = json.dumps(sheet, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def batch_message(center_id: str, hashes: list[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
    return f"NEET-SHEETS|{center_id}|{digest}"


def signer_of(center_id: str, hashes: list[str], signature: str) -> str:
    return Account.recover_message(encode_defunct(text=batch_message(center_id, hashes)), signature=signature)


def score(answers: dict, answer_key: list) -> dict:
    """answers: {question id (str): chosen option key or None}; answer_key: [{id, correct_index}]."""
    right, wrong_n, blank = 0, 0, 0
    correct_mark, wrong_mark = marks()
    for item in answer_key:
        chosen = answers.get(str(item["id"]))
        if chosen is None:
            blank += 1
        elif chosen == item["correct_index"]:
            right += 1
        else:
            wrong_n += 1
    return {"correct": right, "wrong": wrong_n, "blank": blank,
            "score": right * correct_mark + wrong_n * wrong_mark}


def anchored_hashes(log: list, device_address: str) -> dict:
    """{candidate id: sha256 hex} from the on-chain custody log, only entries written by the center device.
    The first entry per candidate wins; a later different one is ignored, so a re-submission cannot replace it."""
    out: dict = {}
    for entry in log:
        stage, actor, _ts, digest = entry[0], entry[1], entry[2], entry[3]
        if not stage.startswith(ANSWER_STAGE) or actor.lower() != device_address.lower():
            continue
        out.setdefault(stage[len(ANSWER_STAGE):], bytes(digest).hex())
    return out


# ---------------------------------------------------------------------
# Merkle tree over the sheet hashes of one center.
# Leaves are the sheet SHA-256 values sorted ascending. leaf = sha256(0x00 | hash), node = sha256(0x01 | left | right);
# an odd node at the end of a level is carried up unchanged (never duplicated). Must match exam-server/exam_server.py.
# ---------------------------------------------------------------------

def _leaf(h_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(h_hex)).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _levels(hashes: list[str]) -> list[list[bytes]]:
    level = [_leaf(h) for h in sorted(set(hashes))]
    if not level:
        raise ValueError("No sheets.")
    levels = [level]
    while len(level) > 1:
        level = [_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i] for i in range(0, len(level), 2)]
        levels.append(level)
    return levels


def merkle_root(hashes: list[str]) -> str:
    return _levels(hashes)[-1][0].hex()


def merkle_proofs(hashes: list[str]) -> dict:
    """{sheet hash: [[side, sibling hex], ...]} where side says which side the sibling is on ("L" or "R")."""
    ordered = sorted(set(hashes))
    levels = _levels(ordered)
    out = {}
    for i, h in enumerate(ordered):
        idx, proof = i, []
        for level in levels[:-1]:
            if idx % 2 == 0:
                if idx + 1 < len(level):
                    proof.append(["R", level[idx + 1].hex()])
            else:
                proof.append(["L", level[idx - 1].hex()])
            idx //= 2
        out[h] = proof
    return out


def verify_proof(sheet_hash_hex: str, proof: list, root_hex: str) -> bool:
    try:
        cur = _leaf(sheet_hash_hex)
        for side, sib in proof:
            cur = _node(cur, bytes.fromhex(sib)) if side == "R" else _node(bytes.fromhex(sib), cur)
        return cur.hex() == root_hex.lower()
    except (ValueError, TypeError):
        return False


def anchored_root(log: list, device_address: str):
    """(root hex, sheet count) of the first ANSWERS_ROOT entry written by the center device, or None."""
    for entry in log:
        stage, actor, digest = entry[0], entry[1], entry[3]
        if stage.startswith(ROOT_STAGE) and actor.lower() == device_address.lower():
            try:
                count = int(stage[len(ROOT_STAGE):])
            except ValueError:
                count = None
            return bytes(digest).hex(), count
    return None
