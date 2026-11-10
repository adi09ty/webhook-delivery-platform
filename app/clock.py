"""One place to ask "what time is it?", so tests can replace it with a clock they control."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
