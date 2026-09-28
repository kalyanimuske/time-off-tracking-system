"""The on_payroll_processed event, and a tiny in-process event bus.

The prompt says we can "subscribe" to on_payroll_processed. In production that
would be a message queue (e.g. SQS or Kafka) between two services. Here it's
an in-process publish/subscribe with the same idea: the Payroll Service
publishes, the Time Off Service's accrual engine is subscribed, and neither
knows about the other's internals.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

ON_PAYROLL_PROCESSED = "on_payroll_processed"


@dataclass
class PayrollEntry:
    employee_id: str
    hours_worked: float


@dataclass
class PayrollProcessedEvent:
    run_id: str  # unique per payroll run; the same run re-processed keeps its id
    company_id: str
    period_start: date
    period_end: date
    entries: list[PayrollEntry] = field(default_factory=list)


@dataclass
class DeliveredEvent:
    name: str
    run_id: str
    subscribers: int
    delivered_at: datetime


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], Any]]] = defaultdict(list)
        self.log: list[DeliveredEvent] = []

    def subscribe(self, event_name: str, handler: Callable[[Any], Any]) -> None:
        self._subscribers[event_name].append(handler)

    def publish(self, event_name: str, event: Any) -> list[Any]:
        """Deliver to every subscriber and return what each one returned."""
        handlers = self._subscribers[event_name]
        results = [handler(event) for handler in handlers]
        self.log.append(
            DeliveredEvent(
                name=event_name,
                run_id=getattr(event, "run_id", ""),
                subscribers=len(handlers),
                delivered_at=datetime.now(timezone.utc),
            )
        )
        return results
