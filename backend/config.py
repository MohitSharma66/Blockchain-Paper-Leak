"""
config.py

Loads settings from a .env file (backend/.env, then <project>/.env) into the
process environment. Real environment variables always win over the file.

Supported format: one KEY=VALUE per line, '#' comment lines, optional quotes.
(No inline comments, no 'export'.)

Never commit .env: it holds the master key and the R2 credentials.
"""

import os
import threading
from pathlib import Path

import requests

BACKEND_DIR = Path(__file__).parent
PROJECT_ROOT = BACKEND_DIR.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


for _p in (BACKEND_DIR / ".env", PROJECT_ROOT / ".env"):
    _load_dotenv(_p)

# Where board.db, keys.db and (if R2 isn't configured) the local blob fallback live.
DATA_DIR = Path(os.environ.get("NEET_DATA_DIR") or (BACKEND_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(DATA_DIR, 0o700)  # best effort; ignored on Windows
except OSError:
    pass


# Secrets that may be loaded from Vault KV v2. Non-secret runtime settings stay in env.
VAULT_SECRET_NAMES = {
    "BOARD_MASTER_KEY", "BOARD_PRIVATE_KEY", "RPC_URL", "SEPOLIA_RPC_URL",
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
    "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION",
}
_VAULT_CACHE: dict[str, str] = {}
_VAULT_LOCK = threading.Lock()


def _vault_secret(name: str) -> str:
    """Read a secret from Vault KV v2. Values are cached for this process lifetime."""
    with _VAULT_LOCK:
        if name in _VAULT_CACHE:
            return _VAULT_CACHE[name]
        addr = (os.environ.get("VAULT_ADDR") or "").strip().rstrip("/")
        token = (os.environ.get("VAULT_TOKEN") or "").strip()
        path = (os.environ.get("VAULT_KV_PATH") or "neet/board").strip("/")
        if not addr or not token:
            raise RuntimeError(
                f"SECRET_PROVIDER=vault but VAULT_ADDR/VAULT_TOKEN is missing (requested {name})."
            )
        url = f"{addr}/v1/secret/data/{path}"
        try:
            response = requests.get(
                url, headers={"X-Vault-Token": token}, timeout=3
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Vault is unavailable while reading {name}: {exc}") from exc
        if response.status_code == 404:
            raise RuntimeError(
                f"Vault secret path secret/{path} is missing. Run scripts/migrate_secrets_to_vault.py."
            )
        if response.status_code == 403:
            raise RuntimeError(f"Vault denied access to secret/{path}; check the Vault token policy.")
        response.raise_for_status()
        try:
            value = response.json()["data"]["data"][name]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Vault secret secret/{path} does not contain {name}.") from exc
        result = str(value).strip()
        _VAULT_CACHE[name] = result
        return result


def env(name: str) -> str:
    """Read runtime configuration; sensitive values can be sourced from Vault KV v2."""
    provider = (os.environ.get("SECRET_PROVIDER") or "env").strip().lower()
    if provider == "vault" and name in VAULT_SECRET_NAMES:
        return _vault_secret(name)
    return (os.environ.get(name) or "").strip()
