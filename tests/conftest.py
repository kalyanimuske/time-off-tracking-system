from datetime import date

import pytest

from app.domain.models import Company, Employee
from app.external.company_service import CompanyService
from app.external.employee_service import EmployeeService
from app.services import build_services


@pytest.fixture
def employees():
    return EmployeeService(
        [
            # 8-hour days, hired years ago, based in California
            Employee(id="alice", company_id="acme", name="Alice", hire_date=date(2022, 1, 10), location="CA"),
            # 6-hour days, joins halfway through 2026, based in New York
            Employee(id="bob", company_id="acme", name="Bob", hire_date=date(2026, 7, 1), hours_per_day=6, location="NY"),
            # Different company
            Employee(id="carol", company_id="other_co", name="Carol", hire_date=date(2020, 1, 1)),
            # HR admin who approves requests
            Employee(id="admin_1", company_id="acme", name="Admin", hire_date=date(2020, 1, 1), is_admin=True),
        ]
    )


@pytest.fixture
def companies():
    return CompanyService(
        [
            Company(id="acme", name="Acme Inc.", locations=["CA", "NY"]),
            Company(id="other_co", name="Other Co", locations=["TX"]),
        ]
    )


@pytest.fixture
def services(employees, companies):
    return build_services(employees=employees, companies=companies)
