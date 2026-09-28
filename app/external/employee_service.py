"""Simulated Employee Service (an existing service, per the prompt).

Returns employees with their pay rates and work configuration. The Time Off
Service only reads from it: hire date, hours per day, work days, location,
termination date. Pay data is used by the simulated Payroll Service.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.domain.errors import NotFoundError
from app.domain.models import Employee


class EmployeeService:
    def __init__(self, employees: Optional[Iterable[Employee]] = None) -> None:
        self._employees: dict[str, Employee] = {e.id: e for e in employees or []}

    def add(self, employee: Employee) -> None:
        self._employees[employee.id] = employee

    def get_employee(self, employee_id: str) -> Employee:
        employee = self._employees.get(employee_id)
        if employee is None:
            raise NotFoundError(f"Employee {employee_id} not found")
        return employee

    def list_employees(self, company_id: str) -> list[Employee]:
        return [e for e in self._employees.values() if e.company_id == company_id]

    def list_all(self) -> list[Employee]:
        return list(self._employees.values())
