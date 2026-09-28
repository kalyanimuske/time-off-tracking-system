"""Employee time off requests and admin approvals.

Lifecycle:

    PENDING --approve--> APPROVED --cancel--> CANCELLED
       |  \\--reject---> REJECTED
       \\----cancel----> CANCELLED

Balance effects (all through the ledger):
    create   -> HOLD           (reserves balance so it can't be double-booked)
    approve  -> HOLD_RELEASE + USAGE
    reject   -> HOLD_RELEASE
    cancel   -> HOLD_RELEASE   (if pending) or USAGE_REVERSAL (if approved)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.external.employee_service import EmployeeService
from app.domain.audit import record_audit, snapshot
from app.domain.errors import (
    InsufficientBalanceError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from app.domain.formatting import fmt_range
from app.domain.ledger import Ledger
from app.domain.models import (
    HOLD_TYPES,
    USAGE_TYPES,
    Employee,
    PolicyVersion,
    RequestDay,
    RequestedDay,
    RequestEvent,
    RequestStatus,
    TimeOffRequest,
    TransactionType,
    new_id,
    utc_now,
)
from app.domain.policies import PolicyService
from app.domain.work_calendar import WorkCalendar
from app.storage.memory import InMemoryStore

ALLOWED_TRANSITIONS = {
    RequestStatus.PENDING: {RequestStatus.APPROVED, RequestStatus.REJECTED, RequestStatus.CANCELLED},
    RequestStatus.APPROVED: {RequestStatus.CANCELLED},
    RequestStatus.REJECTED: set(),
    RequestStatus.CANCELLED: set(),
}

SOURCE_TYPE = "time_off_request"


@dataclass
class RequestPreview:
    """What the UI shows before the employee hits "Continue"."""

    counted_days: list[RequestDay]
    skipped_days: list[date]  # weekends / holidays that won't be deducted
    total_minutes: int
    available_before_minutes: Optional[int]  # None for unlimited policies
    available_after_minutes: Optional[int]


def _hours(minutes: int) -> str:
    return f"{minutes / 60:g}h"


class TimeOffRequestService:
    def __init__(
        self,
        store: InMemoryStore,
        ledger: Ledger,
        policies: PolicyService,
        employees: EmployeeService,
        calendar: WorkCalendar,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.policies = policies
        self.employees = employees
        self.calendar = calendar

    # ----- employee actions ---------------------------------------------- #

    def preview(self, employee_id: str, policy_id: str, requested_days: list[RequestedDay]) -> RequestPreview:
        employee = self.employees.get_employee(employee_id)
        self.policies.get_policy(policy_id)
        days, skipped = self._count_days(employee, requested_days)
        version = self._version_for(policy_id, days)
        total = sum(d.minutes for d in days)

        if version.is_unlimited:
            before = after = None
        else:
            before = self.ledger.available_balance(employee_id, policy_id)
            after = before - total
        return RequestPreview(days, skipped, total, before, after)

    def create_request(
        self,
        *,
        employee_id: str,
        policy_id: str,
        reason: str,
        requested_days: list[RequestedDay],
        actor_id: Optional[str] = None,
    ) -> TimeOffRequest:
        if not reason or not reason.strip():
            raise ValidationError("A reason is required")

        employee = self.employees.get_employee(employee_id)
        preview = self.preview(employee_id, policy_id, requested_days)
        self._check_assignment(employee, policy_id, preview.counted_days)
        self._check_no_overlap(employee_id, preview.counted_days)
        version = self._version_for(policy_id, preview.counted_days)
        if not version.is_unlimited:
            self._check_balance(version, preview)

        request = TimeOffRequest(
            id=new_id("req"),
            employee_id=employee_id,
            policy_id=policy_id,
            reason=reason.strip(),
            days=preview.counted_days,
            created_by=actor_id or employee_id,
        )
        self.store.requests[request.id] = request

        if not version.is_unlimited:
            self.ledger.post(
                employee_id=employee_id,
                policy_id=policy_id,
                amount_minutes=-request.total_minutes,
                txn_type=TransactionType.HOLD,
                effective_date=request.start_date,
                idempotency_key=f"hold:{request.id}",
                policy_version_id=version.id,
                source_type=SOURCE_TYPE,
                source_id=request.id,
                created_by=request.created_by,
                note="Reserved while the request is pending",
            )

        self._record_event(request, None, RequestStatus.PENDING, request.created_by, None)
        record_audit(
            self.store,
            entity_type="time_off_request",
            entity_id=request.id,
            action="created",
            actor_id=request.created_by,
            after=snapshot(request),
        )
        return request

    def cancel(
        self,
        request_id: str,
        actor_id: str,
        today: date,
        by_admin: bool = False,
        reason: Optional[str] = None,
    ) -> TimeOffRequest:
        """Employees can cancel their own request before it starts.
        Admins can revoke any approved time off, but must say why."""
        request = self.get_request(request_id)
        reason = reason.strip() if reason and reason.strip() else None
        if not by_admin and actor_id != request.employee_id:
            raise ValidationError("Only the employee or an admin can cancel this request")
        if by_admin and reason is None:
            raise ValidationError("Add a reason. The employee will see it.")
        self._ensure_transition(request, RequestStatus.CANCELLED)

        if request.status == RequestStatus.APPROVED:
            if request.start_date <= today and not by_admin:
                raise ValidationError("This time off has already started. Ask an admin to cancel it.")
            self._reverse_usage(request, actor_id)
        else:
            self._release_hold(request, actor_id, "Cancelled: hours given back")

        request.cancelled_by = actor_id
        request.cancelled_at = utc_now()
        request.cancel_reason = reason
        self._change_status(request, RequestStatus.CANCELLED, actor_id, reason)
        return request

    # ----- admin actions ------------------------------------------------- #

    def approve(self, request_id: str, approver_id: str, note: Optional[str] = None) -> TimeOffRequest:
        request = self.get_request(request_id)
        if approver_id == request.employee_id:
            raise ValidationError("Employees cannot approve their own time off")
        self._ensure_transition(request, RequestStatus.APPROVED)

        version = self._version_for(request.policy_id, request.days)
        # The balance was already reserved by the hold, so no re-check here.
        self._release_hold(request, approver_id, "Approved: replaced by the time off taken")
        self.ledger.post(
            employee_id=request.employee_id,
            policy_id=request.policy_id,
            amount_minutes=-request.total_minutes,
            txn_type=TransactionType.USAGE,
            effective_date=request.start_date,
            idempotency_key=f"usage:{request.id}",
            policy_version_id=version.id,
            source_type=SOURCE_TYPE,
            source_id=request.id,
            created_by=approver_id,
            note=f"Time off {fmt_range(request.start_date, request.end_date)}",
        )
        self._change_status(request, RequestStatus.APPROVED, approver_id, note)
        return request

    def reject(self, request_id: str, approver_id: str, note: Optional[str] = None) -> TimeOffRequest:
        request = self.get_request(request_id)
        self._ensure_transition(request, RequestStatus.REJECTED)
        self._release_hold(request, approver_id, "Rejected: hours given back")
        self._change_status(request, RequestStatus.REJECTED, approver_id, note)
        return request

    # ----- queries ------------------------------------------------------- #

    def get_request(self, request_id: str) -> TimeOffRequest:
        request = self.store.requests.get(request_id)
        if request is None:
            raise NotFoundError(f"Request {request_id} not found")
        return request

    def list_requests(
        self, employee_id: Optional[str] = None, status: Optional[RequestStatus] = None
    ) -> list[TimeOffRequest]:
        return [
            r
            for r in self.store.requests.values()
            if (employee_id is None or r.employee_id == employee_id) and (status is None or r.status == status)
        ]

    def approved_time_off(self, employee_id: str, start: date, end: date) -> list[dict]:
        """Approved time off per day between two dates. This is what the Payroll
        Service asks for, so it can pay paid time off and not count it as worked."""
        days = []
        for request in self.store.requests.values():
            if request.employee_id != employee_id or request.status != RequestStatus.APPROVED:
                continue
            policy = self.store.policies[request.policy_id]
            for d in request.days:
                if start <= d.day <= end and d.minutes > 0:
                    days.append(
                        {
                            "day": d.day,
                            "minutes": d.minutes,
                            "is_paid": policy.is_paid,
                            "policy_name": policy.name,
                            "request_id": request.id,
                        }
                    )
        return sorted(days, key=lambda d: d["day"])

    def events_for(self, request_id: str) -> list[RequestEvent]:
        return [e for e in self.store.request_events if e.request_id == request_id]

    # ----- helpers ------------------------------------------------------- #

    def _count_days(
        self, employee: Employee, requested_days: list[RequestedDay]
    ) -> tuple[list[RequestDay], list[date]]:
        """Turn the selected dates into the minutes that should be deducted.
        Weekends and company holidays are skipped (they cost nothing)."""
        if not requested_days:
            raise ValidationError("Select at least one day")
        dates = [d.day for d in requested_days]
        if len(set(dates)) != len(dates):
            raise ValidationError("The same day was selected twice")

        holidays = self.calendar.holidays_for(employee, min(dates), max(dates))
        counted: list[RequestDay] = []
        skipped: list[date] = []
        for item in sorted(requested_days, key=lambda d: d.day):
            if not self.calendar.is_working_day(employee, item.day, holidays):
                skipped.append(item.day)
                continue
            minutes = employee.minutes_per_day if item.minutes is None else item.minutes
            if minutes <= 0 or minutes > employee.minutes_per_day:
                raise ValidationError(
                    f"{item.day}: partial days must be between 1 and {employee.minutes_per_day} minutes"
                )
            counted.append(RequestDay(day=item.day, minutes=minutes))

        if not counted:
            raise ValidationError("All selected days are weekends or holidays, nothing to request")
        return counted, skipped

    def _version_for(self, policy_id: str, days: list[RequestDay]) -> PolicyVersion:
        version = self.policies.get_version(policy_id, min(d.day for d in days))
        if version is None:
            raise ValidationError("The policy is not in effect on the requested dates")
        return version

    def _check_assignment(self, employee: Employee, policy_id: str, days: list[RequestDay]) -> None:
        for d in days:
            if self.policies.assignment_covering(employee.id, policy_id, d.day) is None:
                raise ValidationError(f"{employee.name} is not assigned to this policy on {d.day}")

    def _check_no_overlap(self, employee_id: str, days: list[RequestDay]) -> None:
        wanted = {d.day for d in days}
        for other in self.store.requests.values():
            if other.employee_id != employee_id:
                continue
            if other.status not in (RequestStatus.PENDING, RequestStatus.APPROVED):
                continue
            clash = wanted & {d.day for d in other.days if d.minutes > 0}
            if clash:
                raise ValidationError(f"Time off is already requested on {min(clash)} (request {other.id})")

    def _check_balance(self, version: PolicyVersion, preview: RequestPreview) -> None:
        floor = -round(version.max_negative_hours * 60) if version.allow_negative else 0
        if preview.available_after_minutes < floor:
            message = (
                f"Not enough balance: this request needs {_hours(preview.total_minutes)} "
                f"but only {_hours(preview.available_before_minutes)} is available"
            )
            if version.allow_negative:
                message += f" (the policy allows going {version.max_negative_hours:g}h negative)"
            raise InsufficientBalanceError(message)

    def _ensure_transition(self, request: TimeOffRequest, new_status: RequestStatus) -> None:
        if new_status not in ALLOWED_TRANSITIONS[request.status]:
            raise InvalidTransitionError(f"Cannot move a {request.status.value} request to {new_status.value}")

    def _release_hold(self, request: TimeOffRequest, actor_id: str, note: str = "") -> None:
        outstanding = -self.ledger.net_for_source(request.id, HOLD_TYPES)
        if outstanding > 0:
            self.ledger.post(
                employee_id=request.employee_id,
                policy_id=request.policy_id,
                amount_minutes=outstanding,
                txn_type=TransactionType.HOLD_RELEASE,
                effective_date=request.start_date,
                idempotency_key=f"hold_release:{request.id}",
                source_type=SOURCE_TYPE,
                source_id=request.id,
                created_by=actor_id,
                note=note,
            )

    def _reverse_usage(self, request: TimeOffRequest, actor_id: str) -> None:
        used = -self.ledger.net_for_source(request.id, USAGE_TYPES)
        if used > 0:
            self.ledger.post(
                employee_id=request.employee_id,
                policy_id=request.policy_id,
                amount_minutes=used,
                txn_type=TransactionType.USAGE_REVERSAL,
                effective_date=request.start_date,
                idempotency_key=f"usage_reversal:{request.id}",
                source_type=SOURCE_TYPE,
                source_id=request.id,
                created_by=actor_id,
                note="Approved time off was cancelled, hours returned",
            )

    def _change_status(
        self, request: TimeOffRequest, new_status: RequestStatus, actor_id: str, note: Optional[str]
    ) -> None:
        before = snapshot(request)
        old_status = request.status
        request.status = new_status
        if new_status in (RequestStatus.APPROVED, RequestStatus.REJECTED):
            request.decided_by = actor_id
            request.decided_at = utc_now()
            request.decision_note = note
        self._record_event(request, old_status, new_status, actor_id, note)
        record_audit(
            self.store,
            entity_type="time_off_request",
            entity_id=request.id,
            action=new_status.value,
            actor_id=actor_id,
            before=before,
            after=snapshot(request),
        )

    def _record_event(
        self,
        request: TimeOffRequest,
        from_status: Optional[RequestStatus],
        to_status: RequestStatus,
        actor_id: str,
        note: Optional[str],
    ) -> None:
        self.store.request_events.append(
            RequestEvent(
                id=new_id("evt"),
                request_id=request.id,
                from_status=from_status,
                to_status=to_status,
                actor_id=actor_id,
                note=note,
            )
        )
