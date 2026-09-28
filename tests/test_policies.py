from datetime import date

import pytest

from app.domain.errors import ValidationError
from tests.helpers import ADMIN, MONTHLY_5_DAYS, assign, create_policy


def test_updating_settings_creates_a_new_version(services):
    policy = create_policy(services, MONTHLY_5_DAYS)

    v2 = services.policies.update_settings(policy.id, {"rate_amount": 2}, date(2026, 2, 1), ADMIN)

    v1 = services.policies.versions(policy.id)[0]
    assert v1.rate_amount == 5  # history untouched
    assert v2.version_number == 2 and v2.rate_amount == 2
    assert v2.frequency == v1.frequency  # unchanged settings are carried over
    assert services.policies.get_version(policy.id, date(2026, 1, 20)) == v1
    assert services.policies.get_version(policy.id, date(2026, 2, 1)) == v2


def test_policy_update_is_audited_with_before_and_after(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    services.policies.update_settings(policy.id, {"rate_amount": 2}, date(2026, 2, 1), ADMIN)

    entry = services.store.audit_log[-1]
    assert entry.action == "settings_updated"
    assert entry.actor_id == ADMIN
    assert entry.before["rate_amount"] == 5
    assert entry.after["rate_amount"] == 2


def test_new_version_must_start_after_the_current_one(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    with pytest.raises(ValidationError, match="must be after"):
        services.policies.update_settings(policy.id, {"rate_amount": 2}, date(2026, 1, 1), ADMIN)


def test_unknown_setting_is_rejected(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    with pytest.raises(ValidationError, match="Unknown"):
        services.policies.update_settings(policy.id, {"rate": 2}, date(2026, 2, 1), ADMIN)


def test_time_based_policy_needs_a_frequency(services):
    with pytest.raises(ValidationError, match="frequency"):
        create_policy(services, {"accrual_method": "time_based", "rate_amount": 5})


def test_negative_balance_needs_an_explicit_limit(services):
    with pytest.raises(ValidationError, match="max_negative_hours"):
        create_policy(services, {**MONTHLY_5_DAYS, "allow_negative": True})


def test_employee_cannot_have_two_policies_of_the_same_type_at_once(services):
    full_time = create_policy(services, MONTHLY_5_DAYS, name="Full-time vacation")
    part_time = create_policy(services, MONTHLY_5_DAYS, name="Part-time vacation")
    first = assign(services, full_time, "alice")

    with pytest.raises(ValidationError, match="already on"):
        assign(services, part_time, "alice", start=date(2026, 6, 1))

    # Moving from full-time to part-time: end the old assignment first.
    services.policies.end_assignment(first.id, date(2026, 5, 31), ADMIN)
    assign(services, part_time, "alice", start=date(2026, 6, 1))


def test_different_types_can_overlap(services):
    vacation = create_policy(services, MONTHLY_5_DAYS)
    sick = create_policy(services, MONTHLY_5_DAYS, name="Sick", time_off_type="sick")
    assign(services, vacation, "alice")
    assign(services, sick, "alice")
    assert len(services.policies.active_assignments_for("alice", date(2026, 3, 1))) == 2


def test_cannot_assign_employee_from_another_company(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    with pytest.raises(ValidationError, match="different companies"):
        assign(services, policy, "carol")
