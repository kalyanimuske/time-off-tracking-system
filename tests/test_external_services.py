"""Tests for the simulated existing services and how they connect to Time Off."""

from datetime import date

import pytest

from app.domain.errors import NotFoundError, ValidationError
from app.domain.models import Employee, EmploymentType, PayType, RequestedDay, TransactionType
from app.external.events import EventBus
from tests.helpers import ADMIN, MONTHLY_5_DAYS, assign, create_policy

SEPT_1, SEPT_15 = date(2026, 9, 1), date(2026, 9, 15)  # Labor Day (Sep 7) is inside
AFTER = date(2026, 9, 20)
HOURLY_SICK = {"accrual_method": "hours_worked", "rate_amount": 1, "rate_unit": "hours", "hours_worked_per_accrual": 30}


@pytest.fixture
def payroll_people(employees):
    employees.add(
        Employee(id="sam", company_id="acme", name="Sam", hire_date=date(2020, 1, 1),
                 employment_type=EmploymentType.FULL_TIME, pay_type=PayType.SALARY, pay_rate=120_000)
    )
    employees.add(
        Employee(id="kim", company_id="acme", name="Kim", hire_date=date(2020, 1, 1),
                 employment_type=EmploymentType.PART_TIME, pay_type=PayType.HOURLY, pay_rate=20)
    )
    employees.add(
        Employee(id="cory", company_id="acme", name="Cory", hire_date=date(2020, 1, 1),
                 employment_type=EmploymentType.CONTRACTOR, pay_type=PayType.HOURLY, pay_rate=50)
    )
    return employees


def line_for(lines, employee_id):
    return next(line for line in lines if line.employee_id == employee_id)


def add_labor_day(services):
    services.holidays.add_holiday(company_id="acme", day=date(2026, 9, 7), name="Labor Day", actor_id=ADMIN)


# ----- Company Service --------------------------------------------------- #


def test_unknown_company_is_rejected(services):
    with pytest.raises(NotFoundError):
        services.companies.get_company("nope")
    with pytest.raises(NotFoundError):
        services.policies.create_policy(
            company_id="nope", name="X", time_off_type="vacation", settings=MONTHLY_5_DAYS,
            effective_from=date(2026, 1, 1), actor_id=ADMIN,
        )


def test_holiday_location_must_be_a_company_location(services):
    with pytest.raises(ValidationError, match="no location TX"):
        services.holidays.add_holiday(company_id="acme", day=date(2026, 9, 7), name="X", location="TX", actor_id=ADMIN)


# ----- Payroll Service: hours ------------------------------------------- #


def test_hours_worked_subtract_holidays_and_time_off(services, payroll_people):
    add_labor_day(services)
    vacation = create_policy(services, MONTHLY_5_DAYS)
    assign(services, vacation, "sam")
    services.accruals.run_time_based_accruals(as_of=SEPT_1)
    req = services.requests.create_request(
        employee_id="sam", policy_id=vacation.id, reason="Trip",
        requested_days=[RequestedDay(date(2026, 9, 10)), RequestedDay(date(2026, 9, 11))],
    )
    services.requests.approve(req.id, ADMIN)

    sam = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15), "sam")

    assert sam.scheduled_hours == 88  # 11 weekdays x 8h
    assert sam.holiday_hours == 8
    assert sam.paid_time_off_hours == 16
    assert sam.hours_worked == 64  # 88 - 8 - 16


def test_pending_time_off_is_not_taken_out_of_hours(services, payroll_people):
    vacation = create_policy(services, MONTHLY_5_DAYS)
    assign(services, vacation, "sam")
    services.accruals.run_time_based_accruals(as_of=SEPT_1)
    services.requests.create_request(
        employee_id="sam", policy_id=vacation.id, reason="Trip", requested_days=[RequestedDay(date(2026, 9, 10))]
    )
    assert line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15), "sam").paid_time_off_hours == 0


def test_people_hired_mid_period_only_get_their_days(services, payroll_people):
    # Bob (6h days) was hired Jul 1, so he isn't in June's payroll at all
    june = services.payroll.calculate("acme", date(2026, 6, 1), date(2026, 6, 15))
    assert "bob" not in [line.employee_id for line in june]
    july = line_for(services.payroll.calculate("acme", date(2026, 7, 1), date(2026, 7, 15)), "bob")
    assert july.scheduled_hours == 11 * 6


# ----- Payroll Service: pay (simplified) --------------------------------- #


def test_salaried_pay_is_the_same_every_period(services, payroll_people):
    sam = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15), "sam")
    assert sam.gross_pay == 5000.00  # 120,000 / 24
    assert sam.benefits == 150.00
    assert sam.taxes == round((5000 - 150) * 0.20, 2)
    assert sam.net_pay == round(5000 - 150 - sam.taxes, 2)


def test_holidays_are_not_worked_but_are_paid(services, payroll_people):
    add_labor_day(services)
    kim = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15), "kim")
    assert kim.hours_worked == 80  # sick leave is earned on this
    assert kim.paid_hours == 88  # pay is based on this
    assert kim.gross_pay == 88 * 20


