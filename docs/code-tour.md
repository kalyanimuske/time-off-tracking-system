# Code tour

A guide to the code for someone opening it for the first time. It explains
what each folder does, then follows one button click all the way through.

## The folders

```
app/
├── main.py          The front door. Every URL the app answers is here.
├── schemas.py       What each request body must look like (checked automatically).
├── services.py      Creates all the parts and connects them. Read this to see how things fit.
├── demo_data.py     Builds the demo company when the app starts.
│
├── domain/          THE TIME OFF SERVICE. All the rules live here.
│   ├── models.py            The "nouns": Policy, Employee, TimeOffRequest, BalanceTransaction...
│   ├── ledger.py            Adds ledger rows and calculates balances. The only place balances change.
│   ├── policies.py          Create and change policies (versions), assign employees.
│   ├── accrual.py           Earning time off: monthly/yearly job, and payroll-based.
│   ├── time_off_requests.py Request, preview, approve, reject, cancel.
│   ├── holidays.py          Add holidays; refund days already requested.
│   ├── year_end.py          Carryover and expiration on Jan 1.
│   ├── balances.py          The numbers shown on the balance cards.
│   ├── work_calendar.py     Is this day a working day?
│   ├── audit.py             Writes audit log entries.
│   ├── formatting.py        Makes dates and hours readable in notes.
│   └── errors.py            The kinds of errors (not found, not allowed...).
│
├── external/        SIMULATED EXISTING SERVICES (given by the prompt).
│   ├── employee_service.py  Employees, pay, schedules.
│   ├── company_service.py   The company.
│   ├── payroll_service.py   Hours worked, pay, publishes the event.
│   └── events.py            The on_payroll_processed event and a tiny event bus.
│
├── storage/memory.py  Where data is kept (in memory). Same shape as schema.sql.
└── static/            The website: index.html, styles.css, app.js (plain JavaScript).

tests/               83 tests, one file per area.
docs/                These documents.
schema.sql           The real database design (PostgreSQL).
```

The most important idea in the layout: **`domain/` doesn't know about the
website or the database.** `main.py` calls into it, and it saves through
`storage/`. That's why the tests can check every rule quickly without starting
a server.

## Follow one click: "Submit request"

Alice asks for Dec 23 to Dec 28 off.

1. **Browser** (`static/app.js`, `openRequestModal`). She fills in the form.
   While she types, the page calls `POST /time-off-requests/preview` to show
   "Uses 24h, Christmas and the weekend not counted".
2. She clicks **Submit request**. The page sends `POST /time-off-requests`
   with her id, the policy id, the reason and the list of days.
3. **API** (`main.py`, `create_request`). FastAPI checks the body matches
   `CreateRequestIn` in `schemas.py`, then calls the domain.
4. **Domain** (`time_off_requests.py`, `create_request`):
   - `_count_days`: skips weekends and holidays, turns each day into minutes
     (480 for Alice's 8-hour day)
   - `_check_assignment`: is she on this policy for every day?
   - `_check_no_overlap`: does she already have time off on these days?
   - `_check_balance`: is her available balance enough?
   - saves the request, then asks the ledger to add a **HOLD** row
5. **Ledger** (`ledger.py`, `post`). Checks the idempotency key
   (`hold:req_...`) isn't used yet, then saves the row through `storage/`.
6. The request event and an audit entry are recorded, and the API returns
   the request as JSON. The page shows "Request submitted".

**When Dana approves**, `approve` in the same file releases the hold and adds
a **USAGE** row. That's the whole balance change: two ledger rows.

## Follow one click: "Run payroll"

1. **Browser** (`app.js`, `viewPayroll`): `POST /payroll/runs`.
2. **API** (`main.py`, `run_payroll`): checks Dana is an admin.
3. **Payroll Service** (`external/payroll_service.py`, `run`):
   - `calculate`: for each employee, scheduled hours, minus holidays, minus
     approved time off (it asks `approved_time_off` in `time_off_requests.py`),
     then pay
   - saves the run, then publishes `on_payroll_processed` on the event bus
4. **Event bus** (`external/events.py`, `publish`): calls every subscriber.
   The subscriber was connected in `services.py`.
5. **Accrual engine** (`domain/accrual.py`, `handle_payroll_processed`): for
   each hours-worked policy, `hours / 30` becomes a ledger row. If this run
   was seen before, only the difference is added.

## Where to find things

| I want to understand... | Look at |
|---|---|
| How a balance is calculated | `ledger.py`: `settled_balance`, `available_balance` |
| The monthly accrual math | `accrual.py`: `_accrue_period`, `proration_factor`, `rate_for` |
| Why nothing is counted twice | `ledger.py`: `post` (idempotency key), and the keys in `accrual.py` |
| Which status changes are allowed | `time_off_requests.py`: `ALLOWED_TRANSITIONS` |
| How a policy change works | `policies.py`: `update_settings` |
| How payroll hours are worked out | `payroll_service.py`: `_line` |
| Who is allowed to do what | `main.py`: `require_admin` |
| The real database tables | `schema.sql` |
| Examples of every rule | `tests/`: each test name is a sentence, e.g. `test_joining_halfway_through_the_year_is_prorated` |

## A suggested order to read the code

1. `domain/models.py`: the nouns (skim)
2. `domain/ledger.py`: short and the most important
3. `domain/time_off_requests.py`: create and approve
4. `domain/accrual.py`: the math
5. `services.py`: how everything connects
6. `main.py`: the URLs
7. `external/payroll_service.py`: the payroll side
8. `tests/test_requests.py` and `tests/test_accrual.py`: see every rule in action

You can skip `static/app.js` (the website) and `styles.css` unless you're
asked about the UI. They only call the API.
