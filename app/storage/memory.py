"""A tiny in-memory stand-in for Postgres.

I kept persistence in memory so the take-home stays focused on the business
logic. schema.sql shows the real tables, and every dict below maps to one of
them. Swapping this for SQLAlchemy/Postgres would not change the domain code.
"""

from __future__ import annotations

from app.domain.models import (
    AuditLogEntry,
    BalanceTransaction,
    Holiday,
    Policy,
    PolicyAssignment,
    PolicyVersion,
    RequestEvent,
    TimeOffRequest,
)


class InMemoryStore:
    def __init__(self) -> None:
        self.policies: dict[str, Policy] = {}
        self.policy_versions: dict[str, list[PolicyVersion]] = {}  # policy_id -> versions (ordered)
        self.assignments: dict[str, PolicyAssignment] = {}
        self.transactions: list[BalanceTransaction] = []
        self.transactions_by_key: dict[str, BalanceTransaction] = {}
        self.requests: dict[str, TimeOffRequest] = {}
        self.request_events: list[RequestEvent] = []
        self.holidays: dict[str, Holiday] = {}
        self.audit_log: list[AuditLogEntry] = []

    def add_transaction(self, txn: BalanceTransaction) -> None:
        # Mirrors the UNIQUE(idempotency_key) constraint in schema.sql.
        if txn.idempotency_key in self.transactions_by_key:
            raise ValueError(f"Duplicate idempotency key: {txn.idempotency_key}")
        self.transactions.append(txn)
        self.transactions_by_key[txn.idempotency_key] = txn
