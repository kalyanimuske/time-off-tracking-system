from datetime import date

import pytest

from app.domain.models import RequestedDay
from app.domain.errors import ValidationError
from tests.helpers import ADMIN, DAY_8H, MONTHLY_5_DAYS, assign, create_policy

JAN_1 = date(2027, 1, 1)


@pytest.fixture
def year_of_accruals(services):
    def _make(carryover_cap_hours):
        policy = create_policy(services, {**MONTHLY_5_DAYS, "carryover_cap_hours": carryover_cap_hours})
        assign(services, policy, "alice")
        services.accruals.run_time_based_accruals(as_of=date(2026, 12, 31))  # 60 days
        return policy

    return _make


def test_balance_above_carryover_cap_expires(services, year_of_accruals):
    policy = year_of_accruals(40)  # keep up to 5 days

    [txn] = services.year_end.run(policy.id, 2026, run_date=JAN_1)

    assert txn.amount_minutes == -(60 * DAY_8H - 40 * 60)
    assert services.ledger.settled_balance("alice", policy.id) == 40 * 60


def test_year_end_is_idempotent(services, year_of_accruals):
    policy = year_of_accruals(40)
    services.year_end.run(policy.id, 2026, run_date=JAN_1)
    assert services.year_end.run(policy.id, 2026, run_date=JAN_1) == []


def test_use_it_or_lose_it(services, year_of_accruals):
    policy = year_of_accruals(0)
    services.year_end.run(policy.id, 2026, run_date=JAN_1)
    assert services.ledger.settled_balance("alice", policy.id) == 0


def test_no_cap_means_everything_carries_over(services, year_of_accruals):
    policy = year_of_accruals(None)
    assert services.year_end.run(policy.id, 2026, run_date=JAN_1) == []


def test_time_off_in_january_comes_from_next_years_balance(services, year_of_accruals):
    policy = year_of_accruals(40)
    req = services.requests.create_request(
        employee_id="alice", policy_id=policy.id, reason="Ski trip", requested_days=[RequestedDay(date(2027, 1, 4))]
    )
    services.requests.approve(req.id, ADMIN)

    [txn] = services.year_end.run(policy.id, 2026, run_date=JAN_1)

    # The January day did not reduce what expired in 2026.
    assert txn.metadata["balance_before"] == 60 * DAY_8H
    assert services.ledger.settled_balance("alice", policy.id) == 40 * 60 - DAY_8H


def test_year_end_cannot_run_before_the_year_is_over(services, year_of_accruals):
    policy = year_of_accruals(40)
    with pytest.raises(ValidationError, match="isn't over yet"):
        services.year_end.run(policy.id, 2026, run_date=date(2026, 12, 31))
