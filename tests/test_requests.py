from datetime import date

import pytest

from app.domain.errors import InsufficientBalanceError, InvalidTransitionError, ValidationError
from app.domain.models import RequestedDay, RequestStatus, TransactionType
from tests.helpers import ADMIN, DAY_6H, DAY_8H, MONTHLY_5_DAYS, assign, create_policy, days, weekdays

MON_MAR_9 = date(2026, 3, 9)
TUE_MAR_10 = date(2026, 3, 10)


@pytest.fixture
def vacation(services):
    """Alice has 15 days (Jan-Mar accruals) on a monthly 5-day policy."""
    policy = create_policy(services, MONTHLY_5_DAYS)
    assign(services, policy, "alice")
    services.accruals.run_time_based_accruals(as_of=date(2026, 3, 1))
    return policy


def request(services, policy, requested, employee_id="alice"):
    return services.requests.create_request(
        employee_id=employee_id, policy_id=policy.id, reason="Trip", requested_days=requested
    )


def test_creating_a_request_puts_a_hold_on_the_balance(services, vacation):
    req = request(services, vacation, days(MON_MAR_9, TUE_MAR_10))

    assert req.status == RequestStatus.PENDING
    assert req.total_minutes == 2 * DAY_8H
    assert services.ledger.settled_balance("alice", vacation.id) == 15 * DAY_8H
    assert services.ledger.available_balance("alice", vacation.id) == 13 * DAY_8H


def test_approving_deducts_the_balance(services, vacation):
    req = request(services, vacation, days(MON_MAR_9, TUE_MAR_10))

    services.requests.approve(req.id, ADMIN)

    assert services.ledger.settled_balance("alice", vacation.id) == 13 * DAY_8H
    assert services.ledger.pending_minutes("alice", vacation.id) == 0
    assert services.ledger.used_minutes("alice", vacation.id) == 2 * DAY_8H


def test_rejecting_releases_the_hold(services, vacation):
    req = request(services, vacation, days(MON_MAR_9, TUE_MAR_10))

    services.requests.reject(req.id, ADMIN, note="Busy week")

    assert services.ledger.available_balance("alice", vacation.id) == 15 * DAY_8H
    assert req.decision_note == "Busy week"


def test_weekends_and_holidays_are_not_deducted(services, vacation):
    services.holidays.add_holiday(company_id="acme", day=date(2026, 12, 25), name="Christmas", actor_id=ADMIN)
    # Thu Dec 24 -> Mon Dec 28: Fri is Christmas, then a weekend
    selected = [RequestedDay(day=date(2026, 12, d)) for d in range(24, 29)]

    preview = services.requests.preview("alice", vacation.id, selected)
    req = request(services, vacation, selected)

    assert req.total_minutes == 2 * DAY_8H
    assert preview.skipped_days == [date(2026, 12, 25), date(2026, 12, 26), date(2026, 12, 27)]


def test_request_with_only_holidays_and_weekends_is_rejected(services, vacation):
    with pytest.raises(ValidationError, match="weekends or holidays"):
        request(services, vacation, days(date(2026, 3, 7), date(2026, 3, 8)))


def test_partial_day_request(services, vacation):
    req = request(services, vacation, [RequestedDay(day=MON_MAR_9, minutes=4 * 60)])
    assert req.total_minutes == 240


def test_partial_day_longer_than_a_workday_is_rejected(services, vacation):
    with pytest.raises(ValidationError, match="partial days"):
        request(services, vacation, [RequestedDay(day=MON_MAR_9, minutes=10 * 60)])


def test_six_hour_employee_uses_six_hours_per_day(services, vacation):
    assign(services, vacation, "bob", start=date(2026, 8, 1))
    services.accruals.run_time_based_accruals(as_of=date(2026, 8, 3))

    req = request(services, vacation, days(date(2026, 8, 3)), employee_id="bob")

    assert req.total_minutes == DAY_6H


def test_not_enough_balance(services, vacation):
    with pytest.raises(InsufficientBalanceError, match="only 120h is available"):
        request(services, vacation, weekdays(MON_MAR_9, 16))


def test_negative_balance_allowed_up_to_the_limit(services):
    sick = create_policy(
        services,
        {"accrual_method": "time_based", "frequency": "monthly", "rate_amount": 1, "allow_negative": True, "max_negative_hours": 16},
        name="Sick",
        time_off_type="sick",
    )
    assign(services, sick, "alice")
    services.accruals.run_time_based_accruals(as_of=date(2026, 3, 1))  # 3 days

    ok = request(services, sick, weekdays(MON_MAR_9, 5))  # -2 days = -16h: allowed
    assert services.ledger.available_balance("alice", sick.id) == -16 * 60

    services.requests.cancel(ok.id, "alice", today=date(2026, 3, 1))
    with pytest.raises(InsufficientBalanceError, match="16h negative"):
        request(services, sick, weekdays(MON_MAR_9, 6))  # -3 days: too much


def test_overlapping_requests_are_rejected(services, vacation):
    request(services, vacation, days(MON_MAR_9))
    with pytest.raises(ValidationError, match="already requested"):
        request(services, vacation, days(MON_MAR_9, TUE_MAR_10))


