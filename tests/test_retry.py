import random

from app.retry import next_retry_delay

DELAYS = [30, 120, 600]


def test_each_failure_waits_longer_than_the_one_before():
    waits = [next_retry_delay(n, DELAYS, 0.0) for n in (1, 2, 3)]
    assert waits == [30, 120, 600]


def test_no_more_retries_after_the_schedule_is_used_up():
    assert next_retry_delay(4, DELAYS, 0.0) is None
    assert next_retry_delay(99, DELAYS, 0.0) is None


def test_a_nonsense_attempt_count_gives_no_retry():
    assert next_retry_delay(0, DELAYS, 0.0) is None
    assert next_retry_delay(-1, DELAYS, 0.0) is None


def test_jitter_stays_within_the_requested_band():
    rng = random.Random(42)
    waits = [next_retry_delay(2, DELAYS, 0.1, rng) for _ in range(500)]
    assert all(108 <= w <= 132 for w in waits)
    assert len(set(waits)) > 100  # really random, not constant


def test_an_empty_schedule_never_retries():
    assert next_retry_delay(1, [], 0.1) is None
