"""FastAPI app: the REST API plus the web UI (served from app/static).

The API is a thin layer: parse input, check permissions, call a service,
return JSON. All business rules live in app/domain.

Auth is out of scope, so the caller identifies itself with an `X-Actor-Id`
header (the UI sends it from the "Viewing as" switcher). Admin-only endpoints
check that the actor is an admin.

Run:  uvicorn app.main:app --reload   then open http://localhost:8000
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import schemas
from app.external.events import ON_PAYROLL_PROCESSED, PayrollEntry, PayrollProcessedEvent
from app.external.payroll_service import PayrollRun, pay_periods_before
from app.demo_data import build_demo_services
from app.domain.errors import (
    InsufficientBalanceError,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
    TimeOffError,
    ValidationError,
)
from app.domain.models import Employee, Policy, RequestedDay, RequestStatus, TimeOffRequest
from app.services import Services

STATIC_DIR = Path(__file__).parent / "static"

ERROR_STATUS_CODES = {
    NotFoundError: 404,
    PermissionDeniedError: 403,
    InvalidTransitionError: 409,
    InsufficientBalanceError: 422,
    ValidationError: 400,
}


def to_requested_days(days: list[schemas.RequestedDayIn]) -> list[RequestedDay]:
    return [RequestedDay(day=d.day, minutes=d.minutes) for d in days]


def create_app(services: Optional[Services] = None) -> FastAPI:
    """Pass `services` in tests. Without it, the app starts with demo data."""
    app = FastAPI(
        title="Warp Time Off",
        version="1.0.0",
        description="Time off policies, accruals, requests and approvals. UI at `/`.",
    )
    app.state.demo_mode = services is None
    app.state.services = services if services is not None else build_demo_services()

    def svc() -> Services:
        return app.state.services

    # ----- helpers ------------------------------------------------------- #

    def require_admin(actor_id: Optional[str]) -> str:
        if not actor_id:
            raise PermissionDeniedError("Missing X-Actor-Id header")
        try:
            actor = svc().employees.get_employee(actor_id)
        except NotFoundError:
            raise PermissionDeniedError(f"Unknown user {actor_id}") from None
        if not actor.is_admin:
            raise PermissionDeniedError("Only admins can do this")
        return actor_id

    def employee_name(employee_id: str) -> str:
        try:
            return svc().employees.get_employee(employee_id).name
        except NotFoundError:
            return employee_id

    def employee_dict(employee: Employee) -> dict[str, Any]:
        data = jsonable_encoder(employee)
        data["minutes_per_day"] = employee.minutes_per_day
        return data

    def payroll_run_dict(run: PayrollRun) -> dict[str, Any]:
        data = jsonable_encoder(run)
        data.update(
            total_gross=run.total_gross,
            total_net=run.total_net,
            processed_by_name=employee_name(run.processed_by),
            is_outdated=svc().payroll.is_outdated(run),
        )
        return data

    def request_dict(request: TimeOffRequest) -> dict[str, Any]:
        s = svc()
        data = jsonable_encoder(request)
        policy = s.store.policies.get(request.policy_id)
        employee = s.employees.get_employee(request.employee_id)
        data.update(
            total_minutes=request.total_minutes,
            start_date=request.start_date.isoformat(),
            end_date=request.end_date.isoformat(),
            employee_name=employee.name,
            minutes_per_day=employee.minutes_per_day,
            policy_name=policy.name if policy else None,
            time_off_type=policy.time_off_type.value if policy else None,
        )
        return data

    def policy_dict(policy: Policy, on_date: date) -> dict[str, Any]:
        s = svc()
        versions = s.policies.versions(policy.id)
        upcoming = [v for v in versions if v.effective_from > on_date]
        assignments = sorted(
            (a for a in s.store.assignments.values() if a.policy_id == policy.id),
            key=lambda a: (a.start_date, a.employee_id),
        )
        return jsonable_encoder(
            {
                "policy": policy,
                "current_version": s.policies.current_version(policy.id, on_date),
                "upcoming_version": upcoming[0] if upcoming else None,
                "version_count": len(versions),
                "assignments": [
                    {**jsonable_encoder(a), "employee_name": employee_name(a.employee_id)} for a in assignments
                ],
            }
        )

    @app.exception_handler(TimeOffError)
    async def handle_domain_error(_: Request, exc: TimeOffError) -> JSONResponse:
        status = next((code for cls, code in ERROR_STATUS_CODES.items() if isinstance(exc, cls)), 400)
        return JSONResponse(status_code=status, content={"error": type(exc).__name__, "detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ----- simulated existing services: Company, Employee, Payroll ------ #

    @app.get("/companies", tags=["Company Service (simulated)"])
    def list_companies():
        return jsonable_encoder(svc().companies.list_companies())

    @app.get("/companies/{company_id}", tags=["Company Service (simulated)"])
    def get_company(company_id: str):
        return jsonable_encoder(svc().companies.get_company(company_id))

    @app.post("/payroll/preview", tags=["Payroll Service (simulated)"])
    def preview_payroll(body: schemas.PayrollIn):
        lines = svc().payroll.calculate(body.company_id, body.period_start, body.period_end, body.hours_overrides)
        return jsonable_encoder(lines)

    @app.post("/payroll/runs", status_code=201, tags=["Payroll Service (simulated)"])
    def run_payroll(body: schemas.PayrollIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        require_admin(actor_id)
        run = svc().payroll.run(
            body.company_id, body.period_start, body.period_end, actor_id, date.today(), body.hours_overrides
        )
        return payroll_run_dict(run)

    @app.get("/payroll/runs", tags=["Payroll Service (simulated)"])
    def list_payroll_runs(company_id: str):
        return [payroll_run_dict(r) for r in svc().payroll.list_runs(company_id)]

    @app.get("/payroll/periods", tags=["Payroll Service (simulated)"])
    def payroll_periods(count: int = 6):
        """The most recent pay periods that have ended (semi-monthly)."""
        return [{"period_start": s, "period_end": e} for s, e in pay_periods_before(date.today(), count)]

    @app.get("/events", tags=["Payroll Service (simulated)"])
    def event_log():
        return jsonable_encoder(list(reversed(svc().bus.log)))

    # ----- employees (read from the Employee Service) -------------------- #

    @app.get("/employees")
    def list_employees():
        return [employee_dict(e) for e in svc().employees.list_all()]

    @app.get("/employees/{employee_id}")
    def get_employee(employee_id: str):
        return employee_dict(svc().employees.get_employee(employee_id))

    # ----- policies ------------------------------------------------------ #

    @app.post("/policies", status_code=201)
    def create_policy(body: schemas.CreatePolicyIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        require_admin(actor_id)
        policy, version = svc().policies.create_policy(
            company_id=body.company_id,
            name=body.name,
            time_off_type=body.time_off_type.value,
            description=body.description,
            is_paid=body.is_paid,
            effective_from=body.effective_from,
            settings=body.settings.model_dump(),
            actor_id=actor_id,
        )
        return jsonable_encoder({"policy": policy, "version": version})

    @app.get("/policies")
    def list_policies(company_id: Optional[str] = None, on_date: Optional[date] = None):
        on_date = on_date or date.today()
        return [policy_dict(p, on_date) for p in svc().policies.list_policies(company_id)]

    @app.get("/policies/{policy_id}")
    def get_policy(policy_id: str):
        s = svc()
        return jsonable_encoder({"policy": s.policies.get_policy(policy_id), "versions": s.policies.versions(policy_id)})

    @app.patch("/policies/{policy_id}")
    def update_policy(
        policy_id: str, body: schemas.UpdatePolicyIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")
    ):
        require_admin(actor_id)
        return jsonable_encoder(svc().policies.update_settings(policy_id, body.changes, body.effective_from, actor_id))

    @app.post("/policies/{policy_id}/assignments", status_code=201)
    def assign(policy_id: str, body: schemas.AssignIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        require_admin(actor_id)
        assignment = svc().policies.assign_employee(
            policy_id, body.employee_id, body.start_date, actor_id=actor_id, end_date=body.end_date
        )
        return jsonable_encoder(assignment)

    @app.post("/assignments/{assignment_id}/end")
    def end_assignment(
        assignment_id: str, body: schemas.EndAssignmentIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")
    ):
        require_admin(actor_id)
        return jsonable_encoder(svc().policies.end_assignment(assignment_id, body.end_date, actor_id))

    # ----- holidays ------------------------------------------------------ #

    @app.post("/holidays", status_code=201)
    def add_holiday(body: schemas.HolidayIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        require_admin(actor_id)
        holiday, refunded = svc().holidays.add_holiday(
            company_id=body.company_id, day=body.day, name=body.name, location=body.location, actor_id=actor_id
        )
        return jsonable_encoder({"holiday": holiday, "refunded_request_ids": refunded})

    @app.get("/holidays")
    def list_holidays(company_id: str):
        return jsonable_encoder(svc().holidays.list_holidays(company_id))

    # ----- balances ------------------------------------------------------ #

    @app.get("/employees/{employee_id}/balances")
    def balances(employee_id: str, on_date: Optional[date] = None):
        return svc().balances.summaries_for(employee_id, on_date or date.today())

    @app.get("/employees/{employee_id}/approved-time-off")
    def approved_time_off(employee_id: str, start: date, end: date):
        """What the Payroll Service calls to learn about approved time off in a pay period."""
        s = svc()
        s.employees.get_employee(employee_id)
        return jsonable_encoder(s.requests.approved_time_off(employee_id, start, end))

    @app.get("/employees/{employee_id}/policies/{policy_id}/transactions")
    def transactions(employee_id: str, policy_id: str):
        s = svc()
        s.employees.get_employee(employee_id)
        s.policies.get_policy(policy_id)
        return jsonable_encoder(s.ledger.transactions(employee_id, policy_id))

    # ----- time off requests --------------------------------------------- #

    @app.post("/time-off-requests/preview")
    def preview_request(body: schemas.PreviewRequestIn):
        preview = svc().requests.preview(body.employee_id, body.policy_id, to_requested_days(body.days))
        return jsonable_encoder(preview)

    @app.post("/time-off-requests", status_code=201)
    def create_request(body: schemas.CreateRequestIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        request = svc().requests.create_request(
            employee_id=body.employee_id,
            policy_id=body.policy_id,
            reason=body.reason,
            requested_days=to_requested_days(body.days),
            actor_id=actor_id,
        )
        return request_dict(request)

    @app.get("/time-off-requests")
    def list_requests(employee_id: Optional[str] = None, status: Optional[RequestStatus] = None):
        return [request_dict(r) for r in svc().requests.list_requests(employee_id, status)]

    @app.get("/time-off-requests/{request_id}")
    def get_request(request_id: str):
        s = svc()
        request = s.requests.get_request(request_id)
        return {**request_dict(request), "events": jsonable_encoder(s.requests.events_for(request_id))}

    @app.post("/time-off-requests/{request_id}/approve")
    def approve(
        request_id: str, body: schemas.DecisionIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")
    ):
        require_admin(actor_id)
        return request_dict(svc().requests.approve(request_id, actor_id, body.note))

    @app.post("/time-off-requests/{request_id}/reject")
    def reject(request_id: str, body: schemas.DecisionIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        require_admin(actor_id)
        return request_dict(svc().requests.reject(request_id, actor_id, body.note))

    @app.post("/time-off-requests/{request_id}/cancel")
    def cancel(request_id: str, body: schemas.CancelIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        if not actor_id:
            raise PermissionDeniedError("Missing X-Actor-Id header")
        if body.by_admin:
            require_admin(actor_id)
        request = svc().requests.cancel(request_id, actor_id, date.today(), by_admin=body.by_admin, reason=body.reason)
        return request_dict(request)

    # ----- jobs and events (called by the scheduler / Payroll Service) ---- #

    @app.post("/jobs/run-accruals")
    def run_accruals(body: schemas.RunAccrualsIn):
        posted = svc().accruals.run_time_based_accruals(body.as_of or date.today())
        return {"posted": len(posted), "transactions": jsonable_encoder(posted)}

    @app.post("/jobs/year-end")
    def year_end(body: schemas.YearEndIn, actor_id: Optional[str] = Header(None, alias="X-Actor-Id")):
        s = svc()
        run_date = body.run_date or date.today()
        if run_date > date(body.year, 12, 31):
            # Make sure every accrual of the year is posted before we look at balances.
            s.accruals.run_time_based_accruals(as_of=date(body.year, 12, 31))
        posted = s.year_end.run(body.policy_id, body.year, actor_id or "system", run_date=run_date)
        return {"expired": len(posted), "transactions": jsonable_encoder(posted)}

    @app.post("/events/payroll-processed")
    def payroll_processed(body: schemas.PayrollProcessedIn):
        event = PayrollProcessedEvent(
            run_id=body.run_id,
            company_id=body.company_id,
            period_start=body.period_start,
            period_end=body.period_end,
            entries=[PayrollEntry(e.employee_id, e.hours_worked) for e in body.entries],
        )
        posted = [t for result in svc().bus.publish(ON_PAYROLL_PROCESSED, event) for t in (result or [])]
        return {"posted": len(posted), "transactions": jsonable_encoder(posted)}

    # ----- audit --------------------------------------------------------- #

    @app.get("/audit-log")
    def audit_log(entity_type: Optional[str] = None, entity_id: Optional[str] = None):
        entries = [
            e
            for e in svc().store.audit_log
            if (entity_type is None or e.entity_type == entity_type) and (entity_id is None or e.entity_id == entity_id)
        ]
        return jsonable_encoder(entries)

    # ----- demo helpers -------------------------------------------------- #

    @app.post("/demo/reset")
    def reset_demo():
        if not app.state.demo_mode:
            raise ValidationError("Reset is only available when running with demo data")
        app.state.services = build_demo_services()
        return {"status": "reset"}

    # ----- web UI -------------------------------------------------------- #

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
