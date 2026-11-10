"""HMAC request signing, in the style of the "Standard Webhooks" convention used by Stripe-like services.

What the receiver gets:
    webhook-id         the event id (the same on every retry, so the receiver can ignore duplicates)
    webhook-timestamp  when this attempt was signed (seconds since 1970)
    webhook-signature  "v1,<base64 HMAC-SHA256>" of  "<id>.<timestamp>.<body>"  made with the endpoint secret

Why it works: only the platform and the receiver know the secret, so a valid signature proves the message came
from the platform and was not changed. The timestamp lets the receiver reject old copies (replay attacks).
"""

import base64
import hashlib
import hmac
import time

DEFAULT_TOLERANCE_SECONDS = 300


def compute_signature(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    signed_content = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed_content, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def build_signature_headers(secret: str, message_id: str, timestamp: int, body: bytes) -> dict[str, str]:
    return {
        "webhook-id": message_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": compute_signature(secret, message_id, timestamp, body),
    }


def verify_signature(
    secret: str,
    message_id: str,
    timestamp_header: str,
    signature_header: str,
    body: bytes,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """What a receiver should do. Returns False for a wrong, missing or too-old signature."""
    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = compute_signature(secret, message_id, timestamp, body)
    # The header may hold several space-separated signatures (used while a secret is being rotated).
    return any(hmac.compare_digest(candidate, expected) for candidate in (signature_header or "").split())
