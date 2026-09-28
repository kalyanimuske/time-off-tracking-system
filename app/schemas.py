"""Request bodies for the API (Pydantic handles validation and docs)."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.domain.models import (
    AccrualFrequency,
    AccrualMethod,
    AccrualTiming,
    ProrationRule,
    RateUnit,
    TimeOffType,
)


class TenureTierIn(BaseModel):
    min_years: int = Field(ge=0)
    rate_amount: float = Field(ge=0)


class PolicySettingsIn(BaseModel):
    accrual_method: AccrualMethod
    frequency: Optional[AccrualFrequency] = None
    accrue_at: AccrualTiming = AccrualTiming.START_OF_PERIOD
    rate_amount: float = 0
    rate_unit: RateUnit = RateUnit.DAYS
    hours_worked_per_accrual: Optional[float] = None
    max_balance_hours: Optional[float] = None
    carryover_cap_hours: Optional[float] = None
    allow_negative: bool = False
    max_negative_hours: float = 0
    proration_rule: ProrationRule = ProrationRule.PRORATE
    waiting_period_days: int = 0
    tenure_tiers: list[TenureTierIn] = []


class CreatePolicyIn(BaseModel):
    company_id: str
    name: str
    time_off_type: TimeOffType
    description: str = ""
    is_paid: bool = True
    effective_from: date
    settings: PolicySettingsIn


class UpdatePolicyIn(BaseModel):
    effective_from: date
    changes: dict[str, Any] = Field(description="Only the settings that change, e.g. {'rate_amount': 2}")


class AssignIn(BaseModel):
    employee_id: str
    start_date: date
    end_date: Optional[date] = None


class EndAssignmentIn(BaseModel):
    end_date: date


class HolidayIn(BaseModel):
    company_id: str
    day: date
    name: str
    location: Optional[str] = None


class RequestedDayIn(BaseModel):
    day: date
    minutes: Optional[int] = Field(default=None, gt=0, description="Leave empty for a full day")


class CreateRequestIn(BaseModel):
    employee_id: str
    policy_id: str
    reason: str
    days: list[RequestedDayIn] = Field(min_length=1)


class PreviewRequestIn(BaseModel):
    employee_id: str
    policy_id: str
    days: list[RequestedDayIn] = Field(min_length=1)


class DecisionIn(BaseModel):
    note: Optional[str] = None


class CancelIn(BaseModel):
    by_admin: bool = False
    reason: Optional[str] = None


class PayrollEntryIn(BaseModel):
    employee_id: str
    hours_worked: float = Field(ge=0)


class PayrollProcessedIn(BaseModel):
    run_id: str
    company_id: str
    period_start: date
    period_end: date
    entries: list[PayrollEntryIn]


class PayrollIn(BaseModel):
    company_id: str
    period_start: date
    period_end: date
    hours_overrides: dict[str, float] = Field(
        default_factory=dict, description="Optional: hours worked per employee id, if different from the schedule"
    )


class RunAccrualsIn(BaseModel):
    as_of: Optional[date] = None


class YearEndIn(BaseModel):
    policy_id: str
    year: int
    run_date: Optional[date] = Field(default=None, description="Pretend 'today' is this date. Defaults to today.")
