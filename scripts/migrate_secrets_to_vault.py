#!/usr/bin/env python3
"""Copy recognized board secrets from local .env files into Vault KV v2.

This script prints secret names only, never values. It intentionally does not delete local .env files.
Run it once after Vault is running and before enabling SECRET_PROVIDER=vault.
"""
from pathlib import Path
import os
import sys
import requests

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = {
    "BOARD_MASTER_KEY", "BOARD_PRIVATE_KEY", "RPC_URL", "SEPOLIA_RPC_URL",
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
    "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION",
}


def read_env(path: Path, allowed: set[str] | None = None) -> dict[str, str]:
    result = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if (allowed is None or key in allowed) and value:
            result[key] = value
    return result


def main() -> int:
    values = {}
    # Root values first; backend/.env wins for overlapping settings.
    values.update(read_env(ROOT / ".env", SECRET_NAMES))
    values.update(read_env(ROOT / "backend" / ".env", SECRET_NAMES))
    if not values:
        print("No recognized non-empty secrets found in .env or backend/.env.", file=sys.stderr)
        return 2

    root_env = read_env(ROOT / ".env", {"VAULT_TOKEN"})
    token = os.environ.get("VAULT_TOKEN") or root_env.get("VAULT_TOKEN")
    addr = os.environ.get("VAULT_ADDR") or "http://127.0.0.1:8200"
    path = os.environ.get("VAULT_KV_PATH") or "neet/board"
    if not token:
        print("Set VAULT_TOKEN in the root .env (or process environment) first.", file=sys.stderr)
        return 2

    url = f"{addr.rstrip('/')}/v1/secret/data/{path.strip('/')}"
    try:
        response = requests.put(url, headers={"X-Vault-Token": token}, json={"data": values}, timeout=5)
        if response.status_code in (401, 403):
            print("Vault rejected the token. Check the token and KV v2 policy.", file=sys.stderr)
            return 3
        response.raise_for_status()
        verify = requests.get(url, headers={"X-Vault-Token": token}, timeout=5)
        verify.raise_for_status()
        stored = verify.json()["data"]["data"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"Vault write/verification failed: {exc}", file=sys.stderr)
        return 4

    missing = sorted(set(values) - set(stored))
    if missing:
        print("Vault verification did not find: " + ", ".join(missing), file=sys.stderr)
        return 5
    print(f"Stored and verified {len(values)} secret(s) at secret/{path}:")
    for name in sorted(values):
        print(f"  - {name}")
    print("Secret values were not printed. Local .env files were not changed.")
    print("After testing Vault reads, remove migrated secrets from local .env files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