def test_employee_cannot_approve_own_request(services, vacation):
    req = request(services, vacation, days(MON_MAR_9))
    with pytest.raises(ValidationError, match="own time off"):
        services.requests.approve(req.id, "alice")


def test_cannot_approve_a_rejected_request(services, vacation):
    req = request(services, vacation, days(MON_MAR_9))
    services.requests.reject(req.id, ADMIN)
    with pytest.raises(InvalidTransitionError):
        services.requests.approve(req.id, ADMIN)


def test_cancelling_approved_future_time_off_refunds_it(services, vacation):
    req = request(services, vacation, days(MON_MAR_9))
    services.requests.approve(req.id, ADMIN)

    services.requests.cancel(req.id, "alice", today=date(2026, 3, 1))

    assert req.status == RequestStatus.CANCELLED
    assert services.ledger.settled_balance("alice", vacation.id) == 15 * DAY_8H


def test_cancelling_time_off_that_already_started_needs_an_admin(services, vacation):
    req = request(services, vacation, days(MON_MAR_9, TUE_MAR_10))
    services.requests.approve(req.id, ADMIN)

    with pytest.raises(ValidationError, match="already started"):
        services.requests.cancel(req.id, "alice", today=TUE_MAR_10)

    with pytest.raises(ValidationError, match="Add a reason"):
        services.requests.cancel(req.id, ADMIN, today=TUE_MAR_10, by_admin=True)

    services.requests.cancel(req.id, ADMIN, today=TUE_MAR_10, by_admin=True, reason="Came back early")
    assert req.status == RequestStatus.CANCELLED
    assert req.cancelled_by == ADMIN and req.cancel_reason == "Came back early"


def test_other_employees_cannot_cancel(services, vacation):
    req = request(services, vacation, days(MON_MAR_9))
    with pytest.raises(ValidationError, match="Only the employee"):
        services.requests.cancel(req.id, "bob", today=date(2026, 3, 1))


def test_holiday_added_after_approval_refunds_that_day(services, vacation):
    req = request(services, vacation, days(MON_MAR_9, TUE_MAR_10))
    services.requests.approve(req.id, ADMIN)

    _, refunded = services.holidays.add_holiday(company_id="acme", day=TUE_MAR_10, name="Offsite", actor_id=ADMIN)

    assert refunded == [req.id]
    assert req.total_minutes == DAY_8H
    assert services.ledger.settled_balance("alice", vacation.id) == 14 * DAY_8H


def test_holiday_added_while_pending_releases_part_of_the_hold(services, vacation):
    req = request(services, vacation, days(MON_MAR_9, TUE_MAR_10))

    services.holidays.add_holiday(company_id="acme", day=TUE_MAR_10, name="Offsite", actor_id=ADMIN)
    services.requests.approve(req.id, ADMIN)

    assert services.ledger.pending_minutes("alice", vacation.id) == 0
    assert services.ledger.settled_balance("alice", vacation.id) == 14 * DAY_8H


def test_location_specific_holiday_only_applies_to_that_location(services, vacation):
    services.holidays.add_holiday(company_id="acme", day=MON_MAR_9, name="NY only", location="NY", actor_id=ADMIN)

    req = request(services, vacation, days(MON_MAR_9))  # Alice is in CA

    assert req.total_minutes == DAY_8H


def test_employee_must_be_assigned_to_the_policy(services, vacation):
    with pytest.raises(ValidationError, match="not assigned"):
        request(services, vacation, days(date(2026, 8, 3)), employee_id="bob")


def test_unlimited_policy_has_no_balance_check_but_tracks_usage(services):
    unlimited = create_policy(services, {"accrual_method": "unlimited"}, name="Unlimited PTO")
    assign(services, unlimited, "bob", start=date(2026, 7, 1))

    req = request(services, unlimited, weekdays(date(2026, 8, 3), 30), employee_id="bob")
    services.requests.approve(req.id, ADMIN)

    summary = services.balances.summary("bob", unlimited.id, date(2026, 8, 3))
    assert summary["unlimited"] is True
    assert summary["available"] is None
    assert summary["used"]["days"] == 30


def test_every_status_change_is_recorded(services, vacation):
    req = request(services, vacation, days(MON_MAR_9))
    services.requests.approve(req.id, ADMIN, note="Enjoy")

    events = services.requests.events_for(req.id)
    assert [(e.from_status, e.to_status) for e in events] == [
        (None, RequestStatus.PENDING),
        (RequestStatus.PENDING, RequestStatus.APPROVED),
    ]
    assert events[-1].actor_id == ADMIN

    types = [t.type for t in services.ledger.transactions_for_source("time_off_request", req.id)]
    assert types == [TransactionType.HOLD, TransactionType.HOLD_RELEASE, TransactionType.USAGE]


def test_accrual_note_explains_the_math(services, vacation):
    [txn] = [t for t in services.ledger.transactions("alice", vacation.id) if t.effective_date == date(2026, 3, 1)]
    assert txn.note == "March 2026: 5 days x 8h = 40h"
