"""Company holidays.

Holidays are skipped when a request is created (see TimeOffRequestService).
The tricky case is a holiday added AFTER someone already requested that day:
we refund that day instead of silently leaving them charged for it.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from app.external.company_service import CompanyService
from app.external.employee_service import EmployeeService
from app.domain.audit import record_audit, snapshot
from app.domain.errors import ValidationError
from app.domain.ledger import Ledger
from app.domain.models import HOLD_TYPES, Holiday, RequestStatus, TransactionType, new_id
from app.storage.memory import InMemoryStore


class HolidayService:
    def __init__(
        self, store: InMemoryStore, ledger: Ledger, employees: EmployeeService, companies: CompanyService
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.employees = employees
        self.companies = companies

    def list_holidays(self, company_id: str) -> list[Holiday]:
        return sorted(
            (h for h in self.store.holidays.values() if h.company_id == company_id), key=lambda h: h.day
        )

    def add_holiday(
        self,
        *,
        company_id: str,
        day: date,
        name: str,
        actor_id: str,
        location: Optional[str] = None,
    ) -> tuple[Holiday, list[str]]:
        """Add a holiday. Returns the holiday and the ids of requests that were refunded."""
        company = self.companies.get_company(company_id)
        if location is not None and location not in company.locations:
            raise ValidationError(f"{company.name} has no location {location}. Locations: {', '.join(company.locations)}")
        for existing in self.store.holidays.values():
            if existing.company_id == company_id and existing.day == day and existing.location == location:
                raise ValidationError(f"There is already a holiday on {day}: {existing.name}")

        holiday = Holiday(
            id=new_id("hol"), company_id=company_id, day=day, name=name, location=location, created_by=actor_id
        )
        self.store.holidays[holiday.id] = holiday
        record_audit(
            self.store,
            entity_type="holiday",
            entity_id=holiday.id,
            action="created",
            actor_id=actor_id,
            after=snapshot(holiday),
        )
        return holiday, self._refund_requests_on(holiday, actor_id)

    def _refund_requests_on(self, holiday: Holiday, actor_id: str) -> list[str]:
        refunded: list[str] = []
        for request in self.store.requests.values():
            if request.status not in (RequestStatus.PENDING, RequestStatus.APPROVED):
                continue
            employee = self.employees.get_employee(request.employee_id)
            if employee.company_id != holiday.company_id:
                continue
            if holiday.location is not None and employee.location != holiday.location:
                continue

            for request_day in request.days:
                if request_day.day != holiday.day or request_day.minutes == 0:
                    continue
                refund = request_day.minutes
                request_day.minutes = 0

                if request.status == RequestStatus.PENDING:
                    txn_type = TransactionType.HOLD_RELEASE
                    # Unlimited policies never placed a hold, so only release what exists.
                    refund = min(refund, -self.ledger.net_for_source(request.id, HOLD_TYPES))
                else:
                    txn_type = TransactionType.USAGE_REVERSAL

                if refund > 0:
                    self.ledger.post(
                        employee_id=request.employee_id,
                        policy_id=request.policy_id,
                        amount_minutes=refund,
                        txn_type=txn_type,
                        effective_date=holiday.day,
                        idempotency_key=f"holiday:{holiday.id}:{request.id}",
                        source_type="time_off_request",
                        source_id=request.id,
                        created_by=actor_id,
                        note=f"{holiday.name} was added as a company holiday",
                    )
                refunded.append(request.id)
                record_audit(
                    self.store,
                    entity_type="time_off_request",
                    entity_id=request.id,
                    action="holiday_refund",
                    actor_id=actor_id,
                    after={"holiday_id": holiday.id, "day": holiday.day.isoformat()},
                )
        return refunded
