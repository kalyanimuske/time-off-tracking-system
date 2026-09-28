# 7. Reliability and audit

## Idempotency: doing something twice has no extra effect

Things **will** happen twice in production: a cron job retries, a message
queue delivers a message again, a user double-clicks. The fix is the same
everywhere: every ledger entry has a key built from what it represents, and
the database has a `UNIQUE` constraint on it.

| Operation | Key | If it happens again |
|---|---|---|
| Monthly accrual | `accrual:alice:vacation:monthly:2026-09-01` | Skipped |
| Payroll accrual | `payroll:run_42:alice:sick` | Skipped (or only the difference, if hours changed) |
| Request hold | `hold:req_123` | Skipped |
| Approval usage | `usage:req_123` | Skipped |
| Year-end expiration | `expire:alice:vacation:2026` | Skipped |
| Holiday refund | `holiday:hol_9:req_123` | Skipped |

Even if two copies of the same job run at the same time, the database
accepts only one insert per key.

## What happens when things go wrong

| Scenario | What happens |
|---|---|
| Accrual job crashes halfway | Whatever was posted stays; the next run posts the rest |
| The job doesn't run for a week | The next run catches up every missed period |
| Payroll sends the same event twice | Second one is ignored |
| Our service is down when payroll finishes | In production the event waits in the queue and is retried. In the demo the event bus is in-process, so this can't happen |
| Payroll re-runs a period | Same run id; only the difference is posted |
| Payroll corrects someone's hours | An `ADJUSTMENT` for the difference; the original stays visible |
| Two requests submitted at the same moment | Serialized by a row lock (below); the second sees the first's hold |
| Admin approves while the employee cancels | State machine: whoever is second gets "can't move a cancelled request to approved" |
| Employee Service is down | Requests fail with a clear error and can be retried; nothing half-done is saved |
| A policy change was a mistake | Create another version, or post adjustments. Nothing is edited in place |

## Concurrency

The demo runs in one process, so there's no race. In Postgres, creating or
approving a request would look like this:

```sql
BEGIN;
-- lock this employee's balance for this policy
SELECT * FROM balance_snapshots
WHERE employee_id = $1 AND policy_id = $2
FOR UPDATE;

-- check available balance, insert the request, insert the HOLD row,
-- update the snapshot, insert the audit entry
COMMIT;
```

Two requests for the **same** balance wait for each other. Requests from
different employees don't block each other at all, so this doesn't slow
anything down in practice.

## Audit: three layers

The prompt asks "are changes to policies and requests tracked and logged?"
Everything is recorded, in three places that answer different questions:

| Layer | Answers | Example |
|---|---|---|
| **Ledger** (`balance_transactions`) | Why is my balance what it is? | "Sep 1: +12h. September 2026: 1.5 days x 8h = 12h (rate after 5 years)" |
| **Request events** | What happened to this request? | "Pending → Approved by Dana, note: Have fun!" |
| **Audit log** | Who changed what, and what was it before? | "Dana changed Sick Leave: Negative balance allowed: ~~Yes~~ → No" |

Things you can answer from the data:
- Why does Alice have 80h? (sum of her ledger, each row with a note)
- Which rules produced her March accrual? (`policy_version_id`)
- Who approved Bob's time off and when? (request events)
- What did the Sick Leave policy look like on Feb 1? (the version in effect then)
- Who revoked this request and why? (request + audit log)

Nothing in these tables is ever updated or deleted. In production I'd also
restrict the database user so the app can only `INSERT` into the ledger and
audit log, and keep them for as long as payroll records must be kept.

## Testing

83 automated tests cover the business rules and the API: accrual math,
proration, tenure, caps, payroll hours and pay, the payroll event, replays and
corrections, holds, overlaps, holidays, cancellations, permissions, year-end.
They run in under a second:

```bash
pytest
```
