"""
app.py

FastAPI backend (the exam board's server).

Board endpoints:
  POST /import-pdf               exam PDF -> reviewable draft (questions, options, figures, answers)
  POST /import-answer-key        apply a separate answer key file to the draft
  POST /generate                 paper JSON + number of variants -> shuffle, render, encrypt, store
  POST /enroll/{center_id}       attach a printer (chain address + RSA public key) to a center
  POST /register                 register a center on-chain (exam start in N minutes, expected copies)
  POST /release-key/{center_id}  wrap the AES key to the printer's public key, post it on-chain
                                 (the contract enforces the exam start time), then destroy the stored key
  GET  /centers                  all centers with live on-chain status
  POST /dev/fast-forward         DEV ONLY: advance the local Hardhat chain clock

Printer / center endpoints:
  GET  /blob/{center_id}         the encrypted blob (ciphertext only; useless without the key)
  GET  /status/{center_id}       registered? key released? copies printed / expected

Other:
  GET  /storage-status           blob store, key store and master-key check
  POST /custody-event            optional audit-trail entry
  /ui/                           serves the frontend

There is deliberately no endpoint that returns a decrypted paper: decryption happens on
the printer controller (printer-agent/), which holds the only key that can unwrap the AES key.

Run with: uvicorn app:app --reload   (from inside backend/), then open http://localhost:8000/
"""

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from web3 import Web3

import config
import db
import grading
import keystore
import keywrap
import pipeline
import pdf_importer
from keystore import KeyStoreError
from storage import BlobStoreError, get_blob_store
from web3_client import PaperCustodyClient, DEPLOYMENT_PATH

BACKEND_DIR = Path(__file__).parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"

app = FastAPI(title="NEET Integrity Chain - Backend")

# Lets the pages work even if opened straight from disk (file://) or another port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _storage_error(request, exc):
    # Misconfigured/unreachable storage or a missing/wrong master key: a server-side
    # problem the board operator has to fix, not a bad request.
    return JSONResponse(status_code=503, content={"detail": str(exc)})


app.add_exception_handler(KeyStoreError, _storage_error)
app.add_exception_handler(BlobStoreError, _storage_error)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

_client: PaperCustodyClient | None = None
_client_deploy_mtime: float | None = None


def get_client() -> PaperCustodyClient:
    """
    Lazily connect to the chain. Rebuilds the client if contract_deployment.json changed
    (i.e. you redeployed after restarting the Hardhat node).
    """
    global _client, _client_deploy_mtime
    mtime = DEPLOYMENT_PATH.stat().st_mtime if DEPLOYMENT_PATH.exists() else None
    if _client is None or mtime != _client_deploy_mtime:
        _client = PaperCustodyClient()
        _client_deploy_mtime = mtime
    return _client


def require_record(center_id: str) -> dict:
    record = db.get_center_record(center_id)
    if record is None:
        raise HTTPException(404, f"Unknown center: {center_id}")
    return record


def require_chain() -> PaperCustodyClient:
    try:
        return get_client()
    except Exception as e:
        raise HTTPException(
            503,
            f"Blockchain not reachable ({e}). Is the chain running "
            f"(`python chain/chainctl.py up`) and the contract deployed?",
        )


def short_error(e: Exception) -> str:
    """Pull the readable revert reason out of web3's long error strings."""
    msg = str(e)
    for marker in ("reverted with reason string '", "execution reverted: "):
        if marker in msg:
            tail = msg.split(marker, 1)[1]
            return tail.split("'", 1)[0].split('"', 1)[0]
    return msg


def printer_funding_eth() -> float:
    """
    How much ETH to send a printer when it is enrolled (it needs gas to log prints).
    Local dev node: 0.5 by default. On a free-gas private chain nothing is sent (see fund_if_needed).
    With BOARD_PRIVATE_KEY set (a real network) the default is 0,
    so test ETH is never spent unless you set PRINTER_FUNDING_ETH explicitly.
    """
    raw = config.env("PRINTER_FUNDING_ETH")
    if raw == "":
        return 0.0 if config.env("BOARD_PRIVATE_KEY") else 0.5
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------
# Board: import a digital exam PDF for human review
# ---------------------------------------------------------------------

