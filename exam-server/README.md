# Online exam mode

One exam server per center (`exam_server.py`), on the same device identity as that center's printer.

## Flow
1. Board: generate papers as before. Each NEW paper now also carries an encrypted question payload
   (same AES key). Papers generated before this update are print-only.
2. Board: enroll the printer and Register the center as before. Registering also writes the payload hash
   on-chain (custody stage `ONLINE_PAYLOAD`).
3. Center, once: `python exam_server.py roster --center center_001 --count 30` prints candidate IDs and
   access codes (shown once; the file keeps salted hashes). Hand out the slips.
4. Center: `python exam_server.py run --center center_001 --rpc-url http://127.0.0.1:8546 --duration-minutes 180`
   (Docker: `chain/compose.exam.yaml`). It waits for the key release, verifies the payload against the
   on-chain hash, decrypts in memory, and opens at the on-chain start time.
5. Candidates open `http://<center server>:8080/`, log in, answer; timer is server-side; the exam closes at
   start + duration and unsubmitted sheets are submitted automatically.
6. Each submitted answer sheet is SHA-256 hashed; the hash goes on-chain as `ANSWERS:<candidate id>`
   (signed by the center device). Sheets stay on the center disk (`data/sheets/`).

## Not included yet
- Grading (the board still holds the answer key; sheets record the chosen option's key in the generated variant).
- Handing sheets back to the board; a Merkle root per center would replace per-candidate transactions at scale.
- Proctoring, browser lockdown, candidate photo check, load testing beyond a few candidates.
