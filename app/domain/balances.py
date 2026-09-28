"""Read-side: the balance numbers the employee sees in the request modal."""

from __future__ import annotations

from datetime import date
from typing import Any

from dataclasses import asdict

from app.external.employee_service import EmployeeService
from app.domain.accrual import rate_for, tenure_tier_years, years_of_service
from app.domain.ledger import Ledger
from app.domain.policies import PolicyService


class BalanceService:
    def __init__(self, ledger: Ledger, policies: PolicyService, employees: EmployeeService) -> None:
        self.ledger = ledger
        self.policies = policies
        self.employees = employees

    def summary(self, employee_id: str, policy_id: str, on_date: date) -> dict[str, Any]:
        employee = self.employees.get_employee(employee_id)
        policy = self.policies.get_policy(policy_id)
        version = self.policies.current_version(policy_id, on_date)

        def amount(minutes: int) -> dict[str, float]:
            return {
                "minutes": minutes,
                "hours": round(minutes / 60, 2),
                "days": round(minutes / employee.minutes_per_day, 2),
            }

        unlimited = version.is_unlimited
        settled = self.ledger.settled_balance(employee_id, policy_id)
        pending = self.ledger.pending_minutes(employee_id, policy_id)
        return {
            "policy_id": policy.id,
            "policy_name": policy.name,
            "time_off_type": policy.time_off_type.value,
            "unlimited": unlimited,
            "balance": None if unlimited else amount(settled),
            "pending": amount(pending),
            "available": None if unlimited else amount(settled - pending),
            "used": amount(self.ledger.used_minutes(employee_id, policy_id)),
            # So the UI can explain how this balance grows, in plain words
            "rules": {
                "accrual_method": version.accrual_method.value,
                "frequency": version.frequency.value if version.frequency else None,
                "accrue_at": version.accrue_at.value,
                "rate": rate_for(version, employee, on_date),
                "rate_unit": version.rate_unit.value,
                "hours_worked_per_accrual": version.hours_worked_per_accrual,
                "max_balance_hours": version.max_balance_hours,
                "carryover_cap_hours": version.carryover_cap_hours,
                "allow_negative": version.allow_negative,
                "max_negative_hours": version.max_negative_hours,
                "tenure_tier_years": tenure_tier_years(version, employee, on_date),
                "years_of_service": years_of_service(employee.hire_date, on_date),
                "tenure_tiers": [asdict(t) for t in version.tenure_tiers],
            },
        }

    def summaries_for(self, employee_id: str, on_date: date) -> list[dict[str, Any]]:
        self.employees.get_employee(employee_id)  # 404 if unknown
        return [
            self.summary(employee_id, a.policy_id, on_date)
            for a in self.policies.active_assignments_for(employee_id, on_date)
        ]
