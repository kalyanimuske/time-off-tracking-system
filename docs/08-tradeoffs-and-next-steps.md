# 8. Tradeoffs and next steps

## Decisions, and what I gave up

| Decision | Alternative | Why I chose it | Cost |
|---|---|---|---|
| **Ledger** of changes | A `balance` column | Explainable, auditable, safe corrections, idempotent | More rows; reads need a sum (fixed by a snapshot cache) |
| **Policy versions** | Edit the policy in place | History never changes; every accrual traceable | Reads must find "the version in effect on date X" |
| **Holds** for pending requests | Only check at approval | No double-booking; employee sees the real available balance | Three ledger rows per approved request instead of one |
| **Minutes** as integers | Days or decimal hours | Exact, works for any schedule | Converting at the edges |
| **Catch-up accrual job** | "Accrue for today only" | Safe to re-run, heals itself after outages | Checks more periods each run (fine at this size) |
| **One service** | Microservices | One transaction for request + balance + audit | Everything deploys together |
| **Plain JS UI** | React | Runs with one command, nothing to build | Harder to grow; I'd use React + TypeScript for the real product |
| **In-memory storage** (demo only) | Postgres now | Reviewer can run it anywhere in seconds | Data resets on restart; `schema.sql` shows the real design |
| **Simulated existing services** | Leave them out, or build them fully | The demo works end to end and the integration is visible | Pay math is deliberately fake (flat rates) |
| **In-process event bus** (demo only) | A real queue (SQS, Kafka) | Same publish/subscribe idea, zero setup | No retries if a subscriber fails; production needs the queue |
| **Payroll asks Time Off for approved time off** | Time Off pushes changes to payroll | Payroll gets a consistent picture at the moment it runs | Time off approved after a run needs a re-run (a correction) |

## Will it scale?

A rough estimate for a large case:

- 1,000 companies x 200 employees x 2 policies = 400,000 balances
- About 24 payroll + 12 accrual + ~10 request rows each per year
  = about **18 million ledger rows a year**

That's comfortable for one Postgres database with the indexes in
[Data model](03-data-model.md#indexes). A balance read touches one employee's
rows, maybe a few hundred. If it ever became a problem, the options, in
order, would be: rely on the snapshot table, archive old years, partition the
ledger by year.

The accrual job processes one assignment at a time and every write is
idempotent, so it can be split by company and run in parallel.

## What I'd do next

1. **Postgres** with SQLAlchemy and Alembic migrations, plus the row lock
   from [Reliability](07-reliability-and-audit.md#concurrency).
2. **Manual adjustments** by admins with a required reason (also the safe way
   to apply retroactive policy fixes).
3. **Balance projection**: "how much will I have on my trip in March?", so
   people can plan ahead.
4. **Notifications** to the approver and the employee.
5. **Real authentication** and roles (employee, manager, admin).
6. **State templates** such as CA or NY sick leave presets.
7. **Payout on termination** sent to payroll.

## Questions I'd ask the team

- Should balances be shown in days or hours by default? Hours are exact; people
  think in days.
- How often are policy changes meant to be retroactive?
- What do you do today with a negative balance when someone leaves?
- Is one admin approval enough for most customers, or do they need managers?
