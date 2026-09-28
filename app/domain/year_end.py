"""Year-end carryover and expiration.

At the end of the policy year, anything above `carryover_cap_hours` expires:
  * carryover_cap_hours = None -> everything carries over (nothing to do)
  * carryover_cap_hours = 40   -> keep up to 40h, the rest expires
  * carryover_cap_hours = 0    -> "use it or lose it"

Expiration is just another ledger entry (EXPIRATION), so it is auditable and
can be reversed with an ADJUSTMENT if a company made a mistake.
"""

from __future__ import annotations

from datetime import date

from typing import Optional

from app.domain.audit import record_audit
from app.domain.errors import ValidationError
from app.domain.formatting import fmt_hours
from app.domain.ledger import Ledger
from app.domain.models import BalanceTransaction, TransactionType
from app.domain.policies import PolicyService
from app.storage.memory import InMemoryStore


class YearEndService:
    def __init__(self, store: InMemoryStore, ledger: Ledger, policies: PolicyService) -> None:
        self.store = store
        self.ledger = ledger
        self.policies = policies

    def run(
        self, policy_id: str, year: int, actor_id: str = "system", run_date: Optional[date] = None
    ) -> list[BalanceTransaction]:
        """Normally called by the scheduler on Jan 1. `run_date` is "today"."""
        year_end = date(year, 12, 31)
        run_date = run_date or date.today()
        if run_date <= year_end:
            raise ValidationError(f"{year} isn't over yet. Year-end can run from Jan 1, {year + 1}.")
        version = self.policies.get_version(policy_id, year_end)
        if version is None or version.is_unlimited or version.carryover_cap_hours is None:
            return []

        cap = round(version.carryover_cap_hours * 60)
        employee_ids = sorted(
            {a.employee_id for a in self.store.assignments.values() if a.policy_id == policy_id and a.covers(year_end)}
        )

        posted = []
        for employee_id in employee_ids:
            key = f"expire:{employee_id}:{policy_id}:{year}"
            if self.ledger.has_key(key):
                continue
            # Only count what happened up to Dec 31. Approved time off in
            # January comes out of next year's balance.
            balance = self.ledger.settled_balance(employee_id, policy_id, as_of=year_end)
            excess = balance - cap
            if excess <= 0:
                continue
            posted.append(
                self.ledger.post(
                    employee_id=employee_id,
                    policy_id=policy_id,
                    amount_minutes=-excess,
                    txn_type=TransactionType.EXPIRATION,
                    effective_date=year_end,
                    idempotency_key=key,
                    policy_version_id=version.id,
                    source_type="year_end",
                    source_id=str(year),
                    created_by=actor_id,
                    note=f"{year} ended with {fmt_hours(balance)}. Only {version.carryover_cap_hours:g}h carries over.",
                    metadata={"balance_before": balance, "carryover_cap_minutes": cap},
                )
            )

        record_audit(
            self.store,
            entity_type="policy",
            entity_id=policy_id,
            action="year_end_processed",
            actor_id=actor_id,
            after={"year": year, "expirations": len(posted)},
        )
        return posted