# Importing is CPU-heavy (a scanned paper can take minutes). It runs in a worker thread so the server
# keeps answering other requests, and one import at a time (the PDF engines are not meant to be shared
# between threads).
_import_lock = asyncio.Lock()


@app.post("/import-pdf")
async def import_pdf(
    file: UploadFile = File(...),
    # Form fields: the page posts them as multipart form data. ("auto" detects the identifier.)
    question_format: str = Form("auto"),
    option_format: str = Form("auto"),
    option_count: int = Form(4),
    question_boundary: str = Form("line"),
):
    """Read an exam PDF (text layer, OCR for scanned pages) and return a reviewable draft; never auto-generate."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF file.")
    data = await file.read()
    if not data or len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "PDF must be non-empty and no larger than 25 MB.")
    async with _import_lock:
        try:
            return await run_in_threadpool(
                pdf_importer.parse_exam_pdf, data, file.filename,
                question_format, option_format, option_count, question_boundary)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/import-answer-key")
async def import_answer_key(file: UploadFile = File(...), paper_json: str = Form(...)):
    """
    Apply a separately uploaded answer key. paper_json only needs each question's id and options
    (the page leaves the pictures out: a form field is limited to 1 MB); the answers come back in
    `paper` and the page merges them into the draft.
    """
    import json
    if not file.filename or not file.filename.lower().endswith((".txt", ".csv", ".tsv", ".pdf")):
        raise HTTPException(400, "Upload an answer key as .txt, .csv, .tsv, or .pdf.")
    data = await file.read()
    if not data or len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "Answer key must be non-empty and no larger than 25 MB.")
    try:
        paper = json.loads(paper_json)
        async with _import_lock:
            result = await run_in_threadpool(
                pdf_importer.parse_answer_key, data, file.filename, paper.get("questions", []))
        return {"paper": paper, **result}
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc) or "Could not parse answer key.")


# ---------------------------------------------------------------------
# Board: generate a batch
# ---------------------------------------------------------------------

class GenerateRequest(BaseModel):
    paper: dict
    num_variants: int


@app.post("/generate")
def generate(req: GenerateRequest):
    """
    Shuffle options, render a PDF, encrypt, store: once per variant, one variant per center.
    The blob goes to the blob store, the AES key to the key store, nothing else is kept.
    """
    try:
        results = pipeline.generate_all(req.paper, req.num_variants)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"generated": len(results), "centers": results}


# ---------------------------------------------------------------------
# Board: list centers with live chain status
# ---------------------------------------------------------------------

@app.get("/centers")
def list_centers():
    records = db.list_all_centers()

    client = None
    chain_error = None
    chain_time = None
    dev_tools = False
    try:
        client = get_client()
        chain_time = client.chain_now()
        dev_tools = client.is_dev_node()
    except Exception as e:
        chain_error = str(e)
        client = None

    keys_present = keystore.centers_with_keys()

    centers = []
    for center_id in sorted(records):
        rec = records[center_id]
        state = None
        if client is not None:
            try:
                state = client.get_state(center_id)
            except Exception as e:
                chain_error = str(e)

        registered = state["registered"] if state else None
        on_chain = bool(state and registered)

        centers.append({
            "center_id": center_id,
            "variant_id": rec["variant_id"],
            "blob_hash": rec["blob_hash"],
            "has_key": center_id in keys_present,
            "enrolled": bool(rec.get("printer_address")),
            "online_ready": bool(rec.get("online_blob_hash")),
            "printer_address": rec.get("printer_address"),
            "printer_fingerprint": rec.get("printer_fingerprint"),
            "registered": registered,
            "key_released": (state["key_released"] if on_chain else False) if state else None,
            "exam_start_time": state["exam_start_time"] if on_chain else None,
            "expected_copies": state["expected_copies"] if on_chain else None,
            "printed_count": state["printed_count"] if on_chain else None,
        })

    return {
        "chain_online": chain_error is None and client is not None,
        "chain_error": chain_error,
        "chain_time": chain_time,
        "dev_tools": dev_tools,   # true only on Hardhat/Anvil/Ganache: shows the fast-forward control
        "centers": centers,
    }


# ---------------------------------------------------------------------
# Board: enroll a printer
# ---------------------------------------------------------------------

class EnrollRequest(BaseModel):
    address: str
    public_key_pem: str


@app.post("/enroll/{center_id}")
def enroll_printer(center_id: str, req: EnrollRequest):
    """
    Attach a printer to a center: its chain address (who may log prints) and its RSA public
    key (what the AES key gets wrapped to). Returns the key's fingerprint, which the person
    enrolling compares against the one shown on the printer itself.
    """
    require_record(center_id)

    if not Web3.is_address(req.address):
        raise HTTPException(400, "Not a valid Ethereum address.")
    address = Web3.to_checksum_address(req.address)

    try:
        fp = keywrap.fingerprint(req.public_key_pem)
    except ValueError as e:
        raise HTTPException(400, str(e))

    for other_id, other in db.list_all_centers().items():
        if other_id != center_id and (other.get("printer_address") or "").lower() == address.lower():
            raise HTTPException(409, f"That printer address is already enrolled for {other_id}.")

    client = None
    try:
        client = get_client()
    except Exception:
        pass
    if client is not None and client.is_registered(center_id):
        raise HTTPException(409, f"{center_id} is already registered on-chain; its printer can't be changed.")

    db.update_center_record(
        center_id,
        printer_address=address,
        printer_public_key=req.public_key_pem.strip(),
        printer_fingerprint=fp,
    )

    funded, warning = False, None
    amount = printer_funding_eth()
    if client is None:
        warning = "Chain not reachable, so the printer was not funded."
    elif amount > 0:
        try:
            funded = client.fund_if_needed(address, amount)
        except Exception as e:
            warning = f"Enrolled, but funding the printer failed: {short_error(e)}"

    return {"status": "enrolled", "center_id": center_id, "address": address,
            "fingerprint": fp, "funded": funded, "warning": warning}


# ---------------------------------------------------------------------
# Board: register / release
# ---------------------------------------------------------------------

@app.post("/register")
def register_center(center_id: str, exam_start_in_minutes: float, expected_copies: int):
    """
    Registers a center on-chain: blob hash, variant, exam start, printer address, expected
    copies. No key goes on-chain here. The exam start is computed from the chain's own clock,
    so it can't drift from what the contract will check.
    """
    record = require_record(center_id)
    if not record.get("printer_address"):
        raise HTTPException(409, f"Enroll {center_id}'s printer before registering it.")
    if exam_start_in_minutes < 0.5:
        raise HTTPException(400, "Exam must start at least 0.5 minutes from now.")
    if not 1 <= expected_copies <= 1_000_000:
        raise HTTPException(400, "Expected copies must be between 1 and 1,000,000.")

    client = require_chain()
    if client.is_registered(center_id):
        raise HTTPException(409, f"{center_id} is already registered on-chain.")

    # Integrity: the stored blob must still be the one we generated.
    blob_bytes = get_blob_store().get(record["blob_object_key"])
    if hashlib.sha256(blob_bytes).hexdigest() != record["blob_hash"]:
        raise HTTPException(409, "The stored blob no longer matches its recorded hash.")

    # Mine a fresh block first so 'now' is the chain's real current time, not a stale one.
    now = client.mine_fresh_block()
    exam_start_time = now + int(exam_start_in_minutes * 60)

    try:
        tx_hash = client.register_paper(
            center_id=center_id,
            blob_hash_hex=record["blob_hash"],
            variant_id=record["variant_id"],
            exam_start_time=exam_start_time,
            printer_address=record["printer_address"],
            expected_copies=expected_copies,
        )
    except Exception as e:
        raise HTTPException(400, f"Registration failed: {short_error(e)}")

    db.update_center_record(center_id, exam_start_time=exam_start_time)

    # Online mode: anchor the payload hash so the center's exam server can verify what it downloads.
    online_warning = None
    if record.get("online_blob_hash"):
        try:
            client.log_custody_event(center_id, "ONLINE_PAYLOAD", record["online_blob_hash"])
        except Exception as e:
            online_warning = f"Registered, but anchoring the online payload failed: {short_error(e)}"
    return {"status": "registered", "tx_hash": tx_hash, "exam_start_time": exam_start_time,
            "online": bool(record.get("online_blob_hash")), "warning": online_warning}


@app.post("/release-key/{center_id}")
def release_key(center_id: str):
    """
    Wrap the center's AES key to its printer's public key and post it on-chain. The contract
    reverts before the exam start time. Once the release is on-chain the board no longer
    needs the key, so the stored copy is destroyed.
    """
    record = require_record(center_id)
    client = require_chain()

    state = client.get_state(center_id)
    if not state["registered"]:
        raise HTTPException(409, f"{center_id} is not registered on-chain yet.")
    if state["key_released"]:
        # Finish an interrupted cleanup if the key is somehow still stored.
        destroyed = keystore.destroy_key(center_id)
        raise HTTPException(409, "Key already released." + (" The stored key has now been destroyed." if destroyed else ""))

    pem = record.get("printer_public_key")
    if not pem:
        raise HTTPException(409, f"{center_id} has no enrolled printer public key.")

    aes_key = keystore.get_key(center_id)
    try:
        wrapped = keywrap.wrap_key(pem, aes_key)
    finally:
        del aes_key

    try:
        tx_hash = client.release_key(center_id, wrapped)
    except Exception as e:
        # Not released (e.g. too early): the stored key stays intact.
        raise HTTPException(400, f"Key release failed: {short_error(e)}")

    destroyed = keystore.destroy_key(center_id)
    return {"status": "key_released", "tx_hash": tx_hash, "stored_key_destroyed": destroyed}


@app.post("/dev/fast-forward")
def dev_fast_forward(seconds: int = 300):
    """DEV ONLY (Hardhat/Anvil/Ganache): advance the chain clock. Refused on the private Besu chain."""
    if not 1 <= seconds <= 60 * 60 * 24 * 30:
        raise HTTPException(400, "seconds must be between 1 and 30 days.")
    client = require_chain()
    try:
        new_time = client.fast_forward(seconds)
    except Exception as e:
        raise HTTPException(400, f"Fast-forward failed: {short_error(e)}")
    return {"status": "advanced", "chain_time": new_time}


@app.post("/custody-event")
def custody_event(center_id: str, stage: str):
    """Optional audit-trail entry (not used by the UI)."""
    record = require_record(center_id)
    client = require_chain()
    current_hash = hashlib.sha256(get_blob_store().get(record["blob_object_key"])).hexdigest()
    tx_hash = client.log_custody_event(center_id, stage, current_hash)
    return {"status": "logged", "stage": stage, "tx_hash": tx_hash}


# ---------------------------------------------------------------------
# Printer / center endpoints
# ---------------------------------------------------------------------

@app.get("/blob/{center_id}")
def get_blob(center_id: str):
    """
    The encrypted blob, for the printer agent to download. It is ciphertext: without the AES
    key (which only that center's printer can unwrap, and only after release) it is useless,
    so this endpoint needs no authentication. The agent re-checks it against the on-chain hash.
    """
    record = require_record(center_id)
    data = get_blob_store().get(record["blob_object_key"])
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-Blob-SHA256": record["blob_hash"]},
    )


@app.get("/online-blob/{center_id}")
def get_online_blob(center_id: str):
    """
    Encrypted question payload for the center's exam server (online mode). Ciphertext only, like /blob.
    Its hash is anchored on-chain by the board (custody stage ONLINE_PAYLOAD) when the center is registered.
    """
    record = require_record(center_id)
    if not record.get("online_object_key"):
        raise HTTPException(404, f"{center_id} was generated before online mode existed; it is print-only.")
    data = get_blob_store().get(record["online_object_key"])
    return Response(content=data, media_type="application/octet-stream",
                    headers={"X-Blob-SHA256": record["online_blob_hash"]})


class SheetItem(BaseModel):
    sheet: dict
    sha256: str


class SheetBatch(BaseModel):
    sheets: list[SheetItem]
    signature: str


@app.post("/sheets/{center_id}")
def receive_sheets(center_id: str, batch: SheetBatch):
    """
    A center's exam server delivers its submitted answer sheets after the exam. Authenticated by a signature
    from the center's enrolled device (no shared secret). Each sheet is checked against the hash the device
    wrote on-chain; only matching sheets are scored. Re-sending is safe; a stored sheet is never overwritten.
    """
    record = require_record(center_id)
    device = record.get("printer_address")
    if not device:
        raise HTTPException(409, f"{center_id} has no enrolled device.")
    if len(batch.sheets) > 20000:
        raise HTTPException(413, "Too many sheets in one request.")

    hashes = [i.sha256 for i in batch.sheets]
    try:
        signer = grading.signer_of(center_id, hashes, batch.signature)
    except Exception:
        raise HTTPException(403, "The signature is not valid.")
    if signer.lower() != device.lower():
        raise HTTPException(403, f"Not signed by the device enrolled for {center_id}.")

    client = require_chain()
    anchored = grading.anchored_hashes(client.get_custody_log(center_id), device)
    answer_key = keystore.get_answers(record["variant_id"])

    counts = {"scored": 0, "unverified": 0, "already_stored": 0, "conflicts": 0}
    for item in batch.sheets:
        sheet = item.sheet
        cand = str(sheet.get("candidate_id", ""))
        actual = grading.sheet_hash(sheet)
        problem = None
        if not cand or sheet.get("center") != center_id or sheet.get("variant_id") != record["variant_id"]:
            problem = "the sheet does not belong to this center's paper"
        elif actual != item.sha256:
            problem = "the sheet does not match its own hash"
        elif cand not in anchored:
            problem = "no hash for this candidate was recorded on-chain"
        elif anchored[cand] != actual:
            problem = "the sheet differs from the hash recorded on-chain"

        if problem:
            status = db.save_result(center_id, cand or "?", actual, json.dumps(sheet), False, None, None, None, None, problem)
            counts["conflicts" if status == "conflict" else "already_stored" if status == "same" else "unverified"] += 1
            continue
        g = grading.score(sheet.get("answers", {}), answer_key)
        status = db.save_result(center_id, cand, actual, json.dumps(sheet), True, g["score"], g["correct"], g["wrong"], g["blank"], None)
        counts["conflicts" if status == "conflict" else "already_stored" if status == "same" else "scored"] += 1
    return counts


@app.get("/results")
def results_summary():
    rows = db.list_results()
    by_center: dict = {}
    for r in rows:
        c = by_center.setdefault(r["center_id"], {"center_id": r["center_id"], "sheets": 0, "verified": 0, "scores": []})
        c["sheets"] += 1
        if r["verified"]:
            c["verified"] += 1
            c["scores"].append(r["score"])
    out = []
    for c in by_center.values():
        sc = c.pop("scores")
        c.update(unverified=c["sheets"] - c["verified"], average=round(sum(sc) / len(sc), 1) if sc else None,
                 highest=max(sc) if sc else None, lowest=min(sc) if sc else None)
        out.append(c)
    correct_mark, wrong_mark = grading.marks()
    return {"marking": {"correct": correct_mark, "wrong": wrong_mark, "blank": 0}, "centers": out}


@app.get("/results/{center_id}")
def results_for_center(center_id: str):
    require_record(center_id)
    return {"center_id": center_id, "candidates": db.list_results(center_id)}


@app.get("/status/{center_id}")
def status(center_id: str):
    require_record(center_id)
    client = require_chain()
    state = client.get_state(center_id)
    return {
        "center_id": center_id,
        "registered": state["registered"],
        "key_released": state["key_released"],
        "expected_copies": state["expected_copies"] if state["registered"] else None,
        "printed_count": state["printed_count"] if state["registered"] else None,
        "exam_start_time": state["exam_start_time"] if state["registered"] else None,
        "online_ready": bool(require_record(center_id).get("online_blob_hash")),
    }


# ---------------------------------------------------------------------
# Storage health
# ---------------------------------------------------------------------

@app.get("/storage-status")
def storage_status():
    """Where blobs and keys live right now, and whether each is usable."""
    try:
        store = get_blob_store()
        blobs = store.describe()
        try:
            store.check()
            blobs["ok"] = True
        except BlobStoreError as e:
            blobs["ok"] = False
            blobs["error"] = str(e)
    except BlobStoreError as e:
        blobs = {"ok": False, "error": str(e)}

    keys = keystore.status()
    return {
        "blobs": blobs,
        "keys": keys,
        "records_database": str(db.DB_PATH),
        "ready": bool(blobs.get("ok")) and keys["master_key_configured"]
                 and keys["master_key_matches_stored"] is not False,
    }


# ---------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")


app.mount("/ui", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")
