from __future__ import annotations

from datetime import date, timedelta

from app.domain.models import RequestedDay
from app.services import Services

ADMIN = "admin_1"
COMPANY = "acme"

MONTHLY_5_DAYS = {"accrual_method": "time_based", "frequency": "monthly", "rate_amount": 5, "rate_unit": "days"}

# Minutes in one working day
DAY_8H = 8 * 60
DAY_6H = 6 * 60


def create_policy(services: Services, settings: dict, *, name="Vacation", time_off_type="vacation", effective_from=date(2026, 1, 1)):
    policy, _ = services.policies.create_policy(
        company_id=COMPANY,
        name=name,
        time_off_type=time_off_type,
        settings=settings,
        effective_from=effective_from,
        actor_id=ADMIN,
    )
    return policy


def assign(services: Services, policy, employee_id: str, start=date(2026, 1, 1)):
    return services.policies.assign_employee(policy.id, employee_id, start, actor_id=ADMIN)


def days(*dates: date) -> list[RequestedDay]:
    return [RequestedDay(day=d) for d in dates]


def weekdays(start: date, count: int) -> list[RequestedDay]:
    result, current = [], start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(RequestedDay(day=current))
        current += timedelta(days=1)
    return result
