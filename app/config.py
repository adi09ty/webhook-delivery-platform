"""Settings, read from environment variables that start with WHP_ (or from a .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WHP_", env_file=".env", extra="ignore")

    # Storage. SQLite is only for quick local runs and tests; use PostgreSQL for real.
    database_url: str = "sqlite:///./webhooks.db"

    # Protects the /admin and /metrics endpoints. If it is not set, those endpoints are switched off.
    admin_token: str | None = None

    # Refuse to deliver to private/loopback addresses (stops customers from aiming the platform at your
    # internal network). Switch on only for local demos, where the receiver lives on a private address.
    allow_private_targets: bool = False

    # Retries: how many seconds to wait after the 1st, 2nd, ... failed attempt. After the last delay is used up
    # and one more attempt fails, the delivery is declared dead (dead-letter queue).
    retry_delays_seconds: list[int] = [30, 120, 600, 3600, 21600]
    retry_jitter: float = 0.1  # +/-10% random spread so retries do not all land at the same moment
    max_retry_after_seconds: int = 3600  # upper limit when a receiver sends a Retry-After header

    # Sending
    request_timeout_seconds: float = 10.0
    max_response_snippet: int = 1000  # how much of a receiver's answer we keep in the delivery log

    # Worker
    lease_seconds: int = 60  # a claimed delivery is "reserved" this long; if the worker dies it is picked up again
    worker_batch_size: int = 10
    worker_concurrency: int = 4
    worker_poll_seconds: float = 1.0

    # Circuit breaker (per endpoint)
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: int = 300
    circuit_probe_window_seconds: int = 30

    # Rate limiting (per endpoint)
    default_rate_limit_per_minute: int = 60
    rate_limit_defer_seconds: int = 5

    # API limits
    max_payload_bytes: int = 256_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
