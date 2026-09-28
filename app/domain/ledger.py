"""The balance ledger.

Balances are never stored as a mutable number. Every change (accrual, request,
expiration, correction) is an append-only transaction, and a balance is the sum
of the transactions. This gives us:

  * a full audit trail for free ("why do I have 9 days?")
  * safe corrections: we add a reversing entry instead of editing history
  * idempotency: every transaction has a unique key, so re-running a job or
    receiving the same payroll event twice never double-counts

Three numbers are exposed:
  settled   = accruals - approved usage +/- adjustments (ignores pending holds)
  pending   = minutes reserved by pending requests
  available = settled - pending   (what the employee can still request)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

from app.domain.models import (
    HOLD_TYPES,
    USAGE_TYPES,
    BalanceTransaction,
    TransactionType,
    new_id,
)
from app.storage.memory import InMemoryStore


class Ledger:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    # ----- writing ------------------------------------------------------- #

    def post(
        self,
        *,
        employee_id: str,
        policy_id: str,
        amount_minutes: int,
        txn_type: TransactionType,
        effective_date: date,
        idempotency_key: str,
        created_by: str = "system",
        policy_version_id: Optional[str] = None,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        note: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> BalanceTransaction:
        """Append a transaction. If the key was already used, return the
        existing transaction instead of creating a duplicate."""
        existing = self.store.transactions_by_key.get(idempotency_key)
        if existing is not None:
            return existing

        txn = BalanceTransaction(
            id=new_id("txn"),
            employee_id=employee_id,
            policy_id=policy_id,
            amount_minutes=int(amount_minutes),
            type=txn_type,
            effective_date=effective_date,
            idempotency_key=idempotency_key,
            policy_version_id=policy_version_id,
            source_type=source_type,
            source_id=source_id,
            note=note,
            metadata=metadata or {},
            created_by=created_by,
        )
        self.store.add_transaction(txn)
        return txn

    def has_key(self, idempotency_key: str) -> bool:
        return idempotency_key in self.store.transactions_by_key

    # ----- reading ------------------------------------------------------- #

    def transactions(
        self, employee_id: str, policy_id: str, as_of: Optional[date] = None
    ) -> list[BalanceTransaction]:
        return [
            t
            for t in self.store.transactions
            if t.employee_id == employee_id
            and t.policy_id == policy_id
            and (as_of is None or t.effective_date <= as_of)
        ]

    def settled_balance(self, employee_id: str, policy_id: str, as_of: Optional[date] = None) -> int:
        return sum(
            t.amount_minutes
            for t in self.transactions(employee_id, policy_id, as_of)
            if t.type not in HOLD_TYPES
        )

    def pending_minutes(self, employee_id: str, policy_id: str) -> int:
        return -sum(
            t.amount_minutes for t in self.transactions(employee_id, policy_id) if t.type in HOLD_TYPES
        )

    def available_balance(self, employee_id: str, policy_id: str) -> int:
        return self.settled_balance(employee_id, policy_id) - self.pending_minutes(employee_id, policy_id)

    def used_minutes(self, employee_id: str, policy_id: str) -> int:
        return -sum(
            t.amount_minutes for t in self.transactions(employee_id, policy_id) if t.type in USAGE_TYPES
        )

    def transactions_for_source(
        self,
        source_type: str,
        source_id: str,
        employee_id: Optional[str] = None,
        policy_id: Optional[str] = None,
    ) -> list[BalanceTransaction]:
        return [
            t
            for t in self.store.transactions
            if t.source_type == source_type
            and t.source_id == source_id
            and (employee_id is None or t.employee_id == employee_id)
            and (policy_id is None or t.policy_id == policy_id)
        ]

    def net_for_source(self, source_id: str, types: Iterable[TransactionType]) -> int:
        wanted = set(types)
        return sum(
            t.amount_minutes for t in self.store.transactions if t.source_id == source_id and t.type in wanted
        )
