# NEET Integrity Chain — Project Status

## Storage (blobs, keys, records)

Three separate places, so that stealing one is not enough:

| What | Where | Form |
|---|---|---|
| Encrypted blobs | Cloudflare R2 bucket (or `backend/data/blobs/` if R2 isn't configured) | ciphertext |
| AES keys + answer keys | `backend/data/keys.db` (own database) | AES-GCM ciphertext under the master key |
| Records (variant, fingerprint, blob hash, object key, exam start) | `backend/data/board.db` | plain, nothing secret |

The **master key** is the `BOARD_MASTER_KEY` environment variable and lives outside all
of the above. No plaintext PDF, variant JSON or raw AES key is written to disk any more.
In production the master key would sit in a KMS/HSM.

One-time setup:

```bash
cd backend
copy .env.example .env          # (Linux/macOS: cp .env.example .env)
python -c "import secrets; print(secrets.token_hex(32))"   # paste into BOARD_MASTER_KEY in .env
```

Cloudflare R2: create a bucket, create an API token with *Object Read & Write* on it,
and put the account ID, access key ID, secret access key and bucket name into `.env`.
Add these lines to your `.gitignore`:

```
.env
backend/.env
backend/data/
```

Then start the API and open **http://localhost:8000/storage-status**. `"ready": true` means
the bucket is reachable and the master key is set and matches the stored keys.
Leave the four `R2_*` values empty to use the local folder instead (the page says which
backend is active). Partial R2 settings are an error, not a silent fallback.

Keys are destroyed at release: once the wrapped key is on-chain, the board erases its stored
copy (keys.db keeps a tombstone with the time). The contract never receives a raw AES key.

The standalone scripts in `paper-generation/` and `encryption/` still work but are no longer
used by the app; it generates everything through `/generate`.

## How it works

```
board:    generate  -> blob in the bucket, AES key (encrypted) in keys.db, record in board.db
printer:  init      -> its own chain key + RSA key, enrollment file, fingerprint (one printer PER center)
board:    enroll    -> that printer's address + public key attached to the center
                       (the fingerprint on the board page is checked against the printer, in person)
board:    register  -> blob hash, variant, exam start, printer address, expected copies go on-chain
                       (NO key on-chain)
board:    release   -> AES key wrapped to THAT center's printer key, posted on-chain; the contract
                       refuses before the exam start time; the board then destroys its stored key
printer:  run       -> already waiting; sees the release, downloads the encrypted blob, checks it
                       against the on-chain hash, unwraps the key with its private key, decrypts in
                       memory, prints in batches. Before each batch the new cumulative count is
                       reported on-chain (reportPrintedCount: only that center's printer, only after
                       release, never decreasing, never above the expected copies)
```

The server never produces a decrypted paper: there is no `/paper` endpoint, and the center page
only shows status. Each printer holds the only key that can unwrap its own center's key.

### Run it (local chain)

```bash
npx hardhat compile                                        # (only if the contract changed)
npx hardhat node                                           # terminal 1 (leave running)
npx hardhat run contracts/deploy.js --network localhost    # terminal 2, once per node start
cd backend
python3 -m pip install -r requirements.txt
uvicorn app:app --reload                                   # API + pages on http://localhost:8000
```

Then, for each center (after generating on the board page):

```bash
cd printer-agent
python3 -m pip install -r requirements.txt
python3 printer_agent.py init --center center_001          # prints address + fingerprint
python3 printer_agent.py run  --center center_001          # leave running: it waits for the release
```

On the board page: **Generate** -> **Enroll printers...** (upload `devices/<center>/enrollment_<center>.json`)
-> check the fingerprint matches the printer's -> set copies -> **Register** -> after the exam
start (local chain: **Fast-forward**) -> **Release key**. The agent prints on its own and the
board and center pages show printed / expected live.

### Printing modes (`printer_agent.py run`)

| Flag | Behaviour |
|---|---|
| *(none)* | simulated: decrypts in memory, "prints" to the console, counts are recorded on-chain |
| `--print` | real printing to the default printer: `lp` (CUPS) on Linux / Raspberry Pi, SumatraPDF on Windows (`SUMATRA_PDF_EXE` in `printer-agent/.env`); `--printer NAME` picks a printer |
| `--print-cmd CMD` | custom command; `{copies}` = batch size; PDF piped to stdin, or written to a temp file only while the command runs if the template contains `{file}` |
| `--dry-run` | verify the blob hash, unwrap and decrypt in memory, then stop: nothing printed, nothing recorded |
| `--batch N` | copies per batch (default 25) |

The PDF is never written to disk, except the temporary file SumatraPDF needs while it runs
(overwritten and deleted afterwards). Print spoolers may keep their own copy, so the honest claim
is "no digital copy kept by our software". The count is recorded before each batch prints, so the
ledger is never behind the paper; if the print command fails, the agent stops and says the ledger
now counts copies that may not exist, to be reconciled with the printer's page counter. Restarting
the agent resumes from the on-chain count.

`python3 printer_agent.py fund-local --center center_001` sends 0.1 test ETH to a printer (Hardhat only).
The board normally funds a printer when it is enrolled.

### Keys on the printer

`printer-agent/devices/<center>/` holds `chain_key.hex` (signs the printer's transactions) and
`wrap_private.pem` (unwraps the AES key), mode 600, separate from the board's configuration. A real
controller would keep them in a TPM or secure element. Copy `printer-agent/` to the Raspberry Pi
(plus `backend/contract_deployment.json`, or pass `--deployment`), run `init` there, and the keys never leave it.

### Settings

`backend/.env` (board): `BOARD_MASTER_KEY` (see Storage); `RPC_URL`; `BOARD_PRIVATE_KEY` (the account that
deployed the contract: when set, all board transactions are signed locally, required on Sepolia);
`PRINTER_FUNDING_ETH` (ETH sent to a printer at enrollment: 0.5 on the local node, 0 when
`BOARD_PRIVATE_KEY` is set).

`printer-agent/.env` (printer): `SUMATRA_PDF_EXE`, `RPC_URL`, `BACKEND_API_URL` (see `.env.example`).
The agent also takes `--rpc-url`, `--api-url` and `--deployment`.

### Upgrading from the earlier shared-printer version

Delete: `backend/printer_setup.py`, `backend/printer_agent.py`, `backend/printer_identity/`,
`backend/printer_output/` (plaintext PDFs), `encryption/keys_store/` (plaintext AES keys), and the
`PRINTER_*` / `EXPECTED_COPIES` lines in `backend/.env`. Centers registered under the old setup are bound
to the shared printer and cannot be re-enrolled: restart the Hardhat node, redeploy, and clear
`backend/data/` (or just generate a new batch). The old printer's private keys were shared outside this
machine, so never fund that address on a real network; use `init` to create fresh identities.

## What's built

- **contracts/PaperCustody.sol**: registration (no key), time-locked release storing the wrapped key,
  `reportPrintedCount` (printer only, after release, monotonic, capped), `getCenterState`, optional custody log.
- **backend/**: `app.py` (API), `pipeline.py` (shuffle, render, encrypt, store), `storage.py` (R2 /
  S3-compatible / local blobs), `keystore.py` (keys.db), `db.py` (board.db), `keywrap.py` (RSA-OAEP wrapping +
  fingerprints), `web3_client.py` (chain client, local signing), `config.py`.
- **printer-agent/printer_agent.py**: `init`, `run`, `fund-local`.
- **frontend/**: `board.html`, `center.html`, `index.html`.
- `paper-generation/` and `encryption/` hold the original standalone scripts (no longer used by the app).

## Not built yet

- `leak-detection/` (scraper, OCR, fingerprint matching). The variant fingerprints it needs are in board.db.
- Sepolia deployment: add a `sepolia` network to the Hardhat config, deploy with `--network sepolia`, set
  `RPC_URL` + `BOARD_PRIVATE_KEY`, give the agent `--rpc-url`. On a real chain there is no fast-forward:
  the time-lock follows real time, blocks take ~12 s, and the board and printers need test ETH.
- Not covered: a compromised printer can still misreport counts or leak what it legitimately decrypts
  (the variants and leak detection address that); a board that hands the key out off-chain early;
  enrollment is only as trustworthy as the in-person fingerprint check; no login on the board page.

## Testing notes

Exercised end to end on a local Hardhat node with this contract: per-center enrollment and copies,
early release refused, release + key destruction, agents printing on their own with on-chain counts,
wrong printer / wrong key / tampered blob / over-cap / decreasing count / unauthorized callers all refused,
dry run, `lp` via stdin, `{file}` and stdin templates, a failing printer then resume, signed and unlocked-node
board modes, and the board and center pages in a simulated browser. A real browser, a real printer and
Sepolia have not been tried.

## Smart contract setup (once you're ready)

```bash
npm install --save-dev hardhat
npx hardhat init          # choose "Create a JavaScript project"
# copy PaperCustody.sol into the contracts/ folder Hardhat expects
npx hardhat compile
npx hardhat node                                          # terminal 1
npx hardhat run contracts/deploy.js --network localhost   # terminal 2
```

## Key design decisions (for your report)

- **No real NEET PDF parsing.** Deliberately skipped — fragile, not where
  the project's value is. Mock paper used instead.
- **No diagram support.** Text-only `question + 4 options` schema for scope
  reasons; diagram support would need an `image_refs` field, described but
  not implemented.
- **Keys and blobs stored separately** — a database breach of one doesn't
  compromise the other.
- **AES-GCM, not plain AES** — gives tamper-detection for free via its
  built-in authentication tag.

## Vault + Docker demo (new)

This project now includes a **local demonstration** of Vault-backed board secrets and isolated
printer containers. It is designed for Docker Desktop on Windows as well as Docker Engine. It does
not turn a laptop into production-grade hardware security: the Vault service below runs in **dev
mode**, and its demo root token is not suitable for production. Use a persistent, initialized Vault
with TLS, least-privilege policies, and managed unseal/recovery for a real deployment.

### What runs where

- `vault`: local Vault KV v2 at `http://127.0.0.1:8200`.
- `backend`: FastAPI board API. With `SECRET_PROVIDER=vault`, it reads the board master key,
  Ethereum board signing key, RPC URL and optional S3/R2 credentials from `secret/neet/board`.
- `printer-center-001` through `printer-center-004`: separate containers and separate named Docker
  volumes. Each container only mounts its own identity volume. Public enrollment JSON is exported to
  `printer-agent/enrollments/`; private keys are not exported.

### First-time setup (Windows CMD)

1. Copy the example settings and edit them locally:

   ```cmd
   copy .env.example .env
   copy backend\.env.example backend\.env
   ```

   In the root `.env`, keep the demo `VAULT_TOKEN` consistent with the Compose default and set
   `PRINTER_RPC_URL` to the RPC endpoint used by the printer agents. For local Hardhat from inside
   Docker, use `http://host.docker.internal:8545` for both board `RPC_URL` and `PRINTER_RPC_URL`;
   for Sepolia, use your Sepolia RPC URL. Do not commit a real API-key URL. In `backend/.env`, set
   the **same existing** `BOARD_MASTER_KEY` that encrypted your current
   `backend/data/keys.db`, your board wallet's `BOARD_PRIVATE_KEY`, and `SEPOLIA_RPC_URL` (or `RPC_URL`).
   Add any S3/R2 credentials there if you use remote blob storage. Do not paste secrets into chat.

2. Start only Vault:

   ```cmd
   docker compose up -d vault
   ```

3. Install `requests` if needed, then migrate the non-empty secrets from the two local `.env` files:

   ```cmd
   python -m pip install requests
   python scripts\migrate_secrets_to_vault.py
   ```

   The script writes to Vault KV v2 at `secret/neet/board`, verifies the stored secret names, and
   never prints their values. It does not delete or alter local `.env` files. If your Vault token or
   address is different, set `VAULT_TOKEN` / `VAULT_ADDR` in the process environment before running it.

4. Once the migration script succeeds, remove migrated secret values from `backend/.env` and keep
   only non-secret settings there if you use native local runs. The Compose backend explicitly uses
   Vault. **Do not change `BOARD_MASTER_KEY`** when using an existing `keys.db`; changing it makes
   existing encrypted key rows unreadable.

5. Build and start the backend:

   ```cmd
   docker compose up --build -d backend
   ```

   Open `http://localhost:8000/storage-status` and check that the key store is ready. The first
   Compose start creates a persistent local `backend/data` folder if it does not already exist.

6. Start printer containers only after checking their identity/enrollment situation:

   ```cmd
   docker compose up --build printer-center-001 printer-center-002 printer-center-003 printer-center-004
   ```

   Each container copies a supplied identity into its own named volume on first start. If no private
   identity is supplied, it generates a new one and writes only the **public** enrollment JSON to
   `printer-agent/enrollments/enrollment_center_NNN.json`. Upload that JSON through the board UI and
   verify its fingerprint before registering a new center.

### Important: preserving existing Sepolia registrations

A registered on-chain center is bound to its original printer address. The clean project archive
intentionally excludes `.env` files, database files and printer private-key files. To preserve an
existing local demo's generated papers and encrypted key rows, privately restore your own
`backend/data/` directory as well as the matching `BOARD_MASTER_KEY` into Vault. If you need to keep
using an already-registered Sepolia center, restore that center's existing `chain_key.hex` and
`wrap_private.pem` from your own private local backup into
`printer-agent/devices/<center_id>/` **before starting its container**. The compose entrypoint imports
those files once into that center's private Docker volume. Do not generate a replacement identity for
an already-registered center: its address will not match the contract. If you don't have the old keys,
use a fresh center ID / clean test deployment and enroll a new identity.

The local bind-mounted seed directory is only an import path; the running agent uses its dedicated
named volume. Docker containers on the same host still share the host kernel and are not equivalent
to separate physical devices, TPMs, secure elements or HSMs. Also, the demo Vault root token is shared
with the board container for simplicity, so it demonstrates centralized secret retrieval rather than
production-grade Vault policy isolation.

### Useful commands

```cmd
docker compose ps
docker compose logs -f backend
docker compose logs -f printer-center-001
docker compose down
```

`docker compose down` preserves named volumes. To deliberately remove the printer identity volumes,
use `docker compose down -v` only if you understand that this deletes the container-held identities.
