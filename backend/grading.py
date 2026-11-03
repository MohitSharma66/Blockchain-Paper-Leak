"""
Grading of online answer sheets.

A center's exam server sends its submitted sheets to the board, signed with the center device's key.
The board then
  1. checks the signature belongs to the device enrolled for that center,
  2. recomputes every sheet's SHA-256 and compares it with the hash the device wrote on-chain
     (custody stage ANSWERS:<candidate>) when the candidate submitted,
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