def test_contractors_get_no_holiday_pay_benefits_or_withholding(services, payroll_people):
    add_labor_day(services)
    cory = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15), "cory")
    assert cory.paid_hours == 80  # no holiday pay
    assert cory.gross_pay == 80 * 50
    assert cory.benefits == 0 and cory.taxes == 0


def test_unpaid_time_off_is_not_paid(services, payroll_people):
    unpaid = create_policy(services, {"accrual_method": "unlimited"}, name="Unpaid", time_off_type="other")
    services.store.policies[unpaid.id].is_paid = False
    assign(services, unpaid, "cory")
    req = services.requests.create_request(
        employee_id="cory", policy_id=unpaid.id, reason="Break", requested_days=[RequestedDay(date(2026, 9, 10))]
    )
    services.requests.approve(req.id, ADMIN)

    cory = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15), "cory")
    assert cory.unpaid_time_off_hours == 8
    assert cory.gross_pay == (88 - 8) * 50


# ----- Payroll Service: running and the event ----------------------------- #


def test_running_payroll_publishes_the_event_and_credits_sick_leave(services, payroll_people):
    add_labor_day(services)
    sick = create_policy(services, HOURLY_SICK, name="Sick", time_off_type="sick")
    assign(services, sick, "sam")

    run = services.payroll.run("acme", SEPT_1, SEPT_15, actor_id=ADMIN, today=AFTER)

    assert services.bus.log[-1].run_id == run.id
    assert services.ledger.settled_balance("sam", sick.id) == 160  # 80h worked / 30 = 2.67h
    assert run.time_off_effects[0]["employee_id"] == "sam"


def test_running_the_same_period_again_is_a_correction(services, payroll_people):
    sick = create_policy(services, HOURLY_SICK, name="Sick", time_off_type="sick")
    assign(services, sick, "sam")
    services.payroll.run("acme", SEPT_1, SEPT_15, actor_id=ADMIN, today=AFTER)  # 88h

    same = services.payroll.run("acme", SEPT_1, SEPT_15, actor_id=ADMIN, today=AFTER)
    assert same.times_processed == 2 and same.time_off_effects == []

    corrected = services.payroll.run("acme", SEPT_1, SEPT_15, actor_id=ADMIN, today=AFTER, hours_overrides={"sam": 94})
    [effect] = corrected.time_off_effects
    assert effect["type"] == TransactionType.ADJUSTMENT.value
    assert effect["amount_minutes"] == 12  # (94 - 88) / 30 h = 0.2h


def test_payroll_cannot_run_before_the_period_ends(services, payroll_people):
    with pytest.raises(ValidationError, match="hasn't ended"):
        services.payroll.run("acme", SEPT_1, SEPT_15, actor_id=ADMIN, today=SEPT_15)


def test_event_bus_delivers_to_every_subscriber():
    bus, received = EventBus(), []
    bus.subscribe("ping", lambda e: received.append(("a", e)))
    bus.subscribe("ping", lambda e: received.append(("b", e)))
    bus.publish("ping", 1)
    assert received == [("a", 1), ("b", 1)]
    assert bus.log[-1].subscribers == 2


def test_run_is_outdated_when_time_off_is_approved_afterwards(services, payroll_people):
    vacation = create_policy(services, MONTHLY_5_DAYS)
    assign(services, vacation, "sam")
    services.accruals.run_time_based_accruals(as_of=SEPT_1)
    run = services.payroll.run("acme", SEPT_1, SEPT_15, actor_id=ADMIN, today=AFTER)
    assert not services.payroll.is_outdated(run)

    late = services.requests.create_request(
        employee_id="sam", policy_id=vacation.id, reason="Forgot to log it", requested_days=[RequestedDay(date(2026, 9, 10))]
    )
    services.requests.approve(late.id, ADMIN)
    assert services.payroll.is_outdated(run)

    rerun = services.payroll.run("acme", SEPT_1, SEPT_15, actor_id=ADMIN, today=AFTER)
    assert not services.payroll.is_outdated(rerun)


def test_changing_hours_changes_pay_for_hourly_people(services, payroll_people):
    add_labor_day(services)
    kim = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15, {"kim": 90}), "kim")
    assert kim.hours_worked == 90 and kim.hours_overridden
    assert kim.paid_hours == 98  # 90 worked + 8 paid holiday
    assert kim.gross_pay == 98 * 20


def test_changing_hours_does_not_change_pay_for_salaried_people(services, payroll_people):
    add_labor_day(services)
    normal = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15), "sam")
    extra = line_for(services.payroll.calculate("acme", SEPT_1, SEPT_15, {"sam": 90}), "sam")
    assert extra.hours_worked == 90  # more sick leave...
    assert extra.paid_hours == normal.paid_hours == 88  # ...but the same pay
    assert extra.gross_pay == normal.gross_pay == 5000.00
