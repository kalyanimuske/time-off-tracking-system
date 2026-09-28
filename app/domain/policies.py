"""Creating, versioning and assigning policies."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from app.external.company_service import CompanyService
from app.external.employee_service import EmployeeService
from app.domain.audit import record_audit, snapshot
from app.domain.errors import NotFoundError, ValidationError
from app.domain.models import (
    POLICY_SETTING_FIELDS,
    AccrualFrequency,
    AccrualMethod,
    AccrualTiming,
    Policy,
    PolicyAssignment,
    PolicyVersion,
    ProrationRule,
    RateUnit,
    TenureTier,
    TimeOffType,
    new_id,
)
from app.storage.memory import InMemoryStore

_ENUM_FIELDS = {
    "frequency": AccrualFrequency,
    "accrue_at": AccrualTiming,
    "rate_unit": RateUnit,
    "proration_rule": ProrationRule,
}


def validate_version(v: PolicyVersion) -> None:
    """Business rules for a policy's settings. Raises ValidationError."""
    if v.rate_amount < 0:
        raise ValidationError("rate_amount cannot be negative")

    if v.accrual_method == AccrualMethod.TIME_BASED:
        if v.frequency is None:
            raise ValidationError("Time-based policies need a frequency (monthly or yearly)")
        if v.rate_amount <= 0 and not v.tenure_tiers:
            raise ValidationError("Time-based policies need a rate_amount greater than 0")

    if v.accrual_method == AccrualMethod.HOURS_WORKED:
        if not v.hours_worked_per_accrual or v.hours_worked_per_accrual <= 0:
            raise ValidationError("Hours-worked policies need hours_worked_per_accrual greater than 0")
        if v.rate_amount <= 0:
            raise ValidationError("Hours-worked policies need a rate_amount greater than 0")

    if v.allow_negative and v.max_negative_hours <= 0:
        # Unlimited negative balances are a real financial risk for the
        # company, so we force admins to choose an explicit limit.
        raise ValidationError("When allow_negative is true, max_negative_hours must be greater than 0")
    if v.max_negative_hours < 0:
        raise ValidationError("max_negative_hours cannot be negative")
    if v.max_balance_hours is not None and v.max_balance_hours <= 0:
        raise ValidationError("max_balance_hours must be greater than 0")
    if v.carryover_cap_hours is not None and v.carryover_cap_hours < 0:
        raise ValidationError("carryover_cap_hours cannot be negative")
    if v.waiting_period_days < 0:
        raise ValidationError("waiting_period_days cannot be negative")
    for tier in v.tenure_tiers:
        if tier.min_years < 0 or tier.rate_amount < 0:
            raise ValidationError("Tenure tiers need min_years >= 0 and rate_amount >= 0")


