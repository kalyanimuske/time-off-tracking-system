# Glossary

Every technical word used in these docs, explained in plain language. If a
word in the docs is unclear, it should be here.

## Time off words

**Policy.** The rules for one kind of time off at a company, e.g.
"Full-Time Vacation: 1.25 days a month". A company can have many policies.

**Assignment.** Putting an employee on a policy, from a start date (and
maybe until an end date). This is how groups like "full-time" or
"contractors" are made: they're just different policies with different
people assigned.

**Unlimited policy.** No balance to track. Time off is still requested and
approved, so the company knows who's out.

**Accrual / accruing.** Earning time off bit by bit. "Accrues 1.25 days a
month" means 1.25 days are added every month.

**Time-based accrual.** Earned by the calendar: every month or every year.

**Hours-worked accrual.** Earned by working: e.g. 1 hour for every 30 hours
worked. The hours come from payroll.

**Balance.** How much time off someone has right now.

**Pending.** Hours reserved by requests that haven't been approved yet.

**Available.** Balance minus pending: what someone can still request.

**Hold (reservation).** When someone submits a request, the hours are
reserved right away so they can't be requested twice. If it's approved the
hold becomes real usage; if it's rejected or cancelled the hold is removed.

**Proration.** Giving part of an amount. Joining halfway through a year on a
"20 days a year" policy gives about 10 days.

**Waiting period.** New hires start earning only after some days, e.g. 90.

**Tenure / tenure tiers.** How long someone has worked at the company. Tiers
give more time off with more years, e.g. 1.5 days a month after 5 years.

**Carryover.** Unused time moving into the next year. A **carryover cap**
limits how much moves over; the rest **expires** on Dec 31.

**Max balance.** A limit where accrual stops until some time is used.

**Negative balance.** Using more time than you have, i.e. borrowing from the
future. Allowed only if the policy says so, and only up to a limit.

**Revoke.** An admin cancelling time off that was already approved. Needs a
reason.

## Payroll words

**Pay period.** The stretch of days one paycheck covers. Here: the 1st to
the 15th, and the 16th to the end of the month.

**Payroll run.** Calculating everyone's hours and pay for one pay period.

**Hours worked.** Scheduled hours minus holidays minus approved time off.
Sick leave is earned on this.

**Paid hours.** Hours worked plus paid time off plus paid holidays. Pay is
based on this. For salaried people it's always their normal schedule, because
their pay doesn't change with the hours they work.

**Salaried / hourly.** Salaried people get a fixed amount per year, split
across pay periods. Hourly people are paid per hour.

**Contractor.** Not an employee: paid only for hours worked, no benefits, no
tax withheld by the company, no holiday pay.

**Gross / deductions / net.** Gross is pay before anything is taken out.
Deductions are benefits and taxes. Net is what reaches the bank account.

**Correction (re-run).** Running payroll again for the same period because
something changed. Time off adds or removes only the difference.

## Design and engineering words

**Ledger.** A list of every change, like a bank statement. A balance is the
sum of the list. Nothing in it is ever edited or deleted; mistakes are fixed
by adding a new line.

**Transaction (ledger row).** One line in the ledger, e.g. "+12h earned for
September".

**Version (policy version).** A saved copy of a policy's rules from a certain
date. Changing a policy makes a new version; old ones stay so history can be
explained.

**Effective date.** The date something applies from, or the date a ledger
row applies to (e.g. the day of the time off).

**Idempotent / idempotency.** Doing something twice has the same result as
doing it once. Here it means a job that runs twice, or an event that arrives
twice, doesn't add time off twice.

**Idempotency key.** A unique name for each ledger row, like
`accrual:alice:vacation:monthly:2026-09-01`. The database refuses a second
row with the same name. That's what makes things idempotent.

**Audit log.** A record of who changed what, when, and what it looked like
before and after.

**State machine.** A list of allowed status changes, e.g. pending can become
approved, but rejected can never become approved.

**Service.** A program that does one job and others can ask it things.
Here: the Time Off Service, plus the (simulated) Employee, Company and Payroll
services.

**API.** The set of URLs a service answers, e.g. `POST /time-off-requests`
to create a request. **Endpoint** is one of those URLs.

**REST / JSON.** REST is the common style for web APIs (URLs + GET/POST).
JSON is the text format the data is sent in.

**Event.** A message that says "something happened", e.g.
`on_payroll_processed`. **Publishing** sends it; **subscribing** means "tell
me when this happens". The sender doesn't need to know who's listening.

**Event bus / queue.** The thing that delivers events. In the demo it's a few
lines of code. In production it would be a queue such as Amazon SQS, which
also retries if the listener is down.

**Scheduler / cron.** Runs a job at set times, e.g. accruals every night.

**Database transaction.** Several changes saved together: all of them, or
none. Approving a request (update the request, add ledger rows, add an audit
entry) is one transaction.

**Row lock (`SELECT ... FOR UPDATE`).** Makes a second request wait while the
first one is changing the same balance, so two requests can't both spend the
same hours.

**Index.** Like a book index for the database: finds rows fast without
reading the whole table.

**Unique constraint.** A database rule that a value (like the idempotency
key) can't appear twice.

**In-memory storage.** Data kept in the program's memory instead of a
database. Fast and simple, but it's gone when the app restarts. Used here only
so the demo runs with one command.

**Domain layer.** The part of the code with the business rules (how time off
works), kept separate from the website and the database.

**Monolith / microservices.** A monolith is one program doing everything; microservices
are many small programs. This design is one service (a "modular monolith")
with clear internal parts.
