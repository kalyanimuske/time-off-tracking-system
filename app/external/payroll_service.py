"""Simulated Payroll Service (an existing service, per the prompt).

For a pay period it works out each employee's hours and a simplified pay
calculation, then publishes on_payroll_processed with the hours worked.

    hours worked = scheduled hours - holiday hours - approved time off hours

The pay numbers are deliberately simple (a flat benefits amount and a flat tax
rate). Real payroll is out of scope for this take-home; the point is to show
how payroll and time off depend on each other:

  * Payroll ASKS the Time Off Service for approved time off in the period
    (paid time off is paid, unpaid time off isn't, and neither counts as worked).
  * Time Off LISTENS to on_payroll_processed to credit hours-worked accruals.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.domain.errors import ValidationError
from app.domain.models import Employee, EmploymentType, PayType
from app.domain.time_off_requests import TimeOffRequestService
from app.domain.work_calendar import WorkCalendar
from app.external.company_service import CompanyService
from app.external.employee_service import EmployeeService
from app.external.events import ON_PAYROLL_PROCESSED, EventBus, PayrollEntry, PayrollProcessedEvent

# Simplified, demo-only numbers
BENEFITS_PER_PERIOD = {
    EmploymentType.FULL_TIME: 150.00,  # e.g. health insurance share
    EmploymentType.PART_TIME: 50.00,
    EmploymentType.CONTRACTOR: 0.00,  # contractors don't get benefits
}
TAX_RATE = 0.20  # flat estimate; contractors handle their own taxes
PERIODS_PER_YEAR = 24  # semi-monthly
WORK_HOURS_PER_YEAR = 2080  # 40h x 52 weeks, used to price unpaid time for salaried people


@dataclass
class PayrollLine:
    employee_id: str
    employee_name: str
    employment_type: str
    pay_type: str
    pay_rate: float
    scheduled_hours: float
    holiday_hours: float
    paid_time_off_hours: float
    unpaid_time_off_hours: float
    hours_worked: float  # used for sick leave (earned per hour WORKED)
    hours_overridden: bool
    paid_hours: float  # used for pay: worked + paid time off + paid holidays
    gross_pay: float
    benefits: float
    taxes: float
    net_pay: float


@dataclass
class PayrollRun:
    id: str
    company_id: str
    period_start: date
    period_end: date
    lines: list[PayrollLine]
    processed_by: str
    processed_at: datetime
    times_processed: int = 1
    time_off_effects: list[dict] = field(default_factory=list)

    @property
    def total_gross(self) -> float:
        return round(sum(line.gross_pay for line in self.lines), 2)

    @property
    def total_net(self) -> float:
        return round(sum(line.net_pay for line in self.lines), 2)


def pay_period_containing(day: date) -> tuple[date, date]:
    """Semi-monthly pay periods: the 1st to the 15th, and the 16th to the end of the month."""
    if day.day <= 15:
        return date(day.year, day.month, 1), date(day.year, day.month, 15)
    last_day = calendar.monthrange(day.year, day.month)[1]
    return date(day.year, day.month, 16), date(day.year, day.month, last_day)


def pay_periods_before(today: date, count: int) -> list[tuple[date, date]]:
    """The last `count` pay periods that have fully ended, newest first."""
    periods = []
    cursor = today - timedelta(days=1)
    while len(periods) < count:
        start, end = pay_period_containing(cursor)
        if end < today:
            periods.append((start, end))
        cursor = start - timedelta(days=1)
    return periods


class PayrollService:
    def __init__(
        self,
        employees: EmployeeService,
        companies: CompanyService,
        time_off: TimeOffRequestService,
        calendar: WorkCalendar,
        bus: EventBus,
    ) -> None:
        self.employees = employees
        self.companies = companies
        self.time_off = time_off
        self.calendar = calendar
        self.bus = bus
        self.runs: dict[str, PayrollRun] = {}  # the Payroll Service's own storage

    # ----- calculation --------------------------------------------------- #

    def calculate(
        self,
        company_id: str,
        period_start: date,
        period_end: date,
        hours_overrides: Optional[dict[str, float]] = None,
    ) -> list[PayrollLine]:
        """Hours and pay for everyone employed during the period. Nothing is saved."""
        self.companies.get_company(company_id)
        if period_end < period_start:
            raise ValidationError("The period ends before it starts")
        overrides = hours_overrides or {}

        lines = []
        for employee in sorted(self.employees.list_employees(company_id), key=lambda e: e.name):
            start = max(period_start, employee.hire_date)
            end = min(period_end, employee.termination_date or period_end)
            if start > end:
                continue  # not employed during this period
            lines.append(self._line(employee, start, end, overrides.get(employee.id)))
        return lines

    def _line(self, employee: Employee, start: date, end: date, override: Optional[float]) -> PayrollLine:
        holidays = self.calendar.holidays_for(employee, start, end)
        workdays = [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() in employee.work_weekdays
        ]
        scheduled = len(workdays) * employee.hours_per_day
        holiday_hours = sum(employee.hours_per_day for d in workdays if d in holidays)

        paid_off = unpaid_off = 0.0
        for item in self.time_off.approved_time_off(employee.id, start, end):
            if item["is_paid"]:
                paid_off += item["minutes"] / 60
            else:
                unpaid_off += item["minutes"] / 60

        worked = max(scheduled - holiday_hours - paid_off - unpaid_off, 0.0)
        if override is not None:
            if override < 0:
                raise ValidationError("Hours worked can't be negative")
            worked = float(override)

        if employee.pay_type == PayType.SALARY:
            # Salaried people are paid for their normal schedule every period.
            # Working extra hours doesn't change pay (no overtime), only unpaid
            # time off reduces it. Their hours worked still matter for sick leave.
            paid_hours = scheduled - unpaid_off
        else:
            # Hourly: holidays and paid time off aren't worked, but they are paid
            # (contractors aren't paid for holidays).
            paid_holidays = 0.0 if employee.employment_type == EmploymentType.CONTRACTOR else holiday_hours
            paid_hours = worked + paid_off + paid_holidays

        gross = self._gross_pay(employee, paid_hours, unpaid_off)
        benefits = BENEFITS_PER_PERIOD[employee.employment_type]
        taxes = 0.0 if employee.employment_type == EmploymentType.CONTRACTOR else round((gross - benefits) * TAX_RATE, 2)
        return PayrollLine(
            employee_id=employee.id,
            employee_name=employee.name,
            employment_type=employee.employment_type.value,
            pay_type=employee.pay_type.value,
            pay_rate=employee.pay_rate,
            scheduled_hours=round(scheduled, 2),
            holiday_hours=round(holiday_hours, 2),
            paid_time_off_hours=round(paid_off, 2),
            unpaid_time_off_hours=round(unpaid_off, 2),
            hours_worked=round(worked, 2),
            hours_overridden=override is not None,
            paid_hours=round(paid_hours, 2),
            gross_pay=gross,
            benefits=benefits,
            taxes=taxes,
            net_pay=round(gross - benefits - taxes, 2),
        )

    @staticmethod
    def _gross_pay(employee: Employee, paid_hours: float, unpaid_off: float) -> float:
        if employee.pay_type == PayType.SALARY:
            # Same pay every period, minus any unpaid time off
            hourly_equivalent = employee.pay_rate / WORK_HOURS_PER_YEAR
            return round(employee.pay_rate / PERIODS_PER_YEAR - unpaid_off * hourly_equivalent, 2)
        # Hourly: paid hours x rate
        return round(paid_hours * employee.pay_rate, 2)

    # ----- running payroll ------------------------------------------------ #

    def run(
        self,
        company_id: str,
        period_start: date,
        period_end: date,
        actor_id: str,
        today: date,
        hours_overrides: Optional[dict[str, float]] = None,
        processed_at: Optional[datetime] = None,
    ) -> PayrollRun:
        """Process payroll for a period and publish on_payroll_processed.

        Running the same period again re-processes it (same run id), which is
        how a payroll correction works. The Time Off Service then posts only
        the difference.
        """
        if period_end >= today:
            raise ValidationError(f"The period hasn't ended yet. Payroll can run from {period_end + timedelta(days=1)}.")
        lines = self.calculate(company_id, period_start, period_end, hours_overrides)

        run_id = f"payroll-{company_id}-{period_start.isoformat()}"
        previous = self.runs.get(run_id)
        run = PayrollRun(
            id=run_id,
            company_id=company_id,
            period_start=period_start,
            period_end=period_end,
            lines=lines,
            processed_by=actor_id,
            processed_at=processed_at or datetime.now(timezone.utc),
            times_processed=previous.times_processed + 1 if previous else 1,
        )
        self.runs[run_id] = run

        event = PayrollProcessedEvent(
            run_id=run_id,
            company_id=company_id,
            period_start=period_start,
            period_end=period_end,
            entries=[PayrollEntry(line.employee_id, line.hours_worked) for line in lines],
        )
        results = self.bus.publish(ON_PAYROLL_PROCESSED, event)
        # Keep what the subscribers did, so the admin can see the effect on time off
        run.time_off_effects = [
            {"employee_id": t.employee_id, "amount_minutes": t.amount_minutes, "type": t.type.value, "note": t.note}
            for posted in results
            for t in (posted or [])
        ]
        return run

    def is_outdated(self, run: PayrollRun) -> bool:
        """True if holidays or approved time off in the period changed after the run.

        Example: payroll ran for Sep 1-15, then an admin approves a sick day on
        Sep 10. The run still has the old hours, so it should be re-run."""
        now = {line.employee_id: line for line in self.calculate(run.company_id, run.period_start, run.period_end)}
        then = {line.employee_id: line for line in run.lines}
        if now.keys() != then.keys():
            return True
        fields = ("holiday_hours", "paid_time_off_hours", "unpaid_time_off_hours")
        return any(getattr(now[e], f) != getattr(then[e], f) for e in then for f in fields)

    def list_runs(self, company_id: str) -> list[PayrollRun]:
        return sorted(
            (r for r in self.runs.values() if r.company_id == company_id), key=lambda r: r.period_start, reverse=True
        )