class PolicyService:
    def __init__(self, store: InMemoryStore, employees: EmployeeService, companies: CompanyService) -> None:
        self.store = store
        self.employees = employees
        self.companies = companies

    # ----- policies ------------------------------------------------------ #

    def create_policy(
        self,
        *,
        company_id: str,
        name: str,
        time_off_type: str,
        settings: dict[str, Any],
        effective_from: date,
        actor_id: str,
        description: str = "",
        is_paid: bool = True,
    ) -> tuple[Policy, PolicyVersion]:
        self.companies.get_company(company_id)  # 404 if the company doesn't exist
        if not name or not name.strip():
            raise ValidationError("Policy name is required")
        try:
            off_type = TimeOffType(time_off_type)
        except ValueError as exc:
            raise ValidationError(f"Unknown time off type: {time_off_type}") from exc

        policy = Policy(
            id=new_id("pol"),
            company_id=company_id,
            name=name.strip(),
            time_off_type=off_type,
            description=description,
            is_paid=is_paid,
            created_by=actor_id,
        )
        version = self._build_version(policy.id, 1, effective_from, settings, actor_id)

        self.store.policies[policy.id] = policy
        self.store.policy_versions[policy.id] = [version]
        record_audit(
            self.store,
            entity_type="policy",
            entity_id=policy.id,
            action="created",
            actor_id=actor_id,
            after={"policy": snapshot(policy), "version": snapshot(version)},
        )
        return policy, version

    def update_settings(
        self, policy_id: str, changes: dict[str, Any], effective_from: date, actor_id: str
    ) -> PolicyVersion:
        """Change a policy's accrual rules from `effective_from` onwards.

        We never edit the old version. Past accruals stay linked to the rules
        that produced them, and the new rules apply from the next accrual
        period that starts on or after `effective_from`.
        """
        versions = self._versions(policy_id)
        latest = versions[-1]
        if effective_from <= latest.effective_from:
            raise ValidationError(
                f"effective_from must be after the current version's start date ({latest.effective_from})"
            )
        unknown = set(changes) - set(POLICY_SETTING_FIELDS)
        if unknown:
            raise ValidationError(f"Unknown policy settings: {sorted(unknown)}")

        settings = {name: getattr(latest, name) for name in POLICY_SETTING_FIELDS}
        settings.update(changes)
        new_version = self._build_version(
            policy_id, latest.version_number + 1, effective_from, settings, actor_id
        )
        versions.append(new_version)
        record_audit(
            self.store,
            entity_type="policy",
            entity_id=policy_id,
            action="settings_updated",
            actor_id=actor_id,
            before=snapshot(latest),
            after=snapshot(new_version),
        )
        return new_version

    def get_policy(self, policy_id: str) -> Policy:
        policy = self.store.policies.get(policy_id)
        if policy is None:
            raise NotFoundError(f"Policy {policy_id} not found")
        return policy

    def list_policies(self, company_id: Optional[str] = None) -> list[Policy]:
        return [p for p in self.store.policies.values() if company_id is None or p.company_id == company_id]

    def versions(self, policy_id: str) -> list[PolicyVersion]:
        return list(self._versions(policy_id))

    def get_version(self, policy_id: str, on_date: date) -> Optional[PolicyVersion]:
        """The version in effect on a given date (None if the policy didn't exist yet)."""
        found = None
        for version in self._versions(policy_id):
            if version.effective_from <= on_date:
                found = version
        return found

    def current_version(self, policy_id: str, on_date: date) -> PolicyVersion:
        return self.get_version(policy_id, on_date) or self._versions(policy_id)[0]

    def next_version_date(self, policy_id: str, after: date) -> Optional[date]:
        for version in self._versions(policy_id):
            if version.effective_from > after:
                return version.effective_from
        return None

    # ----- assignments --------------------------------------------------- #

    def assign_employee(
        self,
        policy_id: str,
        employee_id: str,
        start_date: date,
        actor_id: str,
        end_date: Optional[date] = None,
    ) -> PolicyAssignment:
        policy = self.get_policy(policy_id)
        employee = self.employees.get_employee(employee_id)
        if employee.company_id != policy.company_id:
            raise ValidationError("Employee and policy belong to different companies")
        if end_date is not None and end_date < start_date:
            raise ValidationError("end_date cannot be before start_date")

        # An employee can only be on one policy per time off type at a time,
        # otherwise we wouldn't know which vacation balance to use.
        for existing in self.store.assignments.values():
            if existing.employee_id != employee_id:
                continue
            other = self.store.policies[existing.policy_id]
            if other.time_off_type == policy.time_off_type and existing.overlaps(start_date, end_date):
                raise ValidationError(
                    f"{employee.name} is already on the {policy.time_off_type.value} policy "
                    f"'{other.name}' during this period. End that assignment first."
                )

        assignment = PolicyAssignment(
            id=new_id("asg"),
            policy_id=policy_id,
            employee_id=employee_id,
            start_date=start_date,
            end_date=end_date,
            created_by=actor_id,
        )
        self.store.assignments[assignment.id] = assignment
        record_audit(
            self.store,
            entity_type="policy_assignment",
            entity_id=assignment.id,
            action="created",
            actor_id=actor_id,
            after=snapshot(assignment),
        )
        return assignment

    def end_assignment(self, assignment_id: str, end_date: date, actor_id: str) -> PolicyAssignment:
        assignment = self.store.assignments.get(assignment_id)
        if assignment is None:
            raise NotFoundError(f"Assignment {assignment_id} not found")
        if end_date < assignment.start_date:
            raise ValidationError("end_date cannot be before the assignment's start_date")
        before = snapshot(assignment)
        assignment.end_date = end_date
        record_audit(
            self.store,
            entity_type="policy_assignment",
            entity_id=assignment.id,
            action="ended",
            actor_id=actor_id,
            before=before,
            after=snapshot(assignment),
        )
        return assignment

    def active_assignments_for(self, employee_id: str, on_date: date) -> list[PolicyAssignment]:
        return [
            a for a in self.store.assignments.values() if a.employee_id == employee_id and a.covers(on_date)
        ]

    def assignment_covering(self, employee_id: str, policy_id: str, day: date) -> Optional[PolicyAssignment]:
        for a in self.store.assignments.values():
            if a.employee_id == employee_id and a.policy_id == policy_id and a.covers(day):
                return a
        return None

    # ----- helpers ------------------------------------------------------- #

    def _versions(self, policy_id: str) -> list[PolicyVersion]:
        self.get_policy(policy_id)  # raises NotFoundError
        return self.store.policy_versions[policy_id]

    def _build_version(
        self,
        policy_id: str,
        number: int,
        effective_from: date,
        settings: dict[str, Any],
        actor_id: str,
    ) -> PolicyVersion:
        unknown = set(settings) - set(POLICY_SETTING_FIELDS)
        if unknown:
            raise ValidationError(f"Unknown policy settings: {sorted(unknown)}")
        if settings.get("accrual_method") is None:
            raise ValidationError("accrual_method is required")

        # Missing / None values fall back to the dataclass defaults.
        values = {key: value for key, value in settings.items() if value is not None}
        try:
            values["accrual_method"] = AccrualMethod(values["accrual_method"])
            for key, enum_cls in _ENUM_FIELDS.items():
                if key in values:
                    values[key] = enum_cls(values[key])
            values["tenure_tiers"] = [
                tier if isinstance(tier, TenureTier) else TenureTier(**tier)
                for tier in values.get("tenure_tiers", [])
            ]
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"Invalid policy settings: {exc}") from exc

        version = PolicyVersion(
            id=new_id("polv"),
            policy_id=policy_id,
            version_number=number,
            effective_from=effective_from,
            created_by=actor_id,
            **values,
        )
        validate_version(version)
        return version
