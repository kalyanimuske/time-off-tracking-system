"""Wires all the services together (a very small dependency container).

The Time Off Service is everything in app/domain. The simulated existing
services (Employee, Company, Payroll) are in app/external. The accrual engine
subscribes to on_payroll_processed here, which is the only link from payroll
into time off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.domain.accrual import AccrualService
from app.domain.balances import BalanceService
from app.domain.holidays import HolidayService
from app.domain.ledger import Ledger
from app.domain.policies import PolicyService
from app.domain.time_off_requests import TimeOffRequestService
from app.domain.work_calendar import WorkCalendar
from app.domain.year_end import YearEndService
from app.external.company_service import CompanyService
from app.external.employee_service import EmployeeService
from app.external.events import ON_PAYROLL_PROCESSED, EventBus
from app.external.payroll_service import PayrollService
from app.storage.memory import InMemoryStore


@dataclass
class Services:
    # simulated existing services
    employees: EmployeeService
    companies: CompanyService
    payroll: PayrollService
    bus: EventBus
    # the Time Off Service
    store: InMemoryStore
    ledger: Ledger
    calendar: WorkCalendar
    policies: PolicyService
    accruals: AccrualService
    requests: TimeOffRequestService
    holidays: HolidayService
    year_end: YearEndService
    balances: BalanceService


def build_services(
    employees: Optional[EmployeeService] = None,
    companies: Optional[CompanyService] = None,
    store: Optional[InMemoryStore] = None,
) -> Services:
    store = store or InMemoryStore()
    employees = employees or EmployeeService()
    companies = companies or CompanyService()
    bus = EventBus()

    ledger = Ledger(store)
    calendar = WorkCalendar(store)
    policies = PolicyService(store, employees, companies)
    accruals = AccrualService(store, ledger, policies, employees)
    requests = TimeOffRequestService(store, ledger, policies, employees, calendar)

    # Time Off subscribes to the Payroll Service's event
    bus.subscribe(ON_PAYROLL_PROCESSED, accruals.handle_payroll_processed)
    payroll = PayrollService(employees, companies, requests, calendar, bus)

    return Services(
        employees=employees,
        companies=companies,
        payroll=payroll,
        bus=bus,
        store=store,
        ledger=ledger,
        calendar=calendar,
        policies=policies,
        accruals=accruals,
        requests=requests,
        holidays=HolidayService(store, ledger, employees, companies),
        year_end=YearEndService(store, ledger, policies),
        balances=BalanceService(ledger, policies, employees),
    )
