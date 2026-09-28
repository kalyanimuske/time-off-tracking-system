from datetime import date

import pytest

from app.external.events import PayrollEntry, PayrollProcessedEvent
from app.domain.accrual import proration_factor, years_of_service
from app.domain.models import ProrationRule, TransactionType
from tests.helpers import DAY_6H, DAY_8H, MONTHLY_5_DAYS, assign, create_policy


def balance(services, employee_id, policy):
    return services.ledger.settled_balance(employee_id, policy.id)


# ----- time-based ------------------------------------------------------- #


def test_monthly_accrual_at_start_of_period(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    assign(services, policy, "alice")

    posted = services.accruals.run_time_based_accruals(as_of=date(2026, 3, 15))

    assert len(posted) == 3  # Jan, Feb, Mar
    assert balance(services, "alice", policy) == 15 * DAY_8H


def test_accrual_job_is_idempotent(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    assign(services, policy, "alice")

    services.accruals.run_time_based_accruals(as_of=date(2026, 3, 15))
    second_run = services.accruals.run_time_based_accruals(as_of=date(2026, 3, 15))

    assert second_run == []
    assert balance(services, "alice", policy) == 15 * DAY_8H


def test_accrual_job_catches_up_after_missed_runs(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    assign(services, policy, "alice")

    assert len(services.accruals.run_time_based_accruals(as_of=date(2026, 1, 5))) == 1
    # The job didn't run for months (outage). The next run backfills.
    assert len(services.accruals.run_time_based_accruals(as_of=date(2026, 4, 2))) == 3
    assert balance(services, "alice", policy) == 20 * DAY_8H


def test_end_of_period_accrual_waits_until_period_is_over(services):
    policy = create_policy(services, {**MONTHLY_5_DAYS, "accrue_at": "end_of_period"})
    assign(services, policy, "alice")

    services.accruals.run_time_based_accruals(as_of=date(2026, 3, 15))

    assert balance(services, "alice", policy) == 10 * DAY_8H  # Jan + Feb only


def test_joining_halfway_through_the_year_is_prorated(services):
    """The spec's example: 20 days/year, employee joins halfway."""
    policy = create_policy(services, {"accrual_method": "time_based", "frequency": "yearly", "rate_amount": 20})
    assign(services, policy, "bob", start=date(2026, 7, 1))

    [txn] = services.accruals.run_time_based_accruals(as_of=date(2026, 7, 1))

    assert txn.metadata["proration_factor"] == pytest.approx(184 / 365, abs=1e-4)
    assert txn.amount_minutes / DAY_6H == pytest.approx(10.08, abs=0.01)


def test_proration_rule_full_gives_the_whole_amount(services):
    policy = create_policy(
        services, {"accrual_method": "time_based", "frequency": "yearly", "rate_amount": 20, "proration_rule": "full"}
    )
    assign(services, policy, "bob", start=date(2026, 7, 1))

    services.accruals.run_time_based_accruals(as_of=date(2026, 7, 1))

    assert balance(services, "bob", policy) == 20 * DAY_6H


def test_proration_rule_next_period_starts_accruing_next_year(services):
    policy = create_policy(
        services,
        {"accrual_method": "time_based", "frequency": "yearly", "rate_amount": 20, "proration_rule": "next_period"},
    )
    assign(services, policy, "bob", start=date(2026, 7, 1))

    services.accruals.run_time_based_accruals(as_of=date(2027, 1, 2))

    assert balance(services, "bob", policy) == 20 * DAY_6H  # 0 for 2026 + 20 for 2027


def test_six_hour_employee_accrues_six_hour_days(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    assign(services, policy, "bob", start=date(2026, 8, 1))

    services.accruals.run_time_based_accruals(as_of=date(2026, 8, 1))

    assert balance(services, "bob", policy) == 5 * DAY_6H  # 30 hours, still "5 days"


def test_tenure_tiers_increase_the_rate(services):
    policy = create_policy(
        services,
        {
            "accrual_method": "time_based",
            "frequency": "yearly",
            "rate_amount": 15,
            "tenure_tiers": [{"min_years": 0, "rate_amount": 15}, {"min_years": 5, "rate_amount": 25}],
        },
    )
    assign(services, policy, "alice")  # hired 2022-01-10

    posted = services.accruals.run_time_based_accruals(as_of=date(2028, 1, 5))

    assert [t.amount_minutes // DAY_8H for t in posted] == [15, 15, 25]
    assert posted[-1].metadata["years_of_service"] == 5


def test_max_balance_cap_stops_accrual(services):
    policy = create_policy(services, {**MONTHLY_5_DAYS, "max_balance_hours": 100})
    assign(services, policy, "alice")

    posted = services.accruals.run_time_based_accruals(as_of=date(2026, 4, 15))

    assert balance(services, "alice", policy) == 100 * 60
    assert posted[-1].amount_minutes == 0  # April: already at the cap
    assert "capped" in posted[-2].note


def test_waiting_period_delays_accrual(services):
    policy = create_policy(services, {**MONTHLY_5_DAYS, "waiting_period_days": 90})
    assign(services, policy, "bob", start=date(2026, 7, 1))  # eligible from 2026-09-29

    posted = services.accruals.run_time_based_accruals(as_of=date(2026, 10, 15))

    assert len(posted) == 2  # September (prorated) and October
    assert posted[0].amount_minutes == round(5 * DAY_6H * 2 / 30)
    assert posted[1].amount_minutes == 5 * DAY_6H


def test_policy_change_applies_from_next_period(services):
    policy = create_policy(services, MONTHLY_5_DAYS)
    assign(services, policy, "alice")
    new_version = services.policies.update_settings(policy.id, {"rate_amount": 2}, date(2026, 2, 15), "admin_1")

    posted = services.accruals.run_time_based_accruals(as_of=date(2026, 3, 31))

    assert [t.amount_minutes // DAY_8H for t in posted] == [5, 5, 2]
    assert posted[-1].policy_version_id == new_version.id


def test_terminated_employee_stops_accruing(services, employees):
    employees.get_employee("alice").termination_date = date(2026, 2, 14)
    policy = create_policy(services, {**MONTHLY_5_DAYS, "accrue_at": "end_of_period"})
    assign(services, policy, "alice")

    posted = services.accruals.run_time_based_accruals(as_of=date(2026, 6, 1))

    assert len(posted) == 2
    assert posted[1].amount_minutes == round(5 * DAY_8H * 14 / 28)  # half of February


# ----- hours worked ----------------------------------------------------- #

HOURLY_SICK = {"accrual_method": "hours_worked", "rate_amount": 1, "rate_unit": "hours", "hours_worked_per_accrual": 30}


def payroll_event(run_id="run_1", hours=80.0, employee_id="alice"):
    return PayrollProcessedEvent(
        run_id=run_id,
        company_id="acme",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 15),
        entries=[PayrollEntry(employee_id, hours)],
    )


def test_hours_worked_accrual_from_payroll_event(services):
    policy = create_policy(services, HOURLY_SICK, name="Sick", time_off_type="sick")
    assign(services, policy, "alice")

    [txn] = services.accruals.handle_payroll_processed(payroll_event(hours=80))

    assert txn.amount_minutes == 160  # 80h / 30 = 2.67h
    assert txn.source_id == "run_1"


def test_same_payroll_event_twice_is_ignored(services):
    policy = create_policy(services, HOURLY_SICK, name="Sick", time_off_type="sick")
    assign(services, policy, "alice")

    services.accruals.handle_payroll_processed(payroll_event(hours=80))
    replay = services.accruals.handle_payroll_processed(payroll_event(hours=80))

    assert replay == []
    assert balance(services, "alice", policy) == 160


def test_reprocessed_payroll_posts_a_correction(services):
    policy = create_policy(services, HOURLY_SICK, name="Sick", time_off_type="sick")
    assign(services, policy, "alice")

    services.accruals.handle_payroll_processed(payroll_event(hours=80))
    [correction] = services.accruals.handle_payroll_processed(payroll_event(hours=90))

    assert correction.type == TransactionType.ADJUSTMENT
    assert correction.amount_minutes == 20
    assert balance(services, "alice", policy) == 180


def test_accrual_types_do_not_mix(services):
    time_based = create_policy(services, MONTHLY_5_DAYS)
    hourly = create_policy(services, HOURLY_SICK, name="Sick", time_off_type="sick")
    assign(services, time_based, "alice")
    assign(services, hourly, "alice")

    services.accruals.handle_payroll_processed(payroll_event(hours=80))
    services.accruals.run_time_based_accruals(as_of=date(2026, 1, 20))

    assert balance(services, "alice", time_based) == 5 * DAY_8H
    assert balance(services, "alice", hourly) == 160


# ----- pure helpers ----------------------------------------------------- #


def test_years_of_service():
    assert years_of_service(date(2022, 1, 10), date(2027, 1, 9)) == 4
    assert years_of_service(date(2022, 1, 10), date(2027, 1, 10)) == 5


def test_proration_factor_full_period():
    assert proration_factor(ProrationRule.PRORATE, date(2026, 1, 1), date(2026, 1, 31), date(2026, 1, 1), date(2026, 1, 31)) == 1.0
