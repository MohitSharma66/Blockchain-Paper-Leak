#!/usr/bin/env python3
"""
NEET Integrity Chain - center exam server (online mode).

One of these runs per exam center, on that center's own LAN. It is the online counterpart of the
printer agent and uses the SAME device identity (same folder, same keys):

    1. waits until the board has released the key on-chain (the time-lock),
    2. downloads the board's candidate list and checks it against the hash the board anchored on-chain
       (custody stage ROSTER): the center cannot add or swap candidates,
    3. downloads the encrypted question payload, checks it against the hash the board anchored
       on-chain (custody stage ONLINE_PAYLOAD), unwraps the AES key with this device's private
       RSA key and decrypts the paper IN MEMORY ONLY,
    4. serves it to candidates on the center's network, shuffled differently per candidate,
       with a server-side timer and saved answers,
    5. when a candidate submits (or time runs out), builds and saves the answer sheet and its SHA-256.
       After the exam ends, ONE transaction records the Merkle root of all sheets on-chain (custody stage
       ANSWERS_ROOT:<number of sheets>). Each candidate's receipt has their sheet hash and, once the root is
       recorded, a Merkle proof that the sheet is inside it. The answers themselves stay on the center's disk;
       the chain proves later that no sheet was added, altered or dropped.

The server never holds the answer key. Grading is done by the board afterwards.

Commands
    python exam_server.py run    --center center_001 --rpc-url http://127.0.0.1:8546
    python exam_server.py roster --center center_001 --count 30      # DEMO ONLY: a center-made list, used only
                                                                      # with `run --allow-local-roster`

Candidate page: http://<this machine>:8080/
"""
import argparse
import hashlib
import hmac
import json
import os
import random
import secrets
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
for p in (HERE, HERE.parent / "printer-agent"):          # printer_agent.py sits next to us in Docker
    if p.exists():
        sys.path.insert(0, str(p))

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account.messages import encode_defunct

import printer_agent as pa   # device identity, chain connection, signing: shared with the printer



def pa_batch_message(center_id: str, hashes: list) -> str:
    """Must match backend/grading.py batch_message()."""
    digest = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
    return f"NEET-SHEETS|{center_id}|{digest}"


DEFAULT_DURATION_MIN = 180
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I
MAX_BAD_LOGINS = 5
LOCKOUT_SECONDS = 30


# ---------------------------------------------------------------------
# roster
# ---------------------------------------------------------------------

KDF_ITERATIONS = 20_000
KDF_NAME = f"pbkdf2-sha256-{KDF_ITERATIONS}"


def _code_hash(salt: str, candidate_id: str, code: str, kdf: str | None = None) -> str:
    """Must match backend/roster.py code_hash(). `kdf` comes from the roster file; none = the old plain SHA-256."""
    if kdf and kdf.startswith("pbkdf2-sha256-"):
        return hashlib.pbkdf2_hmac("sha256", f"{candidate_id}|{code.strip().upper()}".encode(), salt.encode(),
                                   int(kdf.rsplit("-", 1)[1])).hex()
    return hashlib.sha256(f"{salt}|{candidate_id}|{code.strip().upper()}".encode()).hexdigest()


