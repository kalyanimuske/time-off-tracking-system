"""Demo data so the app is usable right away at http://localhost:8000.

A made-up company ("Acme Inc.") with five people, four policies, holidays,
some requests, and every payroll run since Jan 1. Everything is generated
relative to today, so the demo always has past time off, requests waiting for
approval, and payroll history.

The order matters and mirrors real life:
  1. monthly accruals up to today
  2. time off requested and approved
  3. payroll runs for every finished pay period. Payroll subtracts approved
     time off from hours worked, then publishes on_payroll_processed, which
     credits sick leave.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from app.domain.models import Company, Employee, EmploymentType, PayType, RequestedDay
from app.external.company_service import CompanyService
from app.external.employee_service import EmployeeService
from app.external.payroll_service import pay_period_containing
from app.services import Services, build_services

COMPANY_ID = "acme"
ADMIN_ID = "dana"
PROGRAM_START = date(2026, 1, 1)


def demo_company() -> Company:
    return Company(
        id=COMPANY_ID,
        name="Acme Inc. (demo company)",
        locations=["CA", "NY"],
        pay_schedule="semi_monthly",
        timezone="America/Los_Angeles",
    )


def demo_employees() -> list[Employee]:
    return [
        Employee(
            id="alice", company_id=COMPANY_ID, name="Alice Chen", hire_date=date(2021, 3, 1), location="CA",
            title="Senior Software Engineer", department="Engineering",
            employment_type=EmploymentType.FULL_TIME, pay_type=PayType.SALARY, pay_rate=165_000,
        ),
        Employee(
            id="bob", company_id=COMPANY_ID, name="Bob Rivera", hire_date=date(2026, 7, 1), location="NY",
            hours_per_day=6, title="Support Specialist", department="Customer Support",
            employment_type=EmploymentType.PART_TIME, pay_type=PayType.HOURLY, pay_rate=28,
        ),
        Employee(
            id="priya", company_id=COMPANY_ID, name="Priya Shah", hire_date=date(2024, 5, 10), location="NY",
            title="Head of Product", department="Product",
            employment_type=EmploymentType.FULL_TIME, pay_type=PayType.SALARY, pay_rate=190_000,
        ),
        Employee(
            id="marco", company_id=COMPANY_ID, name="Marco Diaz", hire_date=date(2025, 11, 3), location="CA",
            title="Contract Designer", department="Design",
            employment_type=EmploymentType.CONTRACTOR, pay_type=PayType.HOURLY, pay_rate=65,
        ),
        Employee(
            id="dana", company_id=COMPANY_ID, name="Dana Brooks", hire_date=date(2020, 1, 6), location="CA",
            title="People Operations Manager", department="People", is_admin=True,
            employment_type=EmploymentType.FULL_TIME, pay_type=PayType.SALARY, pay_rate=120_000,
        ),
    ]


def _next_weekdays(start: date, count: int, holidays: set[date]) -> list[date]:
    result, current = [], start
    while len(result) < count:
        if current.weekday() < 5 and current not in holidays:
            result.append(current)
        current += timedelta(days=1)
    return result


def build_demo_services(today: Optional[date] = None) -> Services:
    today = today or date.today()
    employees = EmployeeService(demo_employees())
    s = build_services(employees=employees, companies=CompanyService([demo_company()]))

    # Holidays (July 4th 2026 is a Saturday, so the observed day is Friday)
    for day, name, location in [
        (date(2026, 1, 1), "New Year's Day", None),
        (date(2026, 3, 31), "César Chávez Day", "CA"),
        (date(2026, 5, 25), "Memorial Day", None),
        (date(2026, 7, 3), "Independence Day (observed)", None),
        (date(2026, 9, 7), "Labor Day", None),
        (date(2026, 11, 26), "Thanksgiving", None),
        (date(2026, 11, 27), "Day after Thanksgiving", None),
        (date(2026, 12, 25), "Christmas Day", None),
        (date(2027, 1, 1), "New Year's Day", None),
    ]:
        s.holidays.add_holiday(company_id=COMPANY_ID, day=day, name=name, location=location, actor_id=ADMIN_ID)

    # Policies. Employees and contractors are on different policies, which is
    # the "Employee vs Contractor" grouping from the prompt.
    vacation, _ = s.policies.create_policy(
        company_id=COMPANY_ID,
        name="Full-Time Vacation",
        time_off_type="vacation",
        description="Accrues monthly. More days after 5 years.",
        effective_from=PROGRAM_START,
        actor_id=ADMIN_ID,
        settings={
            "accrual_method": "time_based",
            "frequency": "monthly",
            "rate_amount": 1.25,  # 15 days a year
            "rate_unit": "days",
            "carryover_cap_hours": 40,
            "tenure_tiers": [{"min_years": 0, "rate_amount": 1.25}, {"min_years": 5, "rate_amount": 1.5}],
        },
    )
    unlimited, _ = s.policies.create_policy(
        company_id=COMPANY_ID,
        name="Unlimited PTO (Leadership)",
        time_off_type="vacation",
        effective_from=PROGRAM_START,
        actor_id=ADMIN_ID,
        settings={"accrual_method": "unlimited"},
    )
    sick, _ = s.policies.create_policy(
        company_id=COMPANY_ID,
        name="Sick Leave",
        time_off_type="sick",
        description="1 hour for every 30 hours worked, from payroll.",
        effective_from=PROGRAM_START,
        actor_id=ADMIN_ID,
        settings={
            "accrual_method": "hours_worked",
            "rate_amount": 1,
            "rate_unit": "hours",
            "hours_worked_per_accrual": 30,
            "max_balance_hours": 80,
            "allow_negative": True,
            "max_negative_hours": 8,
        },
    )
    contractor, _ = s.policies.create_policy(
        company_id=COMPANY_ID,
        name="Contractor Time Off (unpaid)",
        time_off_type="other",
        description="Contractors can take unpaid time off. It's tracked so payroll knows.",
        is_paid=False,
        effective_from=PROGRAM_START,
        actor_id=ADMIN_ID,
        settings={"accrual_method": "unlimited"},
    )

    for employee in employees.list_all():
        start = max(PROGRAM_START, employee.hire_date)
        if employee.employment_type == EmploymentType.CONTRACTOR:
            s.policies.assign_employee(contractor.id, employee.id, start, actor_id=ADMIN_ID)
            continue
        vacation_policy = unlimited if employee.id == "priya" else vacation
        s.policies.assign_employee(vacation_policy.id, employee.id, start, actor_id=ADMIN_ID)
        s.policies.assign_employee(sick.id, employee.id, start, actor_id=ADMIN_ID)

    # 1. Monthly accruals up to today
    s.accruals.run_time_based_accruals(as_of=today)

    # 2. Some requests so every screen has something to show
    holidays = {h.day for h in s.store.holidays.values() if h.location is None}

    def create(employee_id, policy, start, count, reason):
        return s.requests.create_request(
            employee_id=employee_id,
            policy_id=policy.id,
            reason=reason,
            requested_days=[RequestedDay(d) for d in _next_weekdays(start, count, holidays)],
        )

    trip = create("alice", vacation, today - timedelta(days=35), 3, "Long weekend in Big Sur")
    s.requests.approve(trip.id, ADMIN_ID, note="Enjoy!")
    flu = create("alice", sick, today - timedelta(days=14), 1, "Flu")  # borrows from the 8h negative limit
    s.requests.approve(flu.id, ADMIN_ID)
    visit = create("priya", unlimited, today + timedelta(days=30), 5, "Family visit")
    s.requests.approve(visit.id, ADMIN_ID)
    unpaid = create("marco", contractor, today - timedelta(days=20), 2, "Personal days")
    s.requests.approve(unpaid.id, ADMIN_ID)
    create("alice", vacation, today + timedelta(days=21), 2, "Friend's wedding")
    create("bob", vacation, today + timedelta(days=10), 2, "Moving apartments")

    # 3. Every pay period that has ended since Jan 1. Each run publishes
    #    on_payroll_processed, and the Time Off Service credits sick leave.
    period_start, period_end = pay_period_containing(PROGRAM_START)
    while period_end < today:
        processed_on = period_end + timedelta(days=1)
        s.payroll.run(
            COMPANY_ID, period_start, period_end, actor_id=ADMIN_ID, today=processed_on,
            processed_at=datetime.combine(processed_on, time(17, 0), tzinfo=timezone.utc),
        )
        period_start, period_end = pay_period_containing(period_end + timedelta(days=1))
    return s
