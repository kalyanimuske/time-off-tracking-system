"""Domain models for the time off system.

These are plain dataclasses so the business rules can be unit tested without a
database or a web server. In production each of these maps to a table in
schema.sql.

Convention: every amount in the balance ledger is an integer number of MINUTES.
Policies are configured in days or hours, and we convert using the employee's
own working-day length (8h, 6h, ...). This is what lets a 6-hour employee and an
8-hour employee share the same policy and the same code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class TimeOffType(str, Enum):
    VACATION = "vacation"
    SICK = "sick"
    PARENTAL = "parental"
    BEREAVEMENT = "bereavement"
    WFH = "wfh"
    OTHER = "other"


class AccrualMethod(str, Enum):
    UNLIMITED = "unlimited"
    TIME_BASED = "time_based"  # e.g. 5 days per month
    HOURS_WORKED = "hours_worked"  # e.g. 1 hour per 30 hours worked


class AccrualFrequency(str, Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


class AccrualTiming(str, Enum):
    START_OF_PERIOD = "start_of_period"
    END_OF_PERIOD = "end_of_period"


class RateUnit(str, Enum):
    DAYS = "days"
    HOURS = "hours"


class ProrationRule(str, Enum):
    PRORATE = "prorate"  # joined halfway through the period -> get half
    FULL = "full"  # joined halfway -> still get the full amount
    NEXT_PERIOD = "next_period"  # joined halfway -> start accruing next period


class TransactionType(str, Enum):
    ACCRUAL = "accrual"
    HOLD = "hold"  # a pending request reserves balance
    HOLD_RELEASE = "hold_release"  # hold removed (approved / rejected / cancelled)
    USAGE = "usage"  # approved time off
    USAGE_REVERSAL = "usage_reversal"  # approved time off cancelled or refunded
    EXPIRATION = "expiration"  # year-end balance above the carryover cap
    ADJUSTMENT = "adjustment"  # manual admin change or payroll correction


HOLD_TYPES = {TransactionType.HOLD, TransactionType.HOLD_RELEASE}
USAGE_TYPES = {TransactionType.USAGE, TransactionType.USAGE_REVERSAL}


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------- #
# Employees and companies (owned by the existing Employee and Company Services,
# the Time Off Service only reads them)
# --------------------------------------------------------------------------- #


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACTOR = "contractor"


class PayType(str, Enum):
    HOURLY = "hourly"  # pay_rate is dollars per hour
    SALARY = "salary"  # pay_rate is dollars per year


@dataclass
class Employee:
    id: str
    company_id: str
    name: str
    hire_date: date
    hours_per_day: float = 8.0
    work_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday=0 ... Sunday=6
    location: Optional[str] = None  # e.g. "CA", used for location-specific holidays
    termination_date: Optional[date] = None
    is_admin: bool = False  # can approve requests and manage policies
    title: str = ""
    department: str = ""
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    pay_type: PayType = PayType.HOURLY
    pay_rate: float = 0.0  # per hour (hourly) or per year (salary)

    @property
    def minutes_per_day(self) -> int:
        return round(self.hours_per_day * 60)


@dataclass
class Company:
    id: str
    name: str
    locations: list[str] = field(default_factory=list)  # e.g. ["CA", "NY"]
    pay_schedule: str = "semi_monthly"  # 1st-15th and 16th-end of month
    work_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    default_hours_per_day: float = 8.0
    timezone: str = "America/Los_Angeles"


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #


@dataclass
class Policy:
    """The stable identity of a policy. The rules live in PolicyVersion."""

    id: str
    company_id: str
    name: str
    time_off_type: TimeOffType
    description: str = ""
    is_paid: bool = True
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class TenureTier:
    """From `min_years` of service onwards, the rate becomes `rate_amount`."""

    min_years: int
    rate_amount: float


@dataclass
class PolicyVersion:
    """The accrual rules of a policy, starting on `effective_from`.

    Versions are never edited. Changing a policy creates a new version, so we
    always know which rules produced which accrual.
    """

    id: str
    policy_id: str
    version_number: int
    effective_from: date
    accrual_method: AccrualMethod
    frequency: Optional[AccrualFrequency] = None
    accrue_at: AccrualTiming = AccrualTiming.START_OF_PERIOD
    rate_amount: float = 0.0  # per period (time based) or per N hours worked
    rate_unit: RateUnit = RateUnit.DAYS
    hours_worked_per_accrual: Optional[float] = None  # the "N" for hours-worked
    max_balance_hours: Optional[float] = None  # stop accruing above this
    carryover_cap_hours: Optional[float] = None  # None = everything carries over
    allow_negative: bool = False
    max_negative_hours: float = 0.0
    proration_rule: ProrationRule = ProrationRule.PRORATE
    waiting_period_days: int = 0
    tenure_tiers: list[TenureTier] = field(default_factory=list)
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)

    @property
    def is_unlimited(self) -> bool:
        return self.accrual_method == AccrualMethod.UNLIMITED


# The fields an admin is allowed to configure (everything except bookkeeping).
POLICY_SETTING_FIELDS = (
    "accrual_method",
    "frequency",
    "accrue_at",
    "rate_amount",
    "rate_unit",
    "hours_worked_per_accrual",
    "max_balance_hours",
    "carryover_cap_hours",
    "allow_negative",
    "max_negative_hours",
    "proration_rule",
    "waiting_period_days",
    "tenure_tiers",
)


@dataclass
class PolicyAssignment:
    """Links an employee to a policy for a date range (inclusive)."""

    id: str
    policy_id: str
    employee_id: str
    start_date: date
    end_date: Optional[date] = None  # None = open-ended
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)

    def covers(self, day: date) -> bool:
        return self.start_date <= day and (self.end_date is None or day <= self.end_date)

    def overlaps(self, start: date, end: Optional[date]) -> bool:
        my_end = self.end_date or date.max
        other_end = end or date.max
        return self.start_date <= other_end and start <= my_end


# --------------------------------------------------------------------------- #
# Holidays
# --------------------------------------------------------------------------- #


@dataclass
class Holiday:
    id: str
    company_id: str
    day: date
    name: str
    location: Optional[str] = None  # None = applies to everyone in the company
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)


# --------------------------------------------------------------------------- #
# Balance ledger
# --------------------------------------------------------------------------- #


@dataclass
class BalanceTransaction:
    """One append-only row in the balance ledger. Balance = SUM(amount_minutes)."""

    id: str
    employee_id: str
    policy_id: str
    amount_minutes: int
    type: TransactionType
    effective_date: date
    idempotency_key: str
    policy_version_id: Optional[str] = None
    source_type: Optional[str] = None  # "time_off_request", "payroll_run", ...
    source_id: Optional[str] = None
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)


# --------------------------------------------------------------------------- #
# Time off requests
# --------------------------------------------------------------------------- #


@dataclass
class RequestedDay:
    """What the employee picked in the UI. minutes=None means a full day."""

    day: date
    minutes: Optional[int] = None


@dataclass
class RequestDay:
    """A working day that counts against the balance."""

    day: date
    minutes: int


@dataclass
class TimeOffRequest:
    id: str
    employee_id: str
    policy_id: str
    reason: str
    days: list[RequestDay]
    status: RequestStatus = RequestStatus.PENDING
    created_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_note: Optional[str] = None
    cancelled_by: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    @property
    def total_minutes(self) -> int:
        return sum(d.minutes for d in self.days)

    @property
    def start_date(self) -> date:
        return min(d.day for d in self.days)

    @property
    def end_date(self) -> date:
        return max(d.day for d in self.days)


@dataclass
class RequestEvent:
    """Every status change of a request, for the audit trail."""

    id: str
    request_id: str
    from_status: Optional[RequestStatus]
    to_status: RequestStatus
    actor_id: str
    note: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class AuditLogEntry:
    id: str
    entity_type: str
    entity_id: str
    action: str
    actor_id: str
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    created_at: datetime = field(default_factory=utc_now)
