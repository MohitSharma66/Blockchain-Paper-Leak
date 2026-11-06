# Online exam mode

One exam server per center (`exam_server.py`), on the same device identity as that center's printer.

## Exam type
The board chooses Online or Offline (and, for online, the exam length) when it generates a batch. At registration
that is written on-chain (`EXAM_MODE:<mode>:<minutes>`). Start a center's device with
`scripts\\new-center-device.cmd center_013 offline|online`. The printer agent refuses an online center and this
server refuses an offline one; the exam length always comes from the board. Centers made before exam types existed
can do either (use `--duration-minutes` for those).

## Flow
1. Board: generate papers as before. Each NEW paper now also carries an encrypted question payload
   (same AES key). Papers generated before this update are print-only.
2. Board: enroll the printer and Register the center as before. Registering also writes the payload hash
   on-chain (custody stage `ONLINE_PAYLOAD`).
3. Board: create the candidate list for the center (board page, "Candidate lists", or `POST /roster/<center>`).
   Each candidate gets an ID and a one-time access code. The codes are returned once, for the admit cards; the
   board and the centers keep only salted PBKDF2 hashes. The list's SHA-256 goes on-chain (custody stage `ROSTER`)
   when the center is registered (or right away if it already is) and cannot be changed afterwards.
4. Center: `python exam_server.py run --center center_001 --rpc-url http://127.0.0.1:8546 --duration-minutes 180`
   (Docker: `scripts\new-exam-center.cmd center_001`). It downloads the candidate list and refuses it unless it
   matches the on-chain hash, waits for the key release, verifies the payload against its on-chain hash, decrypts
   in memory, and opens at the on-chain start time.
5. Candidates open `http://<center server>:8080/`, log in, answer; timer is server-side; the exam closes at
   start + duration and unsubmitted sheets are submitted automatically.
6. Each submitted answer sheet is saved and SHA-256 hashed (candidate receipt). After the exam ends, ONE
   transaction records the Merkle root of all sheets (custody stage `ANSWERS_ROOT:<number of sheets>`, signed by the
   center device). Each candidate's receipt then shows the root and a Merkle proof for their sheet.
   Sheets stay on the center disk (`data/sheets/`).

`python exam_server.py roster ...` still exists as a demo shortcut (a center-made list) but is ignored unless the
server is started with `--allow-local-roster` AND the board has not anchored a list.

## Not included yet
- Grading (the board still holds the answer key; sheets record the chosen option's key in the generated variant).
- Proctoring, browser lockdown, candidate photo check, load testing beyond a few candidates.

## Grading (board side)
When the exam closes and the Merkle root is on-chain, the exam server sends all sheets, each with its Merkle proof,
to the board (`POST /sheets/<center>`), signed with the center device's key. The board
1. checks the signature is from the device enrolled for that center,
2. recomputes each sheet's SHA-256 and checks its proof against the root on-chain (`ANSWERS_ROOT`),
3. scores only proven sheets against the answer key (+4 / -1 / 0; `GRADE_CORRECT` and `GRADE_WRONG` change it).
The board page shows "Expected" (the sheet count the center committed to on-chain) next to "Received", so a
center cannot silently drop a candidate's sheet. Exams recorded with the older per-candidate hashes
(`ANSWERS:<candidate>`) are still graded.

Honest limits: between a candidate's submit and the exam end the sheet exists only at the center (the candidate's
receipt is what proves it later), and a center could still refuse to send a sheet; the board then sees expected
> received.
