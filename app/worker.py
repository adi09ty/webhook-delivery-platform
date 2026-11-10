"""The background worker: run it as its own process with   python -m app.worker

It repeatedly claims due deliveries and sends them. You can run several workers at once; the database makes
sure they never take the same delivery. Stop it with Ctrl+C: it finishes the deliveries it is working on first.
"""

import argparse
import logging
import signal
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, wait

import httpx

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.delivery import DeliveryService

log = logging.getLogger("worker")


def _safe_process(service: DeliveryService, delivery_id: uuid.UUID) -> str:
    try:
        return service.process(delivery_id)
    except Exception:
        # Do not crash the worker. The delivery's reservation will expire and it will be picked up again.
        log.exception("Unexpected error while processing delivery %s", delivery_id)
        return "error"


def run_once(service: DeliveryService, batch_size: int) -> int:
    """Claim one batch and process it one by one. Returns how many deliveries were handled."""
    delivery_ids = service.claim_batch(batch_size)
    for delivery_id in delivery_ids:
        _safe_process(service, delivery_id)
    return len(delivery_ids)


def run_forever(service: DeliveryService, settings: Settings, stop: threading.Event) -> None:
    log.info("Worker started (batch=%s, concurrency=%s)", settings.worker_batch_size, settings.worker_concurrency)
    with ThreadPoolExecutor(max_workers=settings.worker_concurrency) as pool:
        while not stop.is_set():
            delivery_ids = service.claim_batch(settings.worker_batch_size)
            if not delivery_ids:
                stop.wait(settings.worker_poll_seconds)
                continue
            futures = [pool.submit(_safe_process, service, delivery_id) for delivery_id in delivery_ids]
            wait(futures)
    log.info("Worker stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Webhook delivery worker")
    parser.add_argument("--once", action="store_true", help="process one batch of due deliveries and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    engine = make_engine(settings.database_url)
    service = DeliveryService(make_session_factory(engine), settings, httpx.Client())

    if args.once:
        handled = run_once(service, settings.worker_batch_size)
        log.info("Handled %s deliveries", handled)
        return

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_forever(service, settings, stop)


if __name__ == "__main__":
    main()
