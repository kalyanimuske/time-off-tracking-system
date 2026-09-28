# 1. Requirements and scope

Before designing anything I wrote down what the system has to do, what I'm
assuming, and what I'm leaving out. The table at the end maps every
requirement to where it's handled, so nothing gets lost.

If a word is unfamiliar (accrual, ledger, idempotent...), the
[glossary](glossary.md) explains every term in plain language. The
[code tour](code-tour.md) explains the folders.

## How I read the prompt

The prompt has three parts, and it helps to keep them apart:

| Part | What it is | What I did |
|---|---|---|
| **The main task** ("Your task") | Design a system for policies, balances, time-based accrual, requests, admin approvals and balance updates | Designed and built it. This is most of the work and most of these docs |
| **Existing services** | Employee, Company and Payroll services, plus the `on_payroll_processed` event. The prompt says these already exist | Designed how the Time Off Service uses them, and built small **simulated** versions so the demo works end to end |
| **Bonus challenges** | Custom hours, holidays, carryover, tenure | Designed and built all four |

I kept the simulated services deliberately simple (for example, pay uses a flat
tax rate), because real payroll isn't what's being asked for. What matters is
the **boundary**: what Time Off reads from them, and what they need from Time
Off.

## What the system has to do

**Company admins**
- Create time off policies. A policy is either **unlimited** or
  **accrual-based**.
- Accrual can be **per time** ("20 days per year", "5 days per month") or
  **per hours worked** ("1 hour per 30 hours worked").
- Offer different **types** of time off (vacation, sick, parental...) that
  mostly follow the same rules.
- Put **any group** of employees on a policy (full-time vs part-time, employee
  vs contractor, per state...).
- **Change a policy** later without breaking existing balances.
- **Approve or reject** time off requests.

**Employees**
- See their current **balance** for each policy.
- **Request time off** for specific days and times, knowing the balance
  before they submit.

**The system**
- Increase balances when time is **accrued**, decrease them when time off is
  **taken**.
- Record changes to policies and requests (**auditable**).

**Bonus challenges from the prompt**
1. Custom work hours (6-hour days instead of 8)
2. Company holidays that don't count against time off
3. Carryover caps and year-end expiration
4. Tenure-based rates

## Non-functional requirements

These aren't in the prompt word for word, but they follow from "fast,
reliable and bulletproof" and from the fact that balances are basically money.

| Requirement | What it means here |
|---|---|
| **Correct** | A balance is never wrong because of a retry, a double click, or a job running twice. |
| **Explainable** | For any balance, we can show every change that produced it and why. |
| **Auditable** | We know who changed a policy or a request, when, and what the values were before and after. |
| **Simple to operate** | One service, one database. No infrastructure that a small team can't run. |
| **Fast enough** | Reading a balance is a single indexed query. Nothing here needs heavy scaling. |

## The existing services

**Employee Service.** Returns employees with their pay rates and work
configuration. Time Off reads: hire date, hours per day, work days, location,
termination date. (Pay rates are used by payroll, not by time off.)

**Company Service.** Returns company information. Time Off reads: whether the
company exists, its locations (for location-specific holidays), and its pay
schedule.

**Payroll Service.** Adds up each employee's hours worked for a pay period and
calculates pay, benefits and taxes. When it's done it publishes
`on_payroll_processed` with every employee and their hours worked, and Time
Off is subscribed to it.

One thing the prompt doesn't say, but a real payroll needs: **payroll has to
know about approved time off.** Paid vacation has to be paid, and a day off
isn't a day worked. So the dependency goes both ways:

- Payroll **asks** Time Off for approved time off in the period
- Time Off **listens** to `on_payroll_processed` to credit hours-worked accruals

## Assumptions

- A standard workday is Monday to Friday, 9 to 5 (8 hours). The **Employee
  Service** can override this per person (for example 6-hour days).
- We only read employee and company data; we never own it.
- Payroll runs twice a month (1st to 15th, 16th to end of month).
- Hours worked = scheduled hours − company holidays − approved time off. An
  admin can override hours (for example overtime) before running payroll.
- Policy years follow the calendar year (Jan 1 to Dec 31).
- Dates are the company's local dates. Time zones are out of scope.

## Out of scope (on purpose)

- Real login. The demo uses a "Viewing as" switcher, but the API still
  enforces who can do what.
- Manager approval chains (one admin approval is enough here).
- Real payroll: taxes and benefits in the simulated Payroll Service are flat
  demo numbers.
- Payroll payouts of unused time when someone leaves.
- Email or Slack notifications.

## Requirement to design mapping

| Requirement | How it's handled | Where |
|---|---|---|
| Unlimited and accrual policies | `accrual_method` on the policy version | [Data model](03-data-model.md), [Accrual rules](05-accrual-rules.md) |
| Per-time accrual | Daily scheduled job, one ledger entry per period | [Accrual rules](05-accrual-rules.md#time-based-accrual) |
| Per-hours-worked accrual | Listens to `on_payroll_processed` | [Accrual rules](05-accrual-rules.md#hours-worked-accrual) |
| Many types of time off | `time_off_type` on the policy, same engine for all | [Data model](03-data-model.md) |
| Any grouping of employees | Dated policy assignments | [Data model](03-data-model.md#policy_assignments) |
| Balances go up and down | Append-only ledger | [Data model](03-data-model.md#the-ledger-balance_transactions) |
| Request, approve, reject | Request state machine with holds | [Core flows](04-core-flows.md) |
| Change policy settings | Policy versions with an effective date | [Core flows](04-core-flows.md#changing-a-policy), [Edge cases](06-edge-cases.md) |
| Negative balances | Per-policy setting with a required limit | [Edge cases](06-edge-cases.md) |
| Joining mid-period | Proration rule on the policy | [Accrual rules](05-accrual-rules.md#joining-mid-period-proration) |
| Custom work hours | Store minutes, convert with each person's day length | [Accrual rules](05-accrual-rules.md#one-day--your-workday) |
| Holidays | Skipped when counting days, refunded if added later | [Core flows](04-core-flows.md#adding-a-holiday-after-requests-exist) |
| Carryover and expiration | Year-end job | [Accrual rules](05-accrual-rules.md#year-end-carryover-and-expiration) |
| Tenure-based rates | Tenure tiers on the policy version | [Accrual rules](05-accrual-rules.md#tenure-tiers) |
| Auditable | Ledger + request events + audit log | [Reliability and audit](07-reliability-and-audit.md) |
| Employee Service data | Read only: hire date, schedule, location, pay | [Architecture](02-architecture.md#the-existing-services) |
| Company Service data | Read only: company, locations, pay schedule | [Architecture](02-architecture.md#the-existing-services) |
| Subscribe to `on_payroll_processed` | Event bus; the accrual engine is the subscriber | [Architecture](02-architecture.md#payroll-and-time-off-depend-on-each-other) |
| Payroll needs approved time off | Time Off exposes approved time off per period | [Architecture](02-architecture.md#payroll-and-time-off-depend-on-each-other) |
