"""Metrics in the Prometheus text format, computed from the database when asked.

The API and the worker are separate processes, so counters kept in memory would each show only half the story.
The database sees everything, so the numbers are read from there.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CircuitState, Delivery, DeliveryAttempt, DeliveryStatus, Endpoint, Event


def render_metrics(session: Session, now: datetime) -> str:
    lines: list[str] = []

    def add(name: str, help_text: str, kind: str, samples: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value}")

    by_status = dict(session.execute(select(Delivery.status, func.count()).group_by(Delivery.status)).all())
    add(
        "webhook_deliveries",
        "Deliveries by status (dead = dead-letter queue)",
        "gauge",
        [
            (f'{{status="{s}"}}', by_status.get(s, 0))
            for s in (DeliveryStatus.PENDING, DeliveryStatus.SUCCEEDED, DeliveryStatus.DEAD)
        ],
    )

    oldest_due = session.scalar(
        select(func.min(Delivery.next_attempt_at)).where(
            Delivery.status == DeliveryStatus.PENDING, Delivery.next_attempt_at <= now
        )
    )
    lag = max(0.0, (now - oldest_due).total_seconds()) if oldest_due else 0.0
    add(
        "webhook_queue_lag_seconds",
        "How long the oldest due delivery has been waiting (0 when the queue is keeping up)",
        "gauge",
        [("", round(lag, 3))],
    )

    by_circuit = dict(
        session.execute(select(Endpoint.circuit_state, func.count()).group_by(Endpoint.circuit_state)).all()
    )
    add(
        "webhook_endpoints",
        "Endpoints by circuit-breaker state",
        "gauge",
        [
            (f'{{circuit="{s}"}}', by_circuit.get(s, 0))
            for s in (CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN)
        ],
    )

    hour_ago = now - timedelta(hours=1)
    attempts = dict(
        session.execute(
            select(DeliveryAttempt.outcome, func.count())
            .where(DeliveryAttempt.started_at >= hour_ago)
            .group_by(DeliveryAttempt.outcome)
        ).all()
    )
    add(
        "webhook_attempts_last_hour",
        "Delivery attempts in the last hour by outcome",
        "gauge",
        [(f'{{outcome="{o}"}}', c) for o, c in sorted(attempts.items())],
    )

    events = session.scalar(select(func.count()).select_from(Event))
    add("webhook_events", "Total events stored", "gauge", [("", events)])

    return "\n".join(lines) + "\n"
