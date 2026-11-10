"""API keys and signing secrets."""

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiKey

API_KEY_MARKER = "whp_"
API_KEY_PREFIX_LENGTH = 12  # "whp_" plus the first 8 random characters, used only to find the right row fast


def hash_api_key(key: str) -> str:
    # A fast hash is fine here: API keys are long random values, unlike human passwords.
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (full key, prefix, hash). Only the prefix and hash are stored; the full key is shown once."""
    key = API_KEY_MARKER + secrets.token_urlsafe(32)
    return key, key[:API_KEY_PREFIX_LENGTH], hash_api_key(key)


def generate_signing_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def authenticate_api_key(session: Session, raw_key: str) -> ApiKey | None:
    """Find the live API key matching `raw_key`, or None."""
    if not raw_key.startswith(API_KEY_MARKER) or len(raw_key) < 24:
        return None
    candidates = session.scalars(
        select(ApiKey).where(ApiKey.key_prefix == raw_key[:API_KEY_PREFIX_LENGTH], ApiKey.revoked_at.is_(None))
    ).all()
    wanted = hash_api_key(raw_key)
    for candidate in candidates:
        # compare_digest takes the same time however many characters match, so timing reveals nothing
        if hmac.compare_digest(candidate.key_hash, wanted):
            return candidate
    return None
