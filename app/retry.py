"""The retry schedule."""

import random


def next_retry_delay(
    attempts_made: int,
    delays: list[int],
    jitter: float,
    rng: random.Random | None = None,
) -> float | None:
    """Seconds to wait before the next try, or None when the retries are used up.

    attempts_made counts the tries already done (1 after the first failure).
    The delay grows with every failure (exponential back-off, here a configured list), and a little random
    "jitter" is added so that thousands of failed deliveries do not all come back at the same second.
    """
    if attempts_made < 1 or attempts_made > len(delays):
        return None
    base = delays[attempts_made - 1]
    generator = rng or random
    return base * (1 + generator.uniform(-jitter, jitter))
