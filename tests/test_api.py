from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import build_services

ADMIN = {"X-Actor-Id": "admin_1"}
ALICE = {"X-Actor-Id": "alice"}


@pytest.fixture
def client(employees, companies):
    return TestClient(create_app(build_services(employees=employees, companies=companies)))


@pytest.fixture
def policy_id(client):
    response = client.post(
        "/policies",
        headers=ADMIN,
        json={
            "company_id": "acme",
            "name": "W2 Vacation",
            "time_off_type": "vacation",
            "effective_from": "2026-01-01",
            "settings": {"accrual_method": "time_based", "frequency": "monthly", "rate_amount": 5},
        },
    )
    assert response.status_code == 201
    policy_id = response.json()["policy"]["id"]
    client.post(f"/policies/{policy_id}/assignments", headers=ADMIN, json={"employee_id": "alice", "start_date": "2026-01-01"})
    client.post("/jobs/run-accruals", json={"as_of": "2026-03-01"})
    return policy_id


def available_days(client, on_date="2026-03-01"):
    [summary] = client.get("/employees/alice/balances", params={"on_date": on_date}).json()
    return summary["available"]["days"]


def test_full_request_flow(client, policy_id):
    assert available_days(client) == 15

    response = client.post(
        "/time-off-requests",
        headers=ALICE,
        json={"employee_id": "alice", "policy_id": policy_id, "reason": "Vacation", "days": [{"day": "2026-03-09"}]},
    )
    assert response.status_code == 201
    request_id = response.json()["id"]
    assert available_days(client) == 14

    response = client.post(f"/time-off-requests/{request_id}/approve", headers=ADMIN, json={"note": "Have fun"})
    assert response.json()["status"] == "approved"

    detail = client.get(f"/time-off-requests/{request_id}").json()
    assert [e["to_status"] for e in detail["events"]] == ["pending", "approved"]

    txns = client.get(f"/employees/alice/policies/{policy_id}/transactions").json()
    assert [t["type"] for t in txns][-3:] == ["hold", "hold_release", "usage"]

    audit = client.get("/audit-log", params={"entity_id": request_id}).json()
    assert [a["action"] for a in audit] == ["created", "approved"]


def test_preview_shows_balance_after_request(client, policy_id):
    response = client.post(
        "/time-off-requests/preview",
        json={"employee_id": "alice", "policy_id": policy_id, "days": [{"day": "2026-03-06"}, {"day": "2026-03-07"}]},
    )
    body = response.json()
    assert body["skipped_days"] == ["2026-03-07"]  # Saturday
    assert body["available_after_minutes"] == 14 * 480


def test_policy_update_endpoint(client, policy_id):
    response = client.patch(
        f"/policies/{policy_id}", headers=ADMIN, json={"effective_from": "2026-04-01", "changes": {"rate_amount": 2}}
    )
    assert response.status_code == 200
    assert len(client.get(f"/policies/{policy_id}").json()["versions"]) == 2


def test_payroll_event_endpoint(client):
    policy = client.post(
        "/policies",
        headers=ADMIN,
        json={
            "company_id": "acme",
            "name": "Hourly sick",
            "time_off_type": "sick",
            "effective_from": "2026-01-01",
            "settings": {"accrual_method": "hours_worked", "rate_amount": 1, "rate_unit": "hours", "hours_worked_per_accrual": 30},
        },
    ).json()["policy"]
    client.post(f"/policies/{policy['id']}/assignments", headers=ADMIN, json={"employee_id": "alice", "start_date": "2026-01-01"})

    body = {
        "run_id": "run_42",
        "company_id": "acme",
        "period_start": "2026-01-01",
        "period_end": "2026-01-15",
        "entries": [{"employee_id": "alice", "hours_worked": 90}],
    }
    assert client.post("/events/payroll-processed", json=body).json()["posted"] == 1
    assert client.post("/events/payroll-processed", json=body).json()["posted"] == 0  # replay


def test_error_status_codes(client, policy_id):
    assert client.get("/employees/nobody/balances").status_code == 404

    too_much = [{"day": f"2026-03-{d:02d}"} for d in (9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23, 24, 25, 26, 27, 30)]
    response = client.post(
        "/time-off-requests",
        headers=ALICE,
        json={"employee_id": "alice", "policy_id": policy_id, "reason": "Long trip", "days": too_much},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "InsufficientBalanceError"

    request_id = client.post(
        "/time-off-requests",
        headers=ALICE,
        json={"employee_id": "alice", "policy_id": policy_id, "reason": "Trip", "days": [{"day": "2026-03-09"}]},
    ).json()["id"]
    client.post(f"/time-off-requests/{request_id}/reject", headers=ADMIN, json={})
    assert client.post(f"/time-off-requests/{request_id}/approve", headers=ADMIN, json={}).status_code == 409


def test_only_admins_can_approve_or_change_policies(client, policy_id):
    request_id = client.post(
        "/time-off-requests",
        headers=ALICE,
        json={"employee_id": "alice", "policy_id": policy_id, "reason": "Trip", "days": [{"day": "2026-03-09"}]},
    ).json()["id"]

    response = client.post(f"/time-off-requests/{request_id}/approve", headers={"X-Actor-Id": "bob"}, json={})
    assert response.status_code == 403
    assert client.patch(
        f"/policies/{policy_id}", headers=ALICE, json={"effective_from": "2026-04-01", "changes": {"rate_amount": 9}}
    ).status_code == 403
    assert client.post(f"/time-off-requests/{request_id}/approve", headers=ADMIN, json={}).status_code == 200


def test_policy_list_includes_current_version_and_assignments(client, policy_id):
    [policy] = client.get("/policies", params={"on_date": "2026-03-01"}).json()
    assert policy["current_version"]["rate_amount"] == 5
    assert [a["employee_name"] for a in policy["assignments"]] == ["Alice"]


def test_demo_app_serves_ui_and_seed_data():
    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok"}
    assert "<title>Time Off</title>" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200

    assert len(client.get("/policies").json()) == 4
    assert len(client.get("/employees").json()) == 5
    assert client.get("/companies/acme").json()["locations"] == ["CA", "NY"]
    assert len(client.get("/payroll/runs", params={"company_id": "acme"}).json()) > 0
    assert len(client.get("/employees/alice/balances").json()) == 2
    pending = client.get("/time-off-requests", params={"status": "pending"}).json()
    assert len(pending) == 2

    # Approving changes state; reset brings the seed data back
    client.post(f"/time-off-requests/{pending[0]['id']}/approve", headers={"X-Actor-Id": "dana"}, json={})
    assert len(client.get("/time-off-requests", params={"status": "pending"}).json()) == 1
    assert client.post("/demo/reset").status_code == 200
    assert len(client.get("/time-off-requests", params={"status": "pending"}).json()) == 2


def test_reset_is_disabled_outside_demo_mode(client):
    assert client.post("/demo/reset").status_code == 400


def test_payroll_endpoints(client):
    body = {"company_id": "acme", "period_start": "2026-01-01", "period_end": "2026-01-15"}
    lines = client.post("/payroll/preview", json=body).json()
    assert {line["employee_id"] for line in lines} == {"alice", "admin_1"}  # Bob and Carol weren't there / other company

    assert client.post("/payroll/runs", json=body, headers=ALICE).status_code == 403
    run = client.post("/payroll/runs", json=body, headers=ADMIN).json()
    assert run["id"] == "payroll-acme-2026-01-01"
    assert len(client.get("/events").json()) == 1