def roster_hash(rf: dict) -> str:
    """Must match backend/roster.py roster_hash()."""
    return hashlib.sha256(json.dumps(rf, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Merkle tree over the sheet hashes. Must match backend/grading.py.
def _leaf(h_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(h_hex)).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _levels(hashes: list) -> list:
    level = [_leaf(h) for h in sorted(set(hashes))]
    levels = [level]
    while len(level) > 1:
        level = [_node(level[i], level[i + 1]) if i + 1 < len(level) else level[i] for i in range(0, len(level), 2)]
        levels.append(level)
    return levels


def merkle_root(hashes: list) -> str:
    return _levels(hashes)[-1][0].hex()


def merkle_proofs(hashes: list) -> dict:
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


def roster_path(args) -> Path:
    return pa.device_dir(args) / f"roster_{args.center}.json"


def cmd_roster(args):
    path = roster_path(args)
    if path.exists() and not args.force:
        sys.exit(f"{path} already exists. Use --force to replace it (old access codes stop working).")
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(8)
    num = args.center.split("_")[-1]
    entries, slips = {}, []
    for i in range(1, args.count + 1):
        cid = f"{num}-{i:04d}"
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        entries[cid] = _code_hash(salt, cid, code, KDF_NAME)
        slips.append((cid, code))
    pa.private_file(path, json.dumps({"center": args.center, "kdf": KDF_NAME, "salt": salt, "candidates": entries},
                                     indent=2).encode())
    print(f"DEMO roster for {args.center} (made here, NOT by the board): {args.count} candidates -> {path}")
    print("It is used only if you start the server with --allow-local-roster and the board has not anchored a list.")
    print("Hand each candidate their slip. The codes are shown ONLY now; the file keeps salted hashes.\n")
    print(f"{'candidate id':<14} access code")
    for cid, code in slips:
        print(f"{cid:<14} {code}")


# ---------------------------------------------------------------------
# paper personalisation
# ---------------------------------------------------------------------

def personalise(payload: dict, secret: bytes, candidate_id: str) -> list[dict]:
    """
    Per-candidate order, reproducible from (secret, candidate): questions are shuffled inside their
    subject (subjects keep their order), options are shuffled unless the question marks them fixed.
    Returns display-ready questions with a display number; every option keeps its original `key`.
    """
    seed = hmac.new(secret, candidate_id.encode(), hashlib.sha256).digest()
    rng = random.Random(seed)
    groups: list[list[dict]] = []
    last = object()
    for q in payload["questions"]:
        if not groups or q.get("subject") != last:
            groups.append([])
            last = q.get("subject")
        groups[-1].append(q)
    out = []
    for g in groups:
        g = list(g)
        rng.shuffle(g)
        out.extend(g)
    shown = []
    for n, q in enumerate(out, start=1):
        opts = [dict(o) for o in q["options"]]
        if not q.get("fixed_options"):
            rng.shuffle(opts)
        item = {"n": n, "id": q["id"], "subject": q.get("subject"), "options": opts}
        if "question_image" in q:
            item["question_image"] = q["question_image"]
        else:
            item["question_text"] = q["question_text"]
            item["images"] = q.get("images", [])
        shown.append(item)
    return shown


# ---------------------------------------------------------------------
# the exam
# ---------------------------------------------------------------------

class Exam:
    def __init__(self, args, dev, w3, contract):
        self.args, self.dev, self.w3, self.contract = args, dev, w3, contract
        self.center = args.center
        self.lock = threading.RLock()
        self.phase = "waiting"
        self.message = "Waiting for the board to register this center."
        self.payload: dict | None = None
        self.starts_at = 0
        self.ends_at = 0
        self.duration = int((args.duration_minutes or DEFAULT_DURATION_MIN) * 60)   # the board's value replaces it
        self._mode_checked = False
        self._ref = (0, time.time())

        self.data_dir = Path(args.data_dir).resolve()
        (self.data_dir / "sheets").mkdir(parents=True, exist_ok=True)
        self.secret = self._load_secret()
        self.roster: dict | None = None                    # the board's candidate list, once verified against the chain
        self.roster_msg = "Waiting for the board's candidate list."
        self.sealed = False                                # set when the Merkle root is being built: no new sheets
        self.root_file = self.data_dir / f"answers_root_{self.center}.json"
        self.root_record = json.loads(self.root_file.read_text()) if self.root_file.exists() else None
        self._last_root_try = 0.0
        self._root_error: str | None = None
        self.sessions: dict[str, str] = {}                 # token -> candidate id
        self.state: dict[str, dict] = {}                   # candidate id -> {answers, submitted, ...}
        self.bad: dict[str, list] = {}                     # candidate id -> [count, locked_until]
        self.pushed_marker = self.data_dir / f"results_sent_{self.center}"
        self.results_sent = self.pushed_marker.exists()
        self._last_push_try = 0.0
        self._push_error: str | None = None
        self._replay_journal()
        self.journal = open(self.data_dir / f"journal_{self.center}.jsonl", "a", encoding="utf-8")

    # ---- setup
    def _load_secret(self) -> bytes:
        p = self.data_dir / "session_secret"
        if p.exists():
            return bytes.fromhex(p.read_text().strip())
        s = secrets.token_bytes(32)
        pa.private_file(p, s.hex().encode())
        return s

    def _ensure_roster(self):
        """
        Get the board's candidate list: the file must hash to the value the board wrote on-chain (ROSTER).
        A copy cached on disk is re-checked against the chain every time the server starts.
        """
        c = self.center
        log = self.contract.functions.getCustodyLog(c).call()
        board = self.contract.functions.examBoard().call()
        anchored = [bytes(e[3]).hex() for e in log if e[0] == "ROSTER" and e[1].lower() == board.lower()]
        p = roster_path(self.args)
        if not anchored:
            if self.args.allow_local_roster and p.exists():
                self.roster = json.loads(p.read_text())
                print("   WARNING: using the center's own roster file (--allow-local-roster). Demo use only.")
                return
            self.roster_msg = "Waiting for the board's candidate list (it is not on the chain yet)."
            return
        want = anchored[0]
        rf = None
        if p.exists():
            try:
                cached = json.loads(p.read_text())
                if roster_hash(cached) == want:
                    rf = cached
            except ValueError:
                pass
        if rf is None:
            try:
                r = requests.get(f"{self.args.api_url}/roster-file/{c}", timeout=30)
                r.raise_for_status()
                rf = r.json()
            except (requests.RequestException, ValueError) as e:
                self.roster_msg = f"Could not download the candidate list from the board yet: {e}"
                return
            if roster_hash(rf) != want:
                self._fail("ABORT: the candidate list does not match the hash the board recorded on-chain.")
                return
            pa.private_file(p, json.dumps(rf, indent=2).encode())
        if rf.get("center") != c or not rf.get("candidates"):
            self._fail("ABORT: the candidate list is not for this center.")
            return
        self.roster = rf
        print(f"   candidate list verified against the on-chain hash ({len(rf['candidates'])} candidates)")

    def _replay_journal(self):
        p = self.data_dir / f"journal_{self.center}.jsonl"
        if not p.exists():
            return
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            st = self._st(e["cand"])
            st["started"] = True
            if e["ev"] == "answer":
                if e["key"] is None:
                    st["answers"].pop(e["qid"], None)
                else:
                    st["answers"][e["qid"]] = e["key"]
            elif e["ev"] == "submit":
                st["submitted"] = e["sheet"]

    def _st(self, cand: str) -> dict:
        return self.state.setdefault(cand, {"answers": {}, "submitted": None, "started": False})

    def _log(self, **e):
        self.journal.write(json.dumps({"t": int(time.time()), **e}) + "\n")
        self.journal.flush()
        os.fsync(self.journal.fileno())

    # ---- time
    def sync_clock(self):
        """Chain time = timestamp of the newest block + wall-clock time since we first saw that block."""
        blk = self.w3.eth.get_block("latest")
        if blk["number"] != getattr(self, "_ref_block", None):
            self._ref_block = blk["number"]
            self._ref = (int(blk["timestamp"]), time.time())

    def now(self) -> int:
        ts, wall = self._ref
        return int(ts + (time.time() - wall))

    # ---- lifecycle (background thread)
    def lifecycle(self):
        me = self.dev["account"].address
        last = None
        while True:
            try:
                self.sync_clock()
                st = pa.get_state(self.contract, self.center)
                if st["registered"] and not self._mode_checked:
                    mode, minutes = pa.exam_mode(self.contract, self.center)
                    if mode == "offline":
                        self._fail(f"ABORT: {self.center} is registered for an OFFLINE (printed) exam. This server "
                                   f"does not run it. Start the printer instead: scripts\\new-center-device.cmd "
                                   f"{self.center} offline")
                        return
                    if minutes:
                        self.duration = minutes * 60          # the board decides how long the exam is
                    self._mode_checked = True
                if st["registered"] and self.roster is None and self.payload is None:
                    self._ensure_roster()
                if self.phase in ("waiting", "loading", "error") and self.payload is None:
                    if not st["registered"]:
                        msg = "Waiting for the board to register this center."
                    elif st["printer"].lower() != me.lower():
                        self._fail(f"The device registered for {self.center} is {st['printer']}, not this server.")
                        return
                    elif not st["released"]:
                        when = time.strftime("%H:%M:%S", time.localtime(st["start"]))
                        msg = f"The paper is locked until the exam starts (chain start time {when})."
                    elif self.roster is None:
                        msg = self.roster_msg if self.phase != "error" else None
                    else:
                        msg = None
                        self._load_paper(st)
                    if msg:
                        self._set("waiting", msg)
                if self.payload is not None:
                    self._tick()
            except Exception as e:                              # chain/network hiccup: keep going
                msg = f"chain not reachable ({e}); retrying"
                if msg != last:
                    print(msg)
                    last = msg
            time.sleep(self.args.poll)

    def _set(self, phase, message):
        with self.lock:
            changed = (phase, message) != (self.phase, self.message)
            self.phase, self.message = phase, message
        if changed:
            print(f"[{phase}] {message}")

    def _fail(self, message):
        self._set("error", message)

    def _load_paper(self, st):
        self._set("loading", "Key released. Fetching and verifying the paper...")
        c = self.center
        try:
            r = requests.get(f"{self.args.api_url}/online-blob/{c}", timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            self._fail(f"Could not download the paper payload: {e}")
            time.sleep(self.args.poll)
            return
        blob = r.content

        # The board anchors the payload hash on-chain when it registers the center.
        log = self.contract.functions.getCustodyLog(c).call()
        board = self.contract.functions.examBoard().call()
        anchored = [bytes(e[3]).hex() for e in log if e[0] == "ONLINE_PAYLOAD" and e[1].lower() == board.lower()]
        if not anchored:
            self._fail("ABORT: the board never anchored an online payload for this center on-chain (print-only paper?).")
            return
        if hashlib.sha256(blob).hexdigest() != anchored[0]:
            self._fail("ABORT: the downloaded payload does not match the hash anchored on-chain.")
            return

        wrapped = bytes(self.contract.functions.getKey(c).call())
        try:
            aes_key = self.dev["rsa"].decrypt(wrapped, pa.oaep())
        except ValueError:
            self._fail("ABORT: the wrapped key was not made for this device's key.")
            return
        try:
            plain = AESGCM(aes_key).decrypt(blob[:12], blob[12:], None)
        except Exception:
            self._fail("ABORT: decryption failed (payload tampered with, or wrong key).")
            return
        finally:
            del aes_key
        payload = json.loads(plain)
        del plain

        with self.lock:
            self.payload = payload
            self.starts_at = int(st["start"])
            self.ends_at = self.starts_at + self.duration
        print(f"   paper verified against the on-chain hash and decrypted in memory ({len(payload['questions'])} questions)")
        self._tick()

    def _tick(self):
        now = self.now()
        if now < self.starts_at:
            self._set("waiting", "Key released; waiting for the start time.")
        elif now < self.ends_at:
            self._set("open", "The exam is open.")
        else:
            self._set("closed", "The exam time is over.")
            self._auto_submit_all()
            self._maybe_push_sheets()

    def _maybe_push_sheets(self):
        """After the exam: record the Merkle root on-chain, then send the sheets (with proofs) to the board."""
        if self.results_sent:
            return
        if not self._maybe_anchor_root():
            return
        now = time.time()
        if now - self._last_push_try < 10:
            return
        self._last_push_try = now
        with self.lock:
            sheets = [st["submitted"] for st in self.state.values() if st["submitted"]]
        if not sheets:
            self.results_sent = True
            self.pushed_marker.write_text("no sheets")
            return
        try:
            msg = pa_batch_message(self.center, [s["sha256"] for s in sheets])
            sig = "0x" + self.dev["account"].sign_message(encode_defunct(text=msg)).signature.hex().removeprefix("0x")
            proofs = (self.root_record or {}).get("proofs", {})
            items = [{"sheet": s["sheet"], "sha256": s["sha256"], "proof": proofs.get(s["sha256"])} for s in sheets]
            r = requests.post(f"{self.args.api_url}/sheets/{self.center}",
                              json={"sheets": items, "signature": sig}, timeout=60)
            if not r.ok:
                raise RuntimeError(f"board answered {r.status_code}: {r.text[:200]}")
            summary = r.json()
        except Exception as e:
            if str(e) != self._push_error:
                print(f"   could not send the sheets to the board yet ({e}); will retry")
                self._push_error = str(e)
            return
        self.results_sent = True
        self.pushed_marker.write_text(json.dumps(summary))
        print(f"   sent {len(sheets)} answer sheets to the board for grading: {summary}")

    def _auto_submit_all(self):
        with self.lock:
            pending = [c for c, st in self.state.items() if st["submitted"] is None and (st["started"] or st["answers"])]
        for cand in pending:
            self.submit(cand, auto=True)

    # ---- candidates
    def login(self, candidate_id: str, code: str) -> str:
        cid = candidate_id.strip()
        if self.roster is None:
            raise PermissionError("The candidate list has not been loaded yet.")
        with self.lock:
            fails = self.bad.get(cid, [0, 0.0])
            if fails[1] > time.time():
                raise PermissionError(f"Too many wrong codes. Try again in {int(fails[1] - time.time()) + 1} s.")
            expect = self.roster["candidates"].get(cid)
            got = _code_hash(self.roster["salt"], cid, code, self.roster.get("kdf"))
            if not expect or not hmac.compare_digest(expect, got):
                fails[0] += 1
                if fails[0] >= MAX_BAD_LOGINS:
                    fails = [0, time.time() + LOCKOUT_SECONDS]
                self.bad[cid] = fails
                raise PermissionError("Wrong candidate ID or access code.")
            self.bad.pop(cid, None)
            for t in [t for t, c in self.sessions.items() if c == cid]:    # one live session per candidate
                del self.sessions[t]
            token = secrets.token_urlsafe(32)
            self.sessions[token] = cid
            if not self._st(cid)["started"]:
                self._st(cid)["started"] = True
                self._log(ev="login", cand=cid)
            return token

    def paper_for(self, cand: str) -> dict:
        with self.lock:
            st = self._st(cand)
            return {
                "exam_name": self.payload["exam_name"],
                "option_style": self.payload.get("option_style", "(A)"),
                "candidate_id": cand,
                "starts_at": self.starts_at,
                "ends_at": self.ends_at,
                "now": self.now(),
                "questions": personalise(self.payload, self.secret, cand),
                "answers": dict(st["answers"]),
                "submitted": st["submitted"],
            }

    def answer(self, cand: str, qid, key):
        with self.lock:
            st = self._st(cand)
            if st["submitted"]:
                raise ValueError("Your answer sheet is already submitted.")
            if self.phase != "open":
                raise ValueError("The exam is not open.")
            q = next((q for q in self.payload["questions"] if q["id"] == qid), None)
            if q is None:
                raise ValueError("Unknown question.")
            if key is not None and key not in [o["key"] for o in q["options"]]:
                raise ValueError("Unknown option.")
            if key is None:
                st["answers"].pop(qid, None)
            else:
                st["answers"][qid] = key
            self._log(ev="answer", cand=cand, qid=qid, key=key)

    def submit(self, cand: str, auto: bool = False) -> dict:
        with self.lock:
            st = self._st(cand)
            if st["submitted"]:
                return st["submitted"]
            if self.sealed or self.root_record is not None:
                raise ValueError("The exam is closed; no more sheets can be submitted.")
            sheet = {
                "center": self.center,
                "variant_id": self.payload["variant_id"],
                "candidate_id": cand,
                "answers": {str(q["id"]): st["answers"].get(q["id"]) for q in
                            sorted(self.payload["questions"], key=lambda q: str(q["id"]))},
                "submitted_at": min(self.now(), self.ends_at),
                "auto_submitted": auto,
            }
            canonical = json.dumps(sheet, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            record = {"sheet": sheet, "sha256": digest}
            (self.data_dir / "sheets" / f"{cand}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            st["submitted"] = record
            self._log(ev="submit", cand=cand, sheet=record)
        return record

    # ---- on-chain anchoring: ONE transaction per center, after the exam
    def _maybe_anchor_root(self) -> bool:
        """
        Records the Merkle root of every submitted sheet on-chain (custody stage ANSWERS_ROOT:<count>).
        Returns True when there is nothing left to record (done, or no sheets at all).
        Called after _auto_submit_all(), so every candidate who started has a sheet. Once the list of sheets is
        read, `sealed` stops further submissions so the root always covers exactly the stored sheets.
        """
        if self.root_record is not None:
            return True
        if self._root_error and self._root_error.startswith("ABORT"):
            return False
        now = time.time()
        if now - self._last_root_try < 5:
            return False
        self._last_root_try = now
        with self.lock:
            self.sealed = True
            hashes = [st["submitted"]["sha256"] for st in self.state.values() if st["submitted"]]
        if not hashes:
            return True
        root = merkle_root(hashes)
        n = len(set(hashes))
        me = self.dev["account"].address
        try:
            log = self.contract.functions.getCustodyLog(self.center).call()
            mine = [e for e in log if e[0].startswith("ANSWERS_ROOT:") and e[1].lower() == me.lower()]
            if mine:                                             # we already did this before a restart
                if bytes(mine[0][3]).hex() != root:
                    self._root_error = "ABORT: a different answers root is already recorded on-chain for this center."
                    print(self._root_error)
                    return False
                tx = None
            else:
                tx = pa.send(self.w3, self.dev["account"],
                             self.contract.functions.logCustodyEvent(self.center, f"ANSWERS_ROOT:{n}", bytes.fromhex(root)))
        except Exception as e:
            self._root_error = str(e)[:200]
            print(f"   could not record the answers root on-chain yet ({self._root_error}); will retry")
            return False
        rec = {"root": root, "count": n, "tx": tx, "proofs": merkle_proofs(hashes)}
        self.root_file.write_text(json.dumps(rec), encoding="utf-8")
        with self.lock:
            self.root_record = rec
            self._root_error = None
        print(f"   recorded the Merkle root of {n} answer sheets on-chain" + (f" (tx {tx[:12]}...)" if tx else ""))
        return True


# ---------------------------------------------------------------------
# web app
# ---------------------------------------------------------------------

def build_app(exam: Exam):
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import FileResponse
    from pydantic import BaseModel

    app = FastAPI(title=f"Exam server {exam.center}")
    # The board's online page reads /api/status from another origin. GET only: login, answers and
    # submit (POST/PUT) stay same-origin, so another site cannot drive a candidate's session.
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=[])

    class Login(BaseModel):
        candidate_id: str
        code: str

    class Answer(BaseModel):
        qid: int | str
        key: int | None = None

    def me(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        cand = exam.sessions.get(token)
        if not cand:
            raise HTTPException(401, "Please log in again.")
        return cand

    @app.get("/api/status")
    def status():
        with exam.lock:
            return {"center": exam.center, "phase": exam.phase, "message": exam.message,
                    "starts_at": exam.starts_at, "ends_at": exam.ends_at,
                    "now": exam.now(), "exam_name": exam.payload["exam_name"] if exam.payload else None,
                    "roster_ready": exam.roster is not None,
                    "sheets_root": exam.root_record["root"] if exam.root_record else None,
                    "sheets_count": exam.root_record["count"] if exam.root_record else None,
                    "results_sent": exam.results_sent}

    @app.post("/api/login")
    def login(body: Login):
        if exam.phase != "open":
            # A candidate who already submitted may still log in to see their receipt.
            st = exam.state.get(body.candidate_id.strip())
            if not (st and st["submitted"] and exam.payload):
                raise HTTPException(425, exam.message)
        try:
            return {"token": exam.login(body.candidate_id, body.code)}
        except PermissionError as e:
            raise HTTPException(403, str(e))

    @app.get("/api/paper")
    def paper(cand: str = Depends(me)):
        if exam.payload is None:
            raise HTTPException(425, exam.message)
        return exam.paper_for(cand)

    @app.put("/api/answer")
    def answer(body: Answer, cand: str = Depends(me)):
        try:
            exam.answer(cand, body.qid, body.key)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/submit")
    def submit(cand: str = Depends(me)):
        if exam.phase not in ("open", "closed"):
            raise HTTPException(409, "The exam is not open.")
        try:
            r = exam.submit(cand)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"sha256": r["sha256"], "submitted_at": r["sheet"]["submitted_at"]}

    @app.get("/api/receipt")
    def receipt(cand: str = Depends(me)):
        st = exam.state.get(cand)
        if not st or not st["submitted"]:
            raise HTTPException(404, "Not submitted yet.")
        sha = st["submitted"]["sha256"]
        rec = exam.root_record
        return {"sha256": sha, "submitted_at": st["submitted"]["sheet"]["submitted_at"],
                "anchored": rec is not None, "root": rec["root"] if rec else None,
                "proof": rec["proofs"].get(sha) if rec else None, "sheets_count": rec["count"] if rec else None,
                "tx": rec["tx"] if rec else None, "anchor_error": exam._root_error}

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(HERE / "candidate.html")

    return app


def cmd_run(args):
    dev = pa.load_device(args)
    w3, contract = pa.connect(args)
    exam = Exam(args, dev, w3, contract)
    print(f"Exam server for {args.center}  device {dev['account'].address}  chain {args.rpc_url}")
    print(f"  enrollment fingerprint {pa.device_fingerprint(dev)}  (the board asks for this when enrolling)")
    print(f"  candidate list and exam duration come from the board (checked against the chain); "
          f"http://0.0.0.0:{args.port}/")
    threading.Thread(target=exam.lifecycle, daemon=True).start()
    import uvicorn
    uvicorn.run(build_app(exam), host=args.host, port=args.port, log_level="warning")


def main():
    p = argparse.ArgumentParser(description="NEET Integrity Chain center exam server")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--center", required=True, help="center id, e.g. center_001")
        sp.add_argument("--home", help="folder holding device identities (same as the printer agent)")

    r = sub.add_parser("roster", help="create candidate IDs and one-time access codes")
    common(r)
    r.add_argument("--count", type=int, required=True)
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_roster)

    s = sub.add_parser("run", help="serve the exam")
    common(s)
    s.add_argument("--rpc-url", default=(os.environ.get("CHAIN_RPC_URL") or os.environ.get("RPC_URL")
                                         or "http://127.0.0.1:8545"))
    s.add_argument("--deployment", default=str(pa.DEFAULT_DEPLOYMENT))
    s.add_argument("--api-url", default=(os.environ.get("BACKEND_API_URL") or "http://127.0.0.1:8000").rstrip("/"))
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--duration-minutes", type=float, default=None,
                   help="only used for a center registered before modes existed; otherwise the board's value is used")
    s.add_argument("--poll", type=float, default=3.0)
    s.add_argument("--allow-local-roster", action="store_true",
                   help="DEMO ONLY: accept a roster made on this machine when the board anchored none")
    s.add_argument("--data-dir", default=str(HERE / "data"), help="answers, journal, sheets (persistent)")
    s.set_defaults(func=cmd_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
