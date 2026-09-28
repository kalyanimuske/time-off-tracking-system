"""The accrual engine.

Two ways to earn time off, one ledger:

1. Time based ("5 days per month") -> `run_time_based_accruals(as_of)`.
   Meant to run from a daily scheduler. It walks every assignment period by
   period and posts anything that is due but not posted yet. Each period has a
   deterministic idempotency key, so running it twice (or after an outage) is
   safe and it automatically catches up on missed days.

2. Hours worked ("1 hour per 30 hours worked") -> `handle_payroll_processed`.
   Subscribed to the Payroll Service's `on_payroll_processed` event. The payroll
   run id is the idempotency key. If a run is re-processed with different hours
   we post an ADJUSTMENT for the difference instead of editing history.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional

from app.external.employee_service import EmployeeService
from app.external.events import PayrollProcessedEvent
from app.domain.formatting import fmt_date, fmt_hours, fmt_range
from app.domain.ledger import Ledger
from app.domain.models import (
    AccrualFrequency,
    AccrualMethod,
    AccrualTiming,
    BalanceTransaction,
    Employee,
    PolicyAssignment,
    PolicyVersion,
    ProrationRule,
    RateUnit,
    TransactionType,
)
from app.domain.policies import PolicyService
from app.storage.memory import InMemoryStore

# --------------------------------------------------------------------------- #
# Pure helper functions (easy to unit test)
# --------------------------------------------------------------------------- #


def period_bounds(frequency: AccrualFrequency, day: date) -> tuple[date, date]:
    """The accrual period that contains `day` (both ends inclusive)."""
    if frequency == AccrualFrequency.MONTHLY:
        last_day = calendar.monthrange(day.year, day.month)[1]
        return date(day.year, day.month, 1), date(day.year, day.month, last_day)
    if frequency == AccrualFrequency.YEARLY:
        return date(day.year, 1, 1), date(day.year, 12, 31)
    raise ValueError(f"Unsupported frequency: {frequency}")


def years_of_service(hire_date: date, on_date: date) -> int:
    years = on_date.year - hire_date.year
    if (on_date.month, on_date.day) < (hire_date.month, hire_date.day):
        years -= 1
    return max(years, 0)


def rate_for(version: PolicyVersion, employee: Employee, on_date: date) -> float:
    """The accrual rate after applying tenure tiers (if the policy has any)."""
    rate = version.rate_amount
    years = years_of_service(employee.hire_date, on_date)
    for tier in sorted(version.tenure_tiers, key=lambda t: t.min_years):
        if years >= tier.min_years:
            rate = tier.rate_amount
    return rate


def tenure_tier_years(version: PolicyVersion, employee: Employee, on_date: date) -> Optional[int]:
    """The min_years of the tenure tier in use, or None if the policy has no tiers."""
    years = years_of_service(employee.hire_date, on_date)
    used = None
    for tier in sorted(version.tenure_tiers, key=lambda t: t.min_years):
        if years >= tier.min_years:
            used = tier.min_years
    return used


def to_minutes(amount: float, unit: RateUnit, employee: Employee) -> float:
    """Convert a policy amount to minutes. A "day" is the employee's own
    working day, so 1 day = 8h for most people and 6h for a 6-hour employee."""
    if unit == RateUnit.DAYS:
        return amount * employee.minutes_per_day
    return amount * 60


def proration_factor(
    rule: ProrationRule,
    period_start: date,
    period_end: date,
    eligible_start: date,
    eligible_end: date,
) -> float:
    """Share of the period the employee should be credited for.

    `rule` only decides what happens when someone *joins* mid-period. Leaving
    mid-period (termination, assignment ended) is always prorated.
    """
    joined_late = eligible_start > period_start
    if joined_late and rule == ProrationRule.NEXT_PERIOD:
        return 0.0
    if joined_late and rule == ProrationRule.FULL:
        eligible_start = period_start

    total_days = (period_end - period_start).days + 1
    active_days = (eligible_end - eligible_start).days + 1
    return max(0.0, min(1.0, active_days / total_days))


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


class AccrualService:
    def __init__(
        self,
        store: InMemoryStore,
        ledger: Ledger,
        policies: PolicyService,
        employees: EmployeeService,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.policies = policies
        self.employees = employees

    # ----- time based ---------------------------------------------------- #

    def run_time_based_accruals(self, as_of: date) -> list[BalanceTransaction]:
        """Post every time-based accrual that is due on or before `as_of`."""
        posted: list[BalanceTransaction] = []
        for assignment in list(self.store.assignments.values()):
            employee = self.employees.get_employee(assignment.employee_id)
            posted.extend(self._accrue_assignment(employee, assignment, as_of))
        return posted

    def _accrue_assignment(
        self, employee: Employee, assignment: PolicyAssignment, as_of: date
    ) -> list[BalanceTransaction]:
        posted = []
        last_day = min(d for d in (as_of, assignment.end_date, employee.termination_date) if d is not None)
        cursor = assignment.start_date

        while cursor <= last_day:
            # The version in effect when the period starts applies to the whole
            # period. A change made mid-month takes effect next month.
            version = self.policies.get_version(assignment.policy_id, cursor)
            if version is None or version.accrual_method != AccrualMethod.TIME_BASED:
                next_change = self.policies.next_version_date(assignment.policy_id, cursor)
                if next_change is None:
                    break
                cursor = next_change
                continue

            period_start, period_end = period_bounds(version.frequency, cursor)
            txn = self._accrue_period(employee, assignment, version, period_start, period_end, as_of)
            if txn is not None:
                posted.append(txn)
            cursor = period_end + timedelta(days=1)

        return posted

    def _accrue_period(
        self,
        employee: Employee,
        assignment: PolicyAssignment,
        version: PolicyVersion,
        period_start: date,
        period_end: date,
        as_of: date,
    ) -> Optional[BalanceTransaction]:
        key = f"accrual:{employee.id}:{assignment.policy_id}:{version.frequency.value}:{period_start.isoformat()}"
        if self.ledger.has_key(key):
            return None

        eligible_from = employee.hire_date + timedelta(days=version.waiting_period_days)
        eligible_start = max(period_start, assignment.start_date, version.effective_from, eligible_from)
        eligible_end = min(
            d for d in (period_end, assignment.end_date, employee.termination_date) if d is not None
        )
        if eligible_start > eligible_end:
            return None  # not eligible at all during this period

        if version.accrue_at == AccrualTiming.START_OF_PERIOD:
            due_date = eligible_start
        else:
            due_date = eligible_end
        if due_date > as_of:
            return None  # not due yet

        factor = proration_factor(version.proration_rule, period_start, period_end, eligible_start, eligible_end)
        rate = rate_for(version, employee, due_date)
        minutes = round(to_minutes(rate, version.rate_unit, employee) * factor)
        credited = self._apply_balance_cap(employee.id, assignment.policy_id, version, minutes)

        # A note an employee can read, e.g.
        # "September 2026: 1.67 days (rate after 5 years) x 8h = 13.37h"
        period = f"{period_start:%B %Y}" if version.frequency == AccrualFrequency.MONTHLY else str(period_start.year)
        if version.rate_unit == RateUnit.DAYS:
            note = f"{period}: {rate:g} days x {employee.hours_per_day:g}h = {fmt_hours(round(rate * employee.minutes_per_day))}"
        else:
            note = f"{period}: {rate:g}h"
        tier = tenure_tier_years(version, employee, due_date)
        if tier:
            note += f" (rate after {tier} years)"
        if factor < 1:
            note += f", prorated to {factor:.0%} because eligibility started {fmt_date(eligible_start)}"
        if credited < minutes:
            note += f", capped at the {version.max_balance_hours:g}h max balance"

        return self.ledger.post(
            employee_id=employee.id,
            policy_id=assignment.policy_id,
            amount_minutes=credited,
            txn_type=TransactionType.ACCRUAL,
            effective_date=due_date,
            idempotency_key=key,
            policy_version_id=version.id,
            source_type="accrual_period",
            source_id=f"{version.frequency.value}:{period_start.isoformat()}",
            note=note,
            metadata={
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "proration_factor": round(factor, 4),
                "rate": rate,
                "years_of_service": years_of_service(employee.hire_date, due_date),
                "uncapped_minutes": minutes,
            },
        )

    # ----- hours worked -------------------------------------------------- #

    def handle_payroll_processed(self, event: PayrollProcessedEvent) -> list[BalanceTransaction]:
        """Subscriber for the Payroll Service's `on_payroll_processed` event."""
        posted: list[BalanceTransaction] = []
        for entry in event.entries:
            employee = self.employees.get_employee(entry.employee_id)
            for assignment in self.policies.active_assignments_for(employee.id, event.period_end):
                version = self.policies.get_version(assignment.policy_id, event.period_end)
                if version is None or version.accrual_method != AccrualMethod.HOURS_WORKED:
                    continue
                eligible_from = employee.hire_date + timedelta(days=version.waiting_period_days)
                if event.period_end < eligible_from:
                    continue
                txn = self._accrue_from_payroll(employee, assignment.policy_id, version, event, entry.hours_worked)
                if txn is not None:
                    posted.append(txn)
        return posted

    def _accrue_from_payroll(
        self,
        employee: Employee,
        policy_id: str,
        version: PolicyVersion,
        event: PayrollProcessedEvent,
        hours_worked: float,
    ) -> Optional[BalanceTransaction]:
        rate = rate_for(version, employee, event.period_end)
        earned = round(
            to_minutes(rate, version.rate_unit, employee) * hours_worked / version.hours_worked_per_accrual
        )
        base_key = f"payroll:{event.run_id}:{employee.id}:{policy_id}"
        previous = self.ledger.transactions_for_source("payroll_run", event.run_id, employee.id, policy_id)
        common = dict(
            employee_id=employee.id,
            policy_id=policy_id,
            effective_date=event.period_end,
            policy_version_id=version.id,
            source_type="payroll_run",
            source_id=event.run_id,
            metadata={"hours_worked": hours_worked, "earned_minutes": earned},
        )

        if not previous:
            return self.ledger.post(
                amount_minutes=self._apply_balance_cap(employee.id, policy_id, version, earned),
                txn_type=TransactionType.ACCRUAL,
                idempotency_key=base_key,
                note=(
                    f"{hours_worked:g}h worked / {version.hours_worked_per_accrual:g} x {rate:g}{'h' if version.rate_unit == RateUnit.HOURS else ' days'}"
                    f" = {fmt_hours(earned)} ({fmt_range(event.period_start, event.period_end)})"
                ),
                **common,
            )

        # Same run seen again. If the hours didn't change it's just a replay.
        delta = earned - previous[-1].metadata["earned_minutes"]
        if delta == 0:
            return None
        if delta > 0:
            delta = self._apply_balance_cap(employee.id, policy_id, version, delta)
        return self.ledger.post(
            amount_minutes=delta,
            txn_type=TransactionType.ADJUSTMENT,
            idempotency_key=f"{base_key}:correction:{len(previous)}",
            note=f"Payroll corrected to {hours_worked:g}h worked ({fmt_range(event.period_start, event.period_end)})",
            **common,
        )

    # ----- shared -------------------------------------------------------- #

    def _apply_balance_cap(self, employee_id: str, policy_id: str, version: PolicyVersion, minutes: int) -> int:
        """If the policy has a max balance, only credit up to the cap."""
        if version.max_balance_hours is None or minutes <= 0:
            return minutes
        cap = round(version.max_balance_hours * 60)
        room = max(cap - self.ledger.settled_balance(employee_id, policy_id), 0)
        return min(minutes, room)
