#!/usr/bin/env python3
"""
NEET Integrity Chain - center exam server (online mode).

One of these runs per exam center, on that center's own LAN. It is the online counterpart of the
printer agent and uses the SAME device identity (same folder, same keys):

    1. waits until the board has released the key on-chain (the time-lock),
    2. downloads the encrypted question payload, checks it against the hash the board anchored
       on-chain (custody stage ONLINE_PAYLOAD), unwraps the AES key with this device's private
       RSA key and decrypts the paper IN MEMORY ONLY,
    3. serves it to candidates on the center's network, shuffled differently per candidate,
       with a server-side timer and saved answers,
    4. when a candidate submits (or time runs out), builds the answer sheet, SHA-256 hashes it
       and records the hash on-chain (custody stage ANSWERS:<candidate>). The answers themselves
       stay on the center's disk; the chain proves later that a sheet was not altered.

The server never holds the answer key. Grading is done by the board afterwards.

Commands
    python exam_server.py roster --center center_001 --count 30      # make candidate IDs + access codes
    python exam_server.py run    --center center_001 --rpc-url http://127.0.0.1:8546

Candidate page: http://<this machine>:8080/
"""
import argparse
import hashlib
import hmac
import json
import os
import queue
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

import printer_agent as pa   # device identity, chain connection, signing: shared with the printer

DEFAULT_DURATION_MIN = 180
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I
MAX_BAD_LOGINS = 5
LOCKOUT_SECONDS = 30


# ---------------------------------------------------------------------
# roster
# ---------------------------------------------------------------------

def _code_hash(salt: str, candidate_id: str, code: str) -> str:
    return hashlib.sha256(f"{salt}|{candidate_id}|{code.strip().upper()}".encode()).hexdigest()


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
        entries[cid] = _code_hash(salt, cid, code)
        slips.append((cid, code))
    pa.private_file(path, json.dumps({"center": args.center, "salt": salt, "candidates": entries}, indent=2).encode())
    print(f"Roster for {args.center}: {args.count} candidates -> {path}")
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
        self.duration = int(args.duration_minutes * 60)
        self._ref = (0, time.time())

        self.data_dir = Path(args.data_dir).resolve()
        (self.data_dir / "sheets").mkdir(parents=True, exist_ok=True)
        self.secret = self._load_secret()
        self.roster = self._load_roster()
        self.sessions: dict[str, str] = {}                 # token -> candidate id
        self.state: dict[str, dict] = {}                   # candidate id -> {answers, submitted, ...}
        self.bad: dict[str, list] = {}                     # candidate id -> [count, locked_until]
        self.anchor_q: "queue.Queue[str]" = queue.Queue()
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

    def _load_roster(self) -> dict:
        p = roster_path(self.args)
        if not p.exists():
            sys.exit(f"No roster at {p}. Create one first:  python exam_server.py roster --center {self.center} --count N")
        return json.loads(p.read_text())

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
        return self.state.setdefault(cand, {"answers": {}, "submitted": None, "tx": None, "anchor_error": None, "started": False})

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
                if self.phase in ("waiting", "loading", "error") and self.payload is None:
                    if not st["registered"]:
                        msg = "Waiting for the board to register this center."
                    elif st["printer"].lower() != me.lower():
                        self._fail(f"The device registered for {self.center} is {st['printer']}, not this server.")
                        return
                    elif not st["released"]:
                        when = time.strftime("%H:%M:%S", time.localtime(st["start"]))
                        msg = f"The paper is locked until the exam starts (chain start time {when})."
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

    def _auto_submit_all(self):
        with self.lock:
            pending = [c for c, st in self.state.items() if st["submitted"] is None and (st["started"] or st["answers"])]
        for cand in pending:
            self.submit(cand, auto=True)

    # ---- candidates
    def login(self, candidate_id: str, code: str) -> str:
        cid = candidate_id.strip()
        with self.lock:
            fails = self.bad.get(cid, [0, 0.0])
            if fails[1] > time.time():
                raise PermissionError(f"Too many wrong codes. Try again in {int(fails[1] - time.time()) + 1} s.")
            expect = self.roster["candidates"].get(cid)
            got = _code_hash(self.roster["salt"], cid, code)
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
        self.anchor_q.put(cand)
        return record

    # ---- on-chain anchoring (background thread)
    def anchor_worker(self):
        while True:
            cand = self.anchor_q.get()
            for attempt in range(6):
                try:
                    with self.lock:
                        digest = self.state[cand]["submitted"]["sha256"]
                    tx = pa.send(self.w3, self.dev["account"],
                                 self.contract.functions.logCustodyEvent(
                                     self.center, f"ANSWERS:{cand}", bytes.fromhex(digest)))
                    with self.lock:
                        self.state[cand]["tx"] = tx
                        self.state[cand]["anchor_error"] = None
                    print(f"   anchored sheet of {cand} on-chain (tx {tx[:12]}...)")
                    break
                except Exception as e:
                    with self.lock:
                        self.state[cand]["anchor_error"] = str(e)[:200]
                    time.sleep(2 * (attempt + 1))
            self.anchor_q.task_done()


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
                    "anchoring_queue": exam.anchor_q.qsize()}

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
        r = exam.submit(cand)
        return {"sha256": r["sha256"], "submitted_at": r["sheet"]["submitted_at"]}

    @app.get("/api/receipt")
    def receipt(cand: str = Depends(me)):
        st = exam.state.get(cand)
        if not st or not st["submitted"]:
            raise HTTPException(404, "Not submitted yet.")
        return {"sha256": st["submitted"]["sha256"], "submitted_at": st["submitted"]["sheet"]["submitted_at"],
                "tx": st["tx"], "anchor_error": st["anchor_error"]}

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(HERE / "candidate.html")

    return app


def cmd_run(args):
    dev = pa.load_device(args)
    w3, contract = pa.connect(args)
    exam = Exam(args, dev, w3, contract)
    print(f"Exam server for {args.center}  device {dev['account'].address}  chain {args.rpc_url}")
    print(f"  {len(exam.roster['candidates'])} candidates on the roster; duration {args.duration_minutes} min; http://0.0.0.0:{args.port}/")
    threading.Thread(target=exam.lifecycle, daemon=True).start()
    threading.Thread(target=exam.anchor_worker, daemon=True).start()
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
    s.add_argument("--duration-minutes", type=float, default=DEFAULT_DURATION_MIN)
    s.add_argument("--poll", type=float, default=3.0)
    s.add_argument("--data-dir", default=str(HERE / "data"), help="answers, journal, sheets (persistent)")
    s.set_defaults(func=cmd_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
