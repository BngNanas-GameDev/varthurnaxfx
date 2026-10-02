"""API key loading / validation / rotation helpers.

- Loads from env (or vault path when VAULT_* env is set).
- Validates permission shape (trade-only: no withdraw key material present,
  live trading explicitly enabled only when ALLOW_LIVE=true).
- mask_key() for safe logging. NEVER print/log raw secrets.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ROTATION_INTERVAL_DAYS = 90


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    testnet: bool
    rotation_due_in_days: int = ROTATION_INTERVAL_DAYS


def mask_key(key: str, keep: int = 4) -> str:
    """Return safely loggable form, e.g. 'AB12...WXYZ'. Empty -> '(missing)'."""
    if not key:
        return "(missing)"
    key = str(key)
    if len(key) <= keep * 2:
        return "*" * len(key)
    return f"{key[:keep]}...{key[-keep:]}"


def load_keys() -> Credentials:
    """Load credentials from env (BINANCE_API_KEY / BINANCE_API_SECRET).

    Optional vault: if VAULT_ADDR + VAULT_SECRET_PATH are set, note it in
    logs (no secret material is ever logged) and still read from env, which
    the vault agent is expected to populate.
    """
    if os.getenv("VAULT_ADDR") and os.getenv("VAULT_SECRET_PATH"):
        logger.info("vault configured (addr set, path set); reading injected env vars")
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    testnet = os.getenv("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes")
    rotation_ts = float(os.getenv("KEY_ROTATED_AT", "0") or 0)
    if rotation_ts > 0:
        age_days = (time.time() - rotation_ts) / 86400
        due_in = max(0, int(ROTATION_INTERVAL_DAYS - age_days))
    else:
        due_in = 0  # unknown rotation age -> treat as due
    # Log only masked form.
    logger.info("loaded keys key=%s testnet=%s", mask_key(api_key), testnet)
    return Credentials(api_key=api_key, api_secret=api_secret,
                       testnet=testnet, rotation_due_in_days=due_in)


def validate_permissions(creds: Credentials) -> tuple[bool, str]:
    """Check key permission posture without ever exposing secrets.

    Rules:
    - key + secret must be present
    - BINANCE_WITHDRAW_ENABLED must NOT be true (trade-only key)
    - live (testnet=false) additionally requires ALLOW_LIVE=true
    """
    if not creds.api_key or not creds.api_secret:
        return False, "missing BINANCE_API_KEY / BINANCE_API_SECRET"
    if os.getenv("BINANCE_WITHDRAW_ENABLED", "false").lower() in ("1", "true", "yes"):
        return False, "withdraw permission detected (trade-only key required)"
    if not creds.testnet and os.getenv("ALLOW_LIVE", "false").lower() not in ("1", "true", "yes"):
        return False, "live trading blocked: set ALLOW_LIVE=true explicitly"
    return True, "ok"


def rotation_reminder(creds: Credentials) -> str:
    if creds.rotation_due_in_days <= 0:
        return "ROTATE KEYS NOW: rotation due (90d policy). See runbooks/GO_LIVE.md."
    if creds.rotation_due_in_days <= 14:
        return f"key rotation due in {creds.rotation_due_in_days}d; schedule rotation."
    return f"key rotation ok ({creds.rotation_due_in_days}d remaining)."
