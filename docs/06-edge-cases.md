# 6. Edge cases

## The three questions from the prompt

### What if a company updates the accrual settings of a policy?

**Answer: a new version, effective from a date, never an edit.**

- Accruals already posted stay as they are, and each one points to the
  version that produced it.
- The new rules apply from the first accrual period that starts on or after
  the effective date. A change on Feb 15 applies from March 1.
- The effective date must be after the current version's, so history can't be
  rewritten by accident.
- The audit log shows who changed what, with old and new values.

**What about retroactive changes?** ("We meant 2 days a month since January.")
I don't recalculate history automatically. It would silently change numbers
employees have already seen. The safer way is an admin posting an
`ADJUSTMENT` with a reason, so the employee sees exactly why their balance
changed. This isn't built yet; it's first on my next-steps list.

### Should the system allow employees to have a negative balance?

**Answer: it's a per-policy choice, off by default, and always with a limit.**

- `allow_negative = false`: a request is refused if it would take the
  available balance below zero.
- `allow_negative = true`: the admin must set how far, e.g. 8 hours. The
  database refuses "negative allowed" without a limit.

Why not a single rule for everyone? Companies really do differ. Borrowing
against future sick leave is common and kind: you don't choose when you get
sick. For vacation, many companies don't allow it.

Why always a limit? If someone goes 10 days negative and then leaves, the
company has paid for time that was never earned. Whether that can be taken
back from the final paycheck depends on the state, so it's a payroll and legal
decision. The system's job is to report the negative balance clearly at
termination, not to deduct anything automatically.

Pending requests count too: available = balance − pending, so two requests
submitted before either is approved can't both spend the same hours.

### What if an employee joins halfway through an accrual period?

**Answer: the policy chooses: prorate (default), give the full amount, or
start next period.** For 20 days a year, joining July 1 gives 10.08 days
with proration. The worked examples, including waiting periods and
terminations, are in [Accrual rules](05-accrual-rules.md#joining-mid-period-proration).

## Bonus challenges

| Challenge | Approach | Details |
|---|---|---|
| **Custom work hours** | Store minutes; a "day" is the employee's own workday | [Accrual rules](05-accrual-rules.md#one-day--your-workday) |
| **Holidays** | Skipped when counting a request; refunded if added later; optional location | [Core flows](04-core-flows.md#adding-a-holiday-after-requests-exist) |
| **Carryover and expiration** | Year-end job with a carryover cap; plus an optional max balance | [Accrual rules](05-accrual-rules.md#year-end-carryover-and-expiration) |
| **Tenure-based rates** | Tenure tiers on the policy version, chosen at each accrual | [Accrual rules](05-accrual-rules.md#tenure-tiers) |

**An open question on custom hours:** if someone changes from 8h to 6h days,
their balance in **hours** doesn't change, so their balance in **days** goes
up (40h is 5 days at 8h, but 6.67 days at 6h). I think that's fair, since they
earned those hours, but I'd confirm it with the product team.

## Other edge cases I handled

| Situation | What happens |
|---|---|
| Two requests for the same day | Second one refused, even on a different policy |
| Requesting days before joining the policy | Refused: must be on the policy for every day |
| Only weekends or holidays selected | Refused with a clear message |
| Partial day longer than a workday | Refused ("choose full day instead") |
| Employee approves their own request | Refused (an admin who is also an employee still needs someone else) |
| Approving a rejected or cancelled request | Refused (409) |
| Employee cancels time off that already started | Refused; an admin can revoke it with a reason |
| Admin revokes without a reason | Refused |
| On two vacation policies at once | Refused; end the first assignment, then add the second |
| Employee from another company | Refused |
| Accrual job runs twice, or misses a week | Nothing doubles; missed periods are caught up |
| Same payroll event twice | Ignored |
| Payroll corrected later | Only the difference is posted |
| Balance already at the max | Accrual posts 0 with a note saying why |
| Employee terminated mid-month | Last month prorated, no accruals after |
| Year-end run before Dec 31 is over | Refused |
| Year-end run twice | Second run does nothing |
| January time off approved in December | Doesn't reduce what expires for the old year |
| Holiday that only applies in California | Doesn't affect people in New York |

## Payroll and the existing services

| Situation | What happens |
|---|---|
| Time off approved **after** payroll already ran for that period | The Payroll page marks that period **"Needs re-run"**. Re-running picks up the change, and Time Off posts only the difference |
| A holiday (not worked, but paid) | Taken out of hours worked (so no sick leave for it) but included in paid hours |
| Pending (not approved) time off | Not taken out of hours worked; only approved time off is |
| Unpaid time off (contractor policy) | Not paid and not worked |
| Holiday for a contractor | Not worked and not paid (employees get holiday pay) |
| Someone hired mid-period | Only their days from the hire date are scheduled |
| Running payroll for a period that hasn't ended | Refused |
| Admin changes hours before running (overtime) | Used as the hours worked. Hourly pay changes; salaried pay doesn't (only sick leave does). No overtime rate in the demo |
| Holiday for a location the company doesn't have | Refused, the Company Service's locations are checked |
| Policy for a company that doesn't exist | Refused (404) |

## Things I'd handle next

- **Payout when someone leaves.** Some states (e.g. California) require paying
  out unused vacation. The ledger already gives the exact balance at
  termination; next is sending it to payroll.
- **Front-loaded policies and early leavers.** Someone who gets 20 days on
  Jan 1 and leaves in March has more than they earned. Clawing it back should
  be a policy setting.
- **Requests spanning a policy switch.** Today a request must be fully inside
  one assignment. Splitting it across two would be friendlier.
- **Removing a holiday** after people booked around it (flag for an admin).
- **Time zones** for teams across the country, to decide what "today" means
  for each accrual.
