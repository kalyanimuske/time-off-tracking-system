/* Time Off UI: plain JavaScript, no build step, served by FastAPI.
 *
 * Structure:
 *   helpers      - fetch wrapper, formatting, modal, toast
 *   employee     - "My time off" and "Balance history" views + request modal
 *   admin        - approvals, policies, holidays, accruals & payroll, audit log
 *   boot         - loads employees, wires the "Viewing as" switcher, routes views
 *
 * Every API call sends X-Actor-Id = the person selected in "Viewing as".
 */
"use strict";

// =========================================================================
// Helpers
// =========================================================================

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

const state = {
  employees: [],
  actor: null,
  view: "overview",
  historyPolicyId: null,
  payrollPeriod: null,
  payrollMessage: null,
  auditFilter: "",
};

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Actor-Id": state.actor ? state.actor.id : "" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* empty body */
  }
  if (!response.ok) {
    let message = data && data.detail;
    if (Array.isArray(message)) {
      // Pydantic validation errors
      message = message.map((d) => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join("; ");
    }
    throw new Error(message || `Request failed (${response.status})`);
  }
  return data;
}

// ----- dates -----

function toISO(d) {
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}
function parseISO(value) {
  const [y, m, d] = value.split("-").map(Number);
  return new Date(y, m - 1, d);
}
function addDays(value, n) {
  const d = parseISO(value);
  d.setDate(d.getDate() + n);
  return toISO(d);
}
const todayISO = () => toISO(new Date());

function fmtDate(value, { year = true, weekday = false } = {}) {
  return parseISO(value).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(year ? { year: "numeric" } : {}),
    ...(weekday ? { weekday: "short" } : {}),
  });
}
function fmtRange(start, end) {
  if (start === end) return fmtDate(start);
  const sameYear = start.slice(0, 4) === end.slice(0, 4);
  return `${fmtDate(start, { year: !sameYear })} – ${fmtDate(end)}`;
}
function fmtDateTime(value) {
  return new Date(value).toLocaleString("en-US", {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
  });
}

// ----- amounts (the API always uses minutes) -----

const round2 = (n) => Math.round(n * 100) / 100;
function num(n) {
  const r = round2(n);
  return Number.isInteger(r) ? String(r) : r.toFixed(2).replace(/0$/, "");
}
function daysText(minutes, minutesPerDay) {
  const d = round2(minutes / minutesPerDay);
  return `${num(d)} ${Math.abs(d) === 1 ? "day" : "days"}`;
}
const dollars = (n) => n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const money = (n) => n.toLocaleString("en-US", { style: "currency", currency: "USD" });
const hoursText = (minutes) => `${num(minutes / 60)}h`;
function signed(minutes) {
  const text = hoursText(Math.abs(minutes));
  return minutes > 0 ? `+${text}` : minutes < 0 ? `−${text}` : text;
}

function unitText(amount, unit) {
  if (unit === "days") return `${num(amount)} ${amount === 1 ? "day" : "days"}`;
  return `${num(amount)} ${amount === 1 ? "hour" : "hours"}`;
}

// "How you earn it" sentences for an employee, built from the policy rules.
function earningRules(rules, hoursPerDay) {
  if (rules.accrual_method === "unlimited") {
    return ["There's no balance to earn. Your time off is still tracked and approved."];
  }
  const lines = [];
  const inHours = rules.rate_unit === "days" ? ` (${num(rules.rate * hoursPerDay)}h for you)` : "";
  if (rules.accrual_method === "time_based") {
    const period = rules.frequency === "monthly" ? "month" : "year";
    const when = rules.accrue_at === "start_of_period" ? `on the first day of every ${period}` : `at the end of every ${period}`;
    lines.push(`You earn ${unitText(rules.rate, rules.rate_unit)}${inHours} ${when}.`);
  } else {
    lines.push(
      `You earn ${unitText(rules.rate, rules.rate_unit)} for every ${num(rules.hours_worked_per_accrual)} hours you work. It's added after each payroll.`
    );
  }
  if (rules.tenure_tier_years) {
    lines.push(`That's the rate for ${rules.tenure_tier_years}+ years at the company (you have ${rules.years_of_service}).`);
  } else if (rules.tenure_tiers.length > 1) {
    const next = rules.tenure_tiers.map((t) => t.min_years).filter((y) => y > rules.years_of_service).sort((a, b) => a - b)[0];
    if (next) lines.push(`Your rate goes up after ${next} years at the company.`);
  }
  if (rules.carryover_cap_hours === null) lines.push("Unused time carries over to next year.");
  else if (rules.carryover_cap_hours === 0) lines.push("Unused time expires at the end of the year.");
  else lines.push(`Up to ${num(rules.carryover_cap_hours)}h carries over to next year. The rest expires on Dec 31.`);
  if (rules.max_balance_hours !== null) lines.push(`Your balance stops growing at ${num(rules.max_balance_hours)}h.`);
  if (rules.allow_negative) lines.push(`You can go up to ${num(rules.max_negative_hours)}h below zero if you need to.`);
  return lines;
}

const employeeById = (id) => state.employees.find((e) => e.id === id);
const nameOf = (id) => (employeeById(id) ? employeeById(id).name : id);
const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function statusBadge(status) {
  return `<span class="badge ${esc(status)}">${esc(capitalize(status))}</span>`;
}

function emptyState(title, text) {
  return `<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;
}

// ----- toast -----

let toastTimer;
function toast(message, kind = "ok") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), kind === "error" ? 5000 : 3200);
}

// ----- modal -----

let lastFocus = null;
function openModal(html, { wide = false } = {}) {
  lastFocus = document.activeElement;
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="overlay">
      <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">${html}</div>
    </div>`;
  const overlay = root.firstElementChild;
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) closeModal();
  });
  $$("[data-close]", root).forEach((b) => b.addEventListener("click", closeModal));
  document.addEventListener("keydown", closeOnEscape);
  const first = $("input:not([type=hidden]), select, textarea", root);
  if (first) first.focus();
  return $(".modal", root);
}
function closeModal() {
  $("#modal-root").innerHTML = "";
  document.removeEventListener("keydown", closeOnEscape);
  if (lastFocus) lastFocus.focus();
}
function closeOnEscape(e) {
  if (e.key === "Escape") closeModal();
}
function modalHead(title, subtitle = "") {
  return `
    <div class="modal-head">
      <div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div>
      <button type="button" class="close" data-close aria-label="Close">×</button>
    </div>`;
}
function showFormError(modal, message) {
  const box = $(".form-error", modal);
  box.textContent = message;
  box.hidden = !message;
}

async function withBusy(button, fn) {
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.disabled = false;
  }
}

// =========================================================================
// Employee: My time off
// =========================================================================

async function viewOverview(main) {
  const me = state.actor;
  const [balances, requests] = await Promise.all([
    api(`/employees/${me.id}/balances`),
    api(`/time-off-requests?employee_id=${me.id}`),
  ]);
  const perDay = me.minutes_per_day;
  const today = todayISO();

  requests.sort((a, b) => b.start_date.localeCompare(a.start_date));

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>My time off</h1>
        <p>Your balances update as you accrue time and as requests are approved. One day is ${num(me.hours_per_day)} hours for you.</p>
      </div>
      <button class="btn" id="new-request" ${balances.length ? "" : "disabled"}>Request time off</button>
    </div>

    <section>
      ${
        balances.length
          ? `<div class="balances">${balances.map((b) => balanceCard(b, perDay)).join("")}</div>`
          : `<div class="panel">${emptyState("No time off policies yet", "Ask an admin to add you to a policy.")}</div>`
      }
    </section>

    <section>
      <div class="section-head"><h2>My requests</h2></div>
      <div class="panel table-wrap">
        ${
          requests.length
            ? `<table>
                <thead><tr><th>Dates</th><th>Type</th><th class="num">Amount</th><th>Reason</th><th>Status</th><th></th></tr></thead>
                <tbody>${requests.map((r) => myRequestRow(r, today)).join("")}</tbody>
              </table>`
            : emptyState("No requests yet", "Requests you submit will show up here.")
        }
      </div>
    </section>`;

  $("#new-request").addEventListener("click", () => openRequestModal(balances));
  $$("[data-cancel]", main).forEach((btn) =>
    btn.addEventListener("click", () =>
      openDecisionModal({
        requestId: btn.dataset.cancel,
        action: "cancel",
        title: "Cancel this request?",
        subtitle: "The hours go back to your balance.",
        noteLabel: "Reason (optional)",
        confirmText: "Cancel request",
        danger: true,
        done: "Request cancelled",
      })
    )
  );
}

function balanceCard(b, perDay) {
  if (b.unlimited) {
    return `
      <article class="panel balance-card unlimited">
        <header><h3>${esc(b.policy_name)}</h3><span class="tag">${esc(capitalize(b.time_off_type))}</span></header>
        <div class="balance-figure"><strong>Unlimited</strong></div>
        <p class="muted small">${daysText(b.used.minutes, perDay)} taken${
          b.pending.minutes ? `, ${daysText(b.pending.minutes, perDay)} pending` : ""
        }.</p>
        <p class="rule">${esc(earningRules(b.rules, perDay / 60)[0])}</p>
      </article>`;
  }

  const used = Math.max(b.used.minutes, 0);
  const pending = Math.max(b.pending.minutes, 0);
  const available = b.available.minutes;
  const total = used + pending + Math.max(available, 0) || 1;
  const pct = (m) => `${(Math.max(m, 0) / total) * 100}%`;
  const totalDays = total / perDay;
  const ticks = totalDays <= 60 ? `style="--tick: ${100 / totalDays}%"` : "";

  return `
    <article class="panel balance-card">
      <header><h3>${esc(b.policy_name)}</h3><span class="tag">${esc(capitalize(b.time_off_type))}</span></header>
      <div class="balance-figure ${available < 0 ? "negative" : ""}">
        <strong>${num(available / perDay)}</strong>
        <span>${Math.abs(round2(available / perDay)) === 1 ? "day" : "days"} available · ${hoursText(available)}</span>
      </div>
      <div class="meter ${ticks ? "" : "no-ticks"}" ${ticks} role="img"
           aria-label="${daysText(used, perDay)} used, ${daysText(pending, perDay)} pending, ${daysText(available, perDay)} available">
        <i class="used" style="width:${pct(used)}"></i>
        <i class="pending" style="width:${pct(pending)}"></i>
        <i class="available" style="width:${pct(available)}"></i>
      </div>
      <div class="legend">
        <span><i class="used"></i>${daysText(used, perDay)} used</span>
        ${pending ? `<span><i class="pending"></i>${daysText(pending, perDay)} pending</span>` : ""}
        <span><i class="available"></i>${daysText(Math.max(available, 0), perDay)} left</span>
      </div>
      <p class="rule">${esc(earningRules(b.rules, perDay / 60)[0])}</p>
    </article>`;
}

function requestStatusNote(r, viewerId) {
  if (r.status === "cancelled") {
    const who = r.cancelled_by === viewerId ? "you" : nameOf(r.cancelled_by);
    const verb = r.cancelled_by === r.employee_id ? "Cancelled" : "Revoked";
    return `${verb} by ${esc(who)}${r.cancel_reason ? `: “${esc(r.cancel_reason)}”` : ""}`;
  }
  if (r.decided_by) {
    const who = r.decided_by === viewerId ? "you" : nameOf(r.decided_by);
    return `by ${esc(who)}${r.decision_note ? `: “${esc(r.decision_note)}”` : ""}`;
  }
  return "";
}

function myRequestRow(r, today) {
  const canCancel = r.status === "pending" || (r.status === "approved" && r.start_date > today);
  const note = requestStatusNote(r, state.actor.id);
  return `
    <tr>
      <td>${fmtRange(r.start_date, r.end_date)}</td>
      <td>${esc(r.policy_name)}</td>
      <td class="num">${daysText(r.total_minutes, r.minutes_per_day)}<span class="cell-sub">${hoursText(r.total_minutes)}</span></td>
      <td>${esc(r.reason)}</td>
      <td>${statusBadge(r.status)}${note ? `<span class="cell-sub">${note}</span>` : ""}</td>
      <td class="actions">${canCancel ? `<button class="btn secondary sm" data-cancel="${esc(r.id)}">Cancel</button>` : ""}</td>
    </tr>`;
}

async function openRequestModal(balances) {
  const me = state.actor;
  const perDay = me.minutes_per_day;
  const holidays = await api(`/holidays?company_id=${me.company_id}`);
  const holidayName = (day) => {
    const h = holidays.find((x) => x.day === day && (!x.location || x.location === me.location));
    return h ? h.name : null;
  };

  let start = todayISO();
  while ([0, 6].includes(parseISO(start).getDay())) start = addDays(start, 1);

  const strip = balances
    .map((b) => {
      if (b.unlimited) return `<div><span><b>${esc(b.policy_name)}</b></span><span>Unlimited</span></div>`;
      const avail = b.available.minutes;
      const total = Math.max(b.used.minutes, 0) + Math.max(b.pending.minutes, 0) + Math.max(avail, 0) || 1;
      const width = Math.max(0, Math.min(100, (Math.max(avail, 0) / total) * 100));
      return `
        <div>
          <span><b>${esc(b.policy_name)}</b></span>
          <span>${daysText(b.used.minutes, perDay)} taken, ${daysText(avail, perDay)} left</span>
          <span class="strip-bar"><i style="width:${width}%"></i></span>
        </div>`;
    })
    .join("");

  const modal = openModal(`
    ${modalHead("Request time off", "Weekends and company holidays are not taken from your balance.")}
    <form class="modal-body" id="request-form" novalidate>
      <div class="balance-strip">${strip}</div>

      <label class="field">
        <span>Reason <span class="req">*</span></span>
        <input type="text" name="reason" placeholder="For example: family trip" maxlength="200" required>
      </label>

      <label class="field">
        <span>Category <span class="req">*</span></span>
        <select name="policy_id">
          ${balances.map((b) => `<option value="${esc(b.policy_id)}">${esc(b.policy_name)}</option>`).join("")}
        </select>
      </label>

      <div class="divider"></div>

      <label class="field">
        <span>How long?</span>
        <select name="length">
          <option value="full">Full day(s)</option>
          <option value="partial">Part of a day</option>
        </select>
      </label>

      <div class="grid-2" data-length="full">
        <label class="field"><span>First day <span class="req">*</span></span><input type="date" name="from" value="${start}"></label>
        <label class="field"><span>Last day <span class="req">*</span></span><input type="date" name="to" value="${start}"></label>
      </div>

      <div class="grid-3" data-length="partial" hidden>
        <label class="field"><span>Day <span class="req">*</span></span><input type="date" name="day" value="${start}"></label>
        <label class="field"><span>From</span><input type="time" name="start_time" value="13:00" step="900"></label>
        <label class="field"><span>To</span><input type="time" name="end_time" value="17:00" step="900"></label>
      </div>

      <div class="preview" id="preview" aria-live="polite">Pick your dates to see how much this uses.</div>
      <div class="form-error" hidden></div>
    </form>
    <div class="modal-foot">
      <button type="button" class="btn secondary" data-close>Cancel</button>
      <button type="submit" class="btn" form="request-form" id="submit-request">Submit request</button>
    </div>`);

  const form = $("#request-form", modal);
  const preview = $("#preview", modal);

  const toMinutes = (hhmm) => {
    const [h, m] = hhmm.split(":").map(Number);
    return h * 60 + m;
  };

  function showLength() {
    const partial = form.length.value === "partial";
    $("[data-length=full]", modal).hidden = partial;
    $("[data-length=partial]", modal).hidden = !partial;
  }

  // Turns the form into the list of days the API expects.
  function readDays() {
    if (form.length.value === "partial") {
      if (!form.day.value || !form.start_time.value || !form.end_time.value) throw new Error("Pick a day and a start and end time.");
      const minutes = toMinutes(form.end_time.value) - toMinutes(form.start_time.value);
      if (minutes <= 0) throw new Error("The end time must be after the start time.");
      if (minutes >= perDay) throw new Error(`That's a full day for you (${num(me.hours_per_day)}h). Choose "Full day(s)" instead.`);
      return [{ day: form.day.value, minutes }];
    }
    const from = form.from.value;
    const to = form.to.value;
    if (!from || !to) throw new Error("Pick a first and last day.");
    if (to < from) throw new Error("The last day is before the first day.");
    const days = [];
    for (let d = from; d <= to; d = addDays(d, 1)) {
      days.push({ day: d, minutes: null }); // null = a full working day
      if (days.length > 92) throw new Error("Requests can cover at most 3 months.");
    }
    return days;
  }

  let previewSeq = 0;
  async function updatePreview() {
    const seq = ++previewSeq;
    let days;
    try {
      days = readDays();
    } catch (err) {
      preview.className = "preview warn";
      preview.textContent = err.message;
      return;
    }
    try {
      const p = await api("/time-off-requests/preview", {
        method: "POST",
        body: { employee_id: me.id, policy_id: form.policy_id.value, days },
      });
      if (seq !== previewSeq) return; // a newer preview is on its way
      const skipped = p.skipped_days.map((d) => {
        const name = holidayName(d);
        return `${fmtDate(d, { year: false, weekday: true })} (${name ? esc(name) : "weekend"})`;
      });
      const after = p.available_after_minutes;
      preview.className = `preview ${after !== null && after < 0 ? "warn" : ""}`;
      preview.innerHTML = `
        <strong>Uses ${hoursText(p.total_minutes)} (${daysText(p.total_minutes, perDay)})</strong>
        ${skipped.length ? `<span class="muted">Not counted: ${skipped.join(", ")}</span>` : ""}
        ${
          after === null
            ? `<span class="muted">Unlimited policy, nothing is taken from a balance.</span>`
            : `<span>Balance after this request: <b>${hoursText(after)}</b> (${daysText(after, perDay)})</span>`
        }
        ${after !== null && after < 0 ? `<span>This goes below zero. It's only allowed if the policy permits a negative balance.</span>` : ""}`;
    } catch (err) {
      if (seq !== previewSeq) return;
      preview.className = "preview warn";
      preview.textContent = err.message;
    }
  }

  form.length.addEventListener("change", () => {
    showLength();
    updatePreview();
  });
  ["from", "to", "day", "start_time", "end_time", "policy_id"].forEach((name) =>
    form[name].addEventListener("input", updatePreview)
  );
  form.from.addEventListener("change", () => {
    if (form.to.value < form.from.value) form.to.value = form.from.value;
    updatePreview();
  });
  showLength();
  updatePreview();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormError(modal, "");
    try {
      if (!form.reason.value.trim()) throw new Error("Add a reason so your admin knows what this is for.");
      const days = readDays();
      await withBusy($("#submit-request", modal), () =>
        api("/time-off-requests", {
          method: "POST",
          body: { employee_id: me.id, policy_id: form.policy_id.value, reason: form.reason.value.trim(), days },
        })
      );
      closeModal();
      toast("Request submitted. Your admin will review it.");
      render();
    } catch (err) {
      showFormError(modal, err.message);
    }
  });
}

// =========================================================================
// Employee: Balance history (the ledger)
// =========================================================================

const TXN_LABELS = {
  accrual: "Earned",
  hold: "Reserved (request pending)",
  hold_release: "Reservation removed",
  usage: "Time off taken",
  usage_reversal: "Time off refunded",
  expiration: "Expired at year end",
  adjustment: "Adjustment",
};
const HOLD_TYPES = ["hold", "hold_release"];

async function viewHistory(main) {
  const me = state.actor;
  const balances = await api(`/employees/${me.id}/balances`);
  const tracked = balances.filter((b) => !b.unlimited);

  if (!tracked.length) {
    main.innerHTML = `
      <div class="page-head"><div><h1>Balance history</h1></div></div>
      <div class="panel">${emptyState("Nothing to show", "You have no policies with a balance.")}</div>`;
    return;
  }
  if (!tracked.some((b) => b.policy_id === state.historyPolicyId)) state.historyPolicyId = tracked[0].policy_id;
  const policyId = state.historyPolicyId;
  const current = tracked.find((b) => b.policy_id === policyId);
  const txns = await api(`/employees/${me.id}/policies/${policyId}/transactions`);

  txns.sort((a, b) => a.effective_date.localeCompare(b.effective_date) || a.created_at.localeCompare(b.created_at));
  let running = 0;
  const rows = txns.map((t) => {
    const isHold = HOLD_TYPES.includes(t.type);
    if (!isHold) running += t.amount_minutes;
    return { ...t, isHold, running };
  });
  rows.reverse();

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Balance history</h1>
        <p>Every change to your balance, newest first.</p>
      </div>
      <label class="field" style="min-width:240px">
        <span>Policy</span>
        <select id="history-policy">
          ${tracked.map((b) => `<option value="${esc(b.policy_id)}" ${b.policy_id === policyId ? "selected" : ""}>${esc(b.policy_name)}</option>`).join("")}
        </select>
      </label>
    </div>

    <div class="panel panel-body explain">
      <h2>How this balance works</h2>
      <ul>${earningRules(current.rules, me.hours_per_day).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
      <p class="muted small">
        When you request time off, the hours are <b>reserved</b> right away so they can't be used twice.
        If it's approved, the reservation is removed and the hours are <b>taken</b>. If it's rejected or cancelled, the reservation is just removed.
        Reserved rows are grey because they don't change your balance, only what's left to request.
        The date is the day the time off or accrual applies to, so a trip next month shows next month's date.
      </p>
    </div>

    <div class="panel table-wrap" style="margin-top:14px">
      ${
        rows.length
          ? `<table>
              <thead><tr><th>Date</th><th>What happened</th><th class="num">Change</th><th class="num">Balance</th><th>Details</th><th>Recorded</th></tr></thead>
              <tbody>
                ${rows
                  .map(
                    (t) => `
                  <tr class="${t.isHold ? "row-muted" : ""}">
                    <td>${fmtDate(t.effective_date)}</td>
                    <td>${esc(TXN_LABELS[t.type] || t.type)}</td>
                    <td class="num ${t.isHold ? "" : t.amount_minutes > 0 ? "positive" : t.amount_minutes < 0 ? "negative" : ""}">${signed(t.amount_minutes)}</td>
                    <td class="num">${t.isHold ? "" : hoursText(t.running)}</td>
                    <td class="small">${esc(t.note)}</td>
                    <td class="small muted" style="white-space:nowrap">${fmtDate(t.created_at.slice(0, 10))}</td>
                  </tr>`
                  )
                  .join("")}
              </tbody>
            </table>`
          : emptyState("No activity yet", "Accruals and approved requests will appear here.")
      }
    </div>`;

  $("#history-policy").addEventListener("change", (e) => {
    state.historyPolicyId = e.target.value;
    render();
  });
}

// =========================================================================
// Admin: Approvals
// =========================================================================

async function viewApprovals(main) {
  const all = await api("/time-off-requests");
  const pending = all.filter((r) => r.status === "pending").sort((a, b) => a.start_date.localeCompare(b.start_date));
  const decided = all
    .filter((r) => r.status !== "pending")
    .sort((a, b) => (b.cancelled_at || b.decided_at || b.created_at).localeCompare(a.cancelled_at || a.decided_at || a.created_at))
    .slice(0, 20);
  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Approvals</h1>
        <p>Pending requests already reserve the employee's balance, so approving never takes someone below what the policy allows.</p>
      </div>
    </div>

    <section>
      <div class="section-head"><h2>Waiting for you</h2><p>${pending.length} pending</p></div>
      <div class="panel table-wrap">
        ${
          pending.length
            ? `<table>
                <thead><tr><th>Employee</th><th>Dates</th><th>Type</th><th class="num">Amount</th><th>Reason</th><th></th></tr></thead>
                <tbody>
                  ${pending
                    .map(
                      (r) => `
                    <tr>
                      <td>${esc(r.employee_name)}<span class="cell-sub">Submitted ${fmtDateTime(r.created_at)}</span></td>
                      <td>${fmtRange(r.start_date, r.end_date)}</td>
                      <td>${esc(r.policy_name)}</td>
                      <td class="num">${daysText(r.total_minutes, r.minutes_per_day)}<span class="cell-sub">${hoursText(r.total_minutes)}</span></td>
                      <td>${esc(r.reason)}</td>
                      <td class="actions">
                        <button class="btn secondary sm" data-reject="${esc(r.id)}">Reject</button>
                        <button class="btn sm" data-approve="${esc(r.id)}">Approve</button>
                      </td>
                    </tr>`
                    )
                    .join("")}
                </tbody>
              </table>`
            : emptyState("You're all caught up", "New requests from employees will appear here.")
        }
      </div>
    </section>

    <section>
      <div class="section-head"><h2>Recently decided</h2></div>
      <div class="panel table-wrap">
        ${
          decided.length
            ? `<table>
                <thead><tr><th>Employee</th><th>Dates</th><th>Type</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead>
                <tbody>
                  ${decided
                    .map(
                      (r) => `
                    <tr>
                      <td>${esc(r.employee_name)}</td>
                      <td>${fmtRange(r.start_date, r.end_date)}</td>
                      <td>${esc(r.policy_name)}</td>
                      <td class="num">${daysText(r.total_minutes, r.minutes_per_day)}</td>
                      <td>${statusBadge(r.status)}<span class="cell-sub">${requestStatusNote(r, state.actor.id)}</span></td>
                      <td class="actions">${
                        r.status === "approved" ? `<button class="btn secondary sm" data-revoke="${esc(r.id)}">Revoke</button>` : ""
                      }</td>
                    </tr>`
                    )
                    .join("")}
                </tbody>
              </table>`
            : emptyState("Nothing decided yet", "Approved and rejected requests will show up here.")
        }
      </div>
    </section>`;

  $$("[data-approve]", main).forEach((btn) =>
    btn.addEventListener("click", () =>
      openDecisionModal({
        requestId: btn.dataset.approve,
        action: "approve",
        title: "Approve request",
        subtitle: "The hours are taken from the employee's balance.",
        noteLabel: "Note for the employee (optional)",
        placeholder: "For example: have a great trip!",
        confirmText: "Approve",
        done: "Request approved",
      })
    )
  );
  $$("[data-reject]", main).forEach((btn) =>
    btn.addEventListener("click", () =>
      openDecisionModal({
        requestId: btn.dataset.reject,
        action: "reject",
        title: "Reject request",
        subtitle: "The reserved hours go back to the employee.",
        noteLabel: "Note for the employee (optional)",
        placeholder: "For example: we're short-staffed that week",
        confirmText: "Reject request",
        danger: true,
        done: "Request rejected",
      })
    )
  );
  $$("[data-revoke]", main).forEach((btn) =>
    btn.addEventListener("click", () =>
      openDecisionModal({
        requestId: btn.dataset.revoke,
        action: "revoke",
        title: "Revoke approved time off",
        subtitle: "Use this when plans changed or it was approved by mistake. The hours go back to the employee's balance.",
        noteLabel: "Reason",
        placeholder: "For example: trip cancelled, came back to work",
        required: true,
        confirmText: "Revoke",
        danger: true,
        done: "Time off revoked",
      })
    )
  );
}

// Used for approve, reject, cancel and revoke. Only the words change.
function openDecisionModal({ requestId, action, title, subtitle, noteLabel, placeholder = "", required = false, confirmText, danger = false, done }) {
  const modal = openModal(`
    ${modalHead(title, subtitle)}
    <form class="modal-body" id="decision-form">
      <label class="field">
        <span>${esc(noteLabel)}${required ? ' <span class="req">*</span>' : ""}</span>
        <textarea name="note" placeholder="${esc(placeholder)}"></textarea>
      </label>
      <div class="form-error" hidden></div>
    </form>
    <div class="modal-foot">
      <button type="button" class="btn secondary" data-close>Go back</button>
      <button type="submit" form="decision-form" class="btn ${danger ? "danger" : ""}" id="confirm-decision">${esc(confirmText)}</button>
    </div>`);

  $("#decision-form", modal).addEventListener("submit", async (e) => {
    e.preventDefault();
    const note = e.target.note.value.trim() || null;
    try {
      if (required && !note) throw new Error("Add a reason. The employee will see it.");
      const body =
        action === "cancel" ? { reason: note } : action === "revoke" ? { reason: note, by_admin: true } : { note };
      const path = action === "revoke" ? "cancel" : action;
      await withBusy($("#confirm-decision", modal), () =>
        api(`/time-off-requests/${requestId}/${path}`, { method: "POST", body })
      );
      closeModal();
      toast(done);
      render();
    } catch (err) {
      showFormError(modal, err.message);
    }
  });
}

// =========================================================================
// Admin: Policies
// =========================================================================

const LABELS = {
  frequency: { monthly: "Monthly", yearly: "Yearly" },
  accrue_at: { start_of_period: "Start of period", end_of_period: "End of period" },
  rate_unit: { days: "Day", hours: "Hour" },
  proration_rule: {
    prorate: "Prorated",
    full: "Full amount",
    next_period: "Starts next period",
  },
  accrual_method: { unlimited: "Unlimited", time_based: "Over time", hours_worked: "Per hours worked" },
};

// "Years 0-4: 1.25 days a month, 5+ years: 1.5 days a month"
function tiersText(v) {
  const per = v.accrual_method === "hours_worked" ? `per ${num(v.hours_worked_per_accrual)}h worked` : `a ${v.frequency === "yearly" ? "year" : "month"}`;
  const tiers = [...v.tenure_tiers].sort((a, b) => a.min_years - b.min_years);
  if (tiers.length && tiers[0].min_years > 0) tiers.unshift({ min_years: 0, rate_amount: v.rate_amount });
  return tiers
    .map((t, i) => {
      const next = tiers[i + 1];
      const years = next ? `Years ${t.min_years}-${next.min_years - 1}` : `${t.min_years}+ years`;
      return `${years}: ${unitText(t.rate_amount, v.rate_unit)} ${per}`;
    })
    .join(", ");
}

function describeVersion(v) {
  if (v.accrual_method === "unlimited") {
    return [["Accrual", "Unlimited"], ["Balance", "Not tracked"]];
  }
  const unit = v.rate_unit === "days" ? "days" : "hours";
  const rows = [];
  if (v.accrual_method === "time_based") {
    rows.push(["Schedule", LABELS.frequency[v.frequency]]);
    rows.push(["Rate", `${num(v.rate_amount)} ${unit} per ${v.frequency === "monthly" ? "month" : "year"}`]);
    rows.push(["Accrues", LABELS.accrue_at[v.accrue_at]]);
  } else {
    rows.push(["Schedule", "Every payroll run"]);
    rows.push(["Rate", `${num(v.rate_amount)} ${unit} per ${num(v.hours_worked_per_accrual)} hours worked`]);
  }
  rows.push(["Unit", LABELS.rate_unit[v.rate_unit]]);
  rows.push([
    "Carryover",
    v.carryover_cap_hours === null ? "Everything carries over" : v.carryover_cap_hours === 0 ? "Use it or lose it" : `Up to ${num(v.carryover_cap_hours)}h`,
  ]);
  rows.push(["Max balance", v.max_balance_hours === null ? "No cap" : `${num(v.max_balance_hours)}h`]);
  rows.push(["Negative balance", v.allow_negative ? `Up to ${num(v.max_negative_hours)}h` : "Not allowed"]);
  rows.push(["Joining mid-period", LABELS.proration_rule[v.proration_rule]]);
  if (v.waiting_period_days) rows.push(["Waiting period", `${v.waiting_period_days} days`]);
  if (v.tenure_tiers.length) rows.push(["By years at the company", tiersText(v)]);
  return rows;
}

async function viewPolicies(main) {
  const policies = await api("/policies");
  const today = todayISO();

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Policies</h1>
        <p>Group employees however you like, for example full-time vs part-time, or employee vs contractor. Editing a policy creates a new version, so past balances never change.</p>
      </div>
      <button class="btn" id="new-policy">New policy</button>
    </div>
    <div class="policy-list">
      ${
        policies.length
          ? policies.map((p) => policyCard(p, today)).join("")
          : `<div class="panel">${emptyState("No policies yet", "Create one to start tracking time off.")}</div>`
      }
    </div>`;

  $("#new-policy").addEventListener("click", openNewPolicyModal);
  $$("[data-edit]", main).forEach((b) =>
    b.addEventListener("click", () => openEditPolicyModal(policies.find((p) => p.policy.id === b.dataset.edit)))
  );
  $$("[data-assign]", main).forEach((b) => b.addEventListener("click", () => openAssignmentsModal(b.dataset.assign)));
  $$("[data-versions]", main).forEach((b) => b.addEventListener("click", () => openVersionsModal(b.dataset.versions)));
}

function policyCard(p, today) {
  const v = p.current_version;
  const active = p.assignments.filter((a) => !a.end_date || a.end_date >= today);
  const names = active.map((a) => esc(a.employee_name)).join(", ");
  return `
    <article class="panel policy-card">
      <div class="panel-body">
        <header>
          <div>
            <h3>${esc(p.policy.name)}</h3>
            <p>${esc(capitalize(p.policy.time_off_type))} policy. Version ${v.version_number} since ${fmtDate(v.effective_from)}.</p>
          </div>
          <div class="tags">
            <span class="tag">${p.policy.is_paid ? "Paid" : "Unpaid"}</span>
          </div>
        </header>
        <dl class="details">
          ${describeVersion(v).map(([k, val]) => `<div><dt>${esc(k)}</dt><dd>${esc(val)}</dd></div>`).join("")}
        </dl>
        ${
          p.upcoming_version
            ? `<p class="upcoming-note">Version ${p.upcoming_version.version_number} takes effect on ${fmtDate(p.upcoming_version.effective_from)}.</p>`
            : ""
        }
        <p class="assigned">${active.length ? `<strong>${active.length} assigned:</strong> ${names}` : "Nobody is assigned yet."}</p>
      </div>
      <div class="panel-foot">
        <button class="btn secondary sm" data-versions="${esc(p.policy.id)}">History</button>
        <button class="btn secondary sm" data-edit="${esc(p.policy.id)}">Edit policy</button>
        <button class="btn secondary sm" data-assign="${esc(p.policy.id)}">Manage assignments</button>
      </div>
    </article>`;
}

// ----- shared policy settings form -----

function settingsFields(v = {}) {
  const val = (key, fallback = "") => (v[key] === null || v[key] === undefined ? fallback : v[key]);
  const option = (key, value, label) => `<option value="${value}" ${val(key) === value ? "selected" : ""}>${label}</option>`;
  const tierRows = (v.tenure_tiers || []).map((t) => tierRow(t.min_years, t.rate_amount)).join("");
  return `
    <label class="field">
      <span>How is time off earned?</span>
      <select name="accrual_method">
        ${option("accrual_method", "time_based", "Over time (for example 20 days per year)")}
        ${option("accrual_method", "hours_worked", "Per hours worked (from payroll)")}
        ${option("accrual_method", "unlimited", "Unlimited, no balance")}
      </select>
    </label>

    <div class="grid-2" data-for="time_based">
      <label class="field"><span>Schedule</span>
        <select name="frequency">${option("frequency", "monthly", "Monthly")}${option("frequency", "yearly", "Yearly")}</select>
      </label>
      <label class="field"><span>Accrues</span>
        <select name="accrue_at">${option("accrue_at", "start_of_period", "Start of period")}${option("accrue_at", "end_of_period", "End of period")}</select>
      </label>
    </div>

    <div class="stack" data-for="time_based hours_worked">
      <div class="grid-3">
        <label class="field"><span>Rate</span><input type="number" name="rate_amount" step="0.01" min="0" value="${val("rate_amount", "")}"></label>
        <label class="field"><span>Unit</span>
          <select name="rate_unit">${option("rate_unit", "days", "Days")}${option("rate_unit", "hours", "Hours")}</select>
        </label>
        <label class="field" data-for="hours_worked"><span>Per hours worked</span><input type="number" name="hours_worked_per_accrual" step="0.5" min="0" value="${val("hours_worked_per_accrual", "")}"></label>
      </div>
      <div class="grid-2">
        <label class="field"><span>Carryover cap (hours)</span><input type="number" name="carryover_cap_hours" min="0" step="1" value="${val("carryover_cap_hours", "")}" placeholder="Empty: everything carries over"></label>
        <label class="field"><span>Max balance (hours)</span><input type="number" name="max_balance_hours" min="0" step="1" value="${val("max_balance_hours", "")}" placeholder="Empty: no cap"></label>
      </div>
      <div class="grid-2">
        <label class="field"><span>Joining mid-period</span>
          <select name="proration_rule">
            ${option("proration_rule", "prorate", "Prorate the first period")}
            ${option("proration_rule", "full", "Give the full amount")}
            ${option("proration_rule", "next_period", "Start next period")}
          </select>
        </label>
        <label class="field"><span>Waiting period (days)</span><input type="number" name="waiting_period_days" min="0" step="1" value="${val("waiting_period_days", 0)}"></label>
      </div>
      <div class="grid-2">
        <label class="check" style="align-self:end;height:38px"><input type="checkbox" name="allow_negative" ${v.allow_negative ? "checked" : ""}> Allow a negative balance</label>
        <label class="field"><span>How far below zero (hours)</span><input type="number" name="max_negative_hours" min="0" step="1" value="${val("max_negative_hours", 8)}"></label>
      </div>
      <div class="field">
        <span>Rate by years at the company (optional)</span>
        <div class="tiers" id="tiers">${tierRows}</div>
        <div><button type="button" class="btn secondary sm" id="add-tier">Add a tier</button></div>
        <span class="hint">For example "From year 5, 1.5 days": people with 5+ years earn 1.5 instead of the normal rate.</span>
      </div>
    </div>`;
}

function tierRow(years = "", rate = "") {
  return `
    <div class="tier-row">
      <span>From year</span>
      <input type="number" min="0" step="1" data-tier="years" value="${esc(years)}" aria-label="From year">
      <span>earn</span>
      <input type="number" min="0" step="0.01" data-tier="rate" value="${esc(rate)}" aria-label="Rate">
      <span class="tier-unit"></span>
      <button type="button" class="btn secondary sm" data-remove-tier aria-label="Remove tier">Remove</button>
    </div>`;
}

function bindSettingsFields(root) {
  const method = $("[name=accrual_method]", root);
  const update = () => {
    $$("[data-for]", root).forEach((el) => (el.hidden = !el.dataset.for.split(" ").includes(method.value)));
  };
  method.addEventListener("change", update);
  update();

  // Only let people set a limit when negative balances are allowed (point 9)
  const allow = $("[name=allow_negative]", root);
  const limit = $("[name=max_negative_hours]", root);
  const syncNegative = () => (limit.disabled = !allow.checked);
  allow.addEventListener("change", syncNegative);
  syncNegative();

  // Tenure tier rows
  const tiers = $("#tiers", root);
  const unit = $("[name=rate_unit]", root);
  const syncUnits = () => $$(".tier-unit", root).forEach((el) => (el.textContent = unit.value));
  $("#add-tier", root).addEventListener("click", () => {
    tiers.insertAdjacentHTML("beforeend", tierRow());
    syncUnits();
  });
  tiers.addEventListener("click", (e) => {
    if (e.target.matches("[data-remove-tier]")) e.target.closest(".tier-row").remove();
  });
  unit.addEventListener("change", syncUnits);
  syncUnits();
}

function readSettings(root) {
  const field = (name) => $(`[name=${name}]`, root);
  const numberOrNull = (name) => {
    const raw = field(name).value.trim();
    return raw === "" ? null : Number(raw);
  };
  const method = field("accrual_method").value;
  if (method === "unlimited") return { accrual_method: "unlimited" };

  const tiers = $$(".tier-row", root)
    .map((row) => ({ years: $("[data-tier=years]", row).value.trim(), rate: $("[data-tier=rate]", row).value.trim() }))
    .filter((t) => t.years !== "" || t.rate !== "")
    .map((t) => {
      const years = Number(t.years);
      const rate = Number(t.rate);
      if (t.years === "" || t.rate === "" || !Number.isInteger(years) || Number.isNaN(rate)) {
        throw new Error("Each tier needs a whole number of years and a rate.");
      }
      return { min_years: years, rate_amount: rate };
    })
    .sort((a, b) => a.min_years - b.min_years);

  return {
    accrual_method: method,
    frequency: method === "time_based" ? field("frequency").value : null,
    accrue_at: field("accrue_at").value,
    rate_amount: numberOrNull("rate_amount") ?? 0,
    rate_unit: field("rate_unit").value,
    hours_worked_per_accrual: method === "hours_worked" ? numberOrNull("hours_worked_per_accrual") : null,
    carryover_cap_hours: numberOrNull("carryover_cap_hours"),
    max_balance_hours: numberOrNull("max_balance_hours"),
    proration_rule: field("proration_rule").value,
    waiting_period_days: numberOrNull("waiting_period_days") ?? 0,
    allow_negative: field("allow_negative").checked,
    max_negative_hours: field("allow_negative").checked ? numberOrNull("max_negative_hours") ?? 0 : 0,
    tenure_tiers: tiers,
  };
}

function firstOfNextMonth() {
  const d = new Date();
  return toISO(new Date(d.getFullYear(), d.getMonth() + 1, 1));
}

function openNewPolicyModal() {
  const modal = openModal(
    `
    ${modalHead("New policy", "You can assign employees after it's created.")}
    <form class="modal-body" id="policy-form" novalidate>
      <div class="grid-2">
        <label class="field"><span>Name <span class="req">*</span></span><input type="text" name="name" placeholder="Part-Time Vacation"></label>
        <label class="field"><span>Type</span>
          <select name="time_off_type">
            ${["vacation", "sick", "parental", "bereavement", "wfh", "other"].map((t) => `<option value="${t}">${capitalize(t)}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="grid-2">
        <label class="field"><span>Starts on</span><input type="date" name="effective_from" value="${todayISO().slice(0, 4)}-01-01"></label>
        <label class="check" style="align-self:end;height:38px"><input type="checkbox" name="is_paid" checked> Paid time off</label>
      </div>
      <div class="divider"></div>
      ${settingsFields({ accrual_method: "time_based", frequency: "monthly", rate_amount: 1.25, rate_unit: "days", max_negative_hours: null })}
      <div class="form-error" hidden></div>
    </form>
    <div class="modal-foot">
      <button type="button" class="btn secondary" data-close>Cancel</button>
      <button type="submit" form="policy-form" class="btn" id="save-policy">Create policy</button>
    </div>`,
    { wide: true }
  );
  bindSettingsFields(modal);

  $("#policy-form", modal).addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    try {
      if (!form.name.value.trim()) throw new Error("Give the policy a name.");
      const result = await withBusy($("#save-policy", modal), () =>
        api("/policies", {
          method: "POST",
          body: {
            company_id: state.actor.company_id,
            name: form.name.value.trim(),
            time_off_type: form.time_off_type.value,
            is_paid: form.is_paid.checked,
            effective_from: form.effective_from.value,
            settings: readSettings(modal),
          },
        })
      );
      closeModal();
      toast(`Created ${result.policy.name}`);
      await render();
      openAssignmentsModal(result.policy.id);
    } catch (err) {
      showFormError(modal, err.message);
    }
  });
}

function sameValue(a, b) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

function openEditPolicyModal(p) {
  const v = p.upcoming_version || p.current_version;
  const modal = openModal(
    `
    ${modalHead(`Edit ${p.policy.name}`, "Changes create a new version. Time already earned stays as it is.")}
    <form class="modal-body" id="edit-form" novalidate>
      <label class="field">
        <span>New rules start on</span>
        <input type="date" name="effective_from" value="${firstOfNextMonth()}">
        <span class="hint">They apply from the first accrual period that starts on or after this date.</span>
      </label>
      <div class="divider"></div>
      ${settingsFields(v)}
      <div class="form-error" hidden></div>
    </form>
    <div class="modal-foot">
      <button type="button" class="btn secondary" data-close>Cancel</button>
      <button type="submit" form="edit-form" class="btn" id="save-edit">Save new version</button>
    </div>`,
    { wide: true }
  );
  bindSettingsFields(modal);

  $("#edit-form", modal).addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const settings = readSettings(modal);
      const changes = {};
      for (const [key, value] of Object.entries(settings)) {
        if (!sameValue(value, v[key])) changes[key] = value;
      }
      if (!Object.keys(changes).length) throw new Error("Nothing changed yet.");
      const version = await withBusy($("#save-edit", modal), () =>
        api(`/policies/${p.policy.id}`, {
          method: "PATCH",
          body: { effective_from: e.target.effective_from.value, changes },
        })
      );
      closeModal();
      toast(`Saved version ${version.version_number}, starting ${fmtDate(version.effective_from)}`);
      render();
    } catch (err) {
      showFormError(modal, err.message);
    }
  });
}

async function openVersionsModal(policyId) {
  const data = await api(`/policies/${policyId}`);
  const versions = [...data.versions].reverse();
  openModal(
    `
    ${modalHead(`${data.policy.name}: history`, "Every version of the rules, newest first. Old versions are kept so past balances can always be explained.")}
    <div class="modal-body">
      ${versions
        .map(
          (v) => `
        <div class="panel panel-body">
          <h3>Version ${v.version_number}</h3>
          <p class="muted small">From ${fmtDate(v.effective_from)}. Saved by ${esc(nameOf(v.created_by))} on ${fmtDateTime(v.created_at)}.</p>
          <dl class="details">${describeVersion(v).map(([k, val]) => `<div><dt>${esc(k)}</dt><dd>${esc(val)}</dd></div>`).join("")}</dl>
        </div>`
        )
        .join("")}
    </div>
    <div class="modal-foot"><button type="button" class="btn secondary" data-close>Close</button></div>`,
    { wide: true }
  );
}

async function openAssignmentsModal(policyId) {
  const policies = await api("/policies");
  const p = policies.find((x) => x.policy.id === policyId);
  const today = todayISO();
  const activeIds = new Set(p.assignments.filter((a) => !a.end_date || a.end_date >= today).map((a) => a.employee_id));
  const candidates = state.employees.filter((e) => e.company_id === p.policy.company_id && !activeIds.has(e.id));

  const modal = openModal(
    `
    ${modalHead(`Assignments: ${p.policy.name}`, "Who is on this policy and for which dates. Someone can only be on one policy per type at a time.")}
    <div class="modal-body">
      <div class="panel table-wrap">
        ${
          p.assignments.length
            ? `<table>
                <thead><tr><th>Employee</th><th>From</th><th>Until</th><th></th></tr></thead>
                <tbody>
                  ${p.assignments
                    .map(
                      (a) => `
                    <tr>
                      <td>${esc(a.employee_name)}</td>
                      <td>${fmtDate(a.start_date)}</td>
                      <td>${a.end_date ? fmtDate(a.end_date) : "No end date"}</td>
                      <td class="actions">${a.end_date ? "" : `<button class="btn secondary sm" data-end="${esc(a.id)}">End</button>`}</td>
                    </tr>`
                    )
                    .join("")}
                </tbody>
              </table>`
            : emptyState("Nobody assigned", "Add the first employee below.")
        }
      </div>

      <form id="assign-form" class="stack">
        <h3>Add an employee</h3>
        <div class="grid-2">
          <label class="field"><span>Employee</span>
            <select name="employee_id" ${candidates.length ? "" : "disabled"}>
              ${candidates.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("") || "<option>Everyone is assigned</option>"}
            </select>
          </label>
          <label class="field"><span>From</span><input type="date" name="start_date" value="${today}"></label>
        </div>
        <div><button type="submit" class="btn" id="assign-btn" ${candidates.length ? "" : "disabled"}>Add to policy</button></div>
      </form>
      <div class="form-error" hidden></div>
    </div>
    <div class="modal-foot"><button type="button" class="btn secondary" data-close>Done</button></div>`,
    { wide: true }
  );

  $("#assign-form", modal).addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await withBusy($("#assign-btn", modal), () =>
        api(`/policies/${policyId}/assignments`, {
          method: "POST",
          body: { employee_id: e.target.employee_id.value, start_date: e.target.start_date.value },
        })
      );
      toast(`Added ${nameOf(e.target.employee_id.value)}`);
      render();
      openAssignmentsModal(policyId);
    } catch (err) {
      showFormError(modal, err.message);
    }
  });

  $$("[data-end]", modal).forEach((btn) =>
    btn.addEventListener("click", async () => {
      const end = prompt("Last day on this policy (YYYY-MM-DD):", today);
      if (!end) return;
      try {
        await api(`/assignments/${btn.dataset.end}/end`, { method: "POST", body: { end_date: end } });
        toast("Assignment ended");
        render();
        openAssignmentsModal(policyId);
      } catch (err) {
        showFormError(modal, err.message);
      }
    })
  );
}

// =========================================================================
// Admin: Holidays
// =========================================================================

async function viewHolidays(main) {
  const companyId = state.actor.company_id;
  const holidays = await api(`/holidays?company_id=${companyId}`);
  const locations = [...new Set(state.employees.map((e) => e.location).filter(Boolean))].sort();

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Holidays</h1>
        <p>Holidays are never taken from anyone's balance. If you add one on a day someone already requested, that day is refunded automatically.</p>
      </div>
    </div>

    <section>
      <form class="panel" id="holiday-form">
        <div class="panel-body job-row">
          <label class="field"><span>Date</span><input type="date" name="day" required></label>
          <label class="field" style="flex:1"><span>Name</span><input type="text" name="name" placeholder="Company offsite" required></label>
          <label class="field"><span>Applies to</span>
            <select name="location">
              <option value="">Everyone</option>
              ${locations.map((l) => `<option value="${esc(l)}">Only ${esc(l)}</option>`).join("")}
            </select>
          </label>
          <button class="btn" id="add-holiday">Add holiday</button>
        </div>
      </form>
    </section>

    <section>
      <div class="panel table-wrap">
        ${
          holidays.length
            ? `<table>
                <thead><tr><th>Date</th><th>Holiday</th><th>Applies to</th></tr></thead>
                <tbody>
                  ${holidays
                    .map(
                      (h) => `<tr><td>${fmtDate(h.day, { weekday: true })}</td><td>${esc(h.name)}</td><td>${h.location ? `Only ${esc(h.location)}` : "Everyone"}</td></tr>`
                    )
                    .join("")}
                </tbody>
              </table>`
            : emptyState("No holidays yet", "Add your company's holidays above.")
        }
      </div>
    </section>`;

  $("#holiday-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    try {
      if (!form.day.value || !form.name.value.trim()) throw new Error("Pick a date and give the holiday a name.");
      const result = await withBusy($("#add-holiday"), () =>
        api("/holidays", {
          method: "POST",
          body: { company_id: companyId, day: form.day.value, name: form.name.value.trim(), location: form.location.value || null },
        })
      );
      const refunded = result.refunded_request_ids.length;
      toast(refunded ? `Holiday added. Refunded the day on ${refunded} request${refunded > 1 ? "s" : ""}.` : "Holiday added");
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

// =========================================================================
// Admin: Scheduled jobs (accruals and year-end)
// =========================================================================





async function viewJobs(main) {
  const policies = await api("/policies");
  const yearEndPolicies = policies.filter((p) => p.current_version.accrual_method !== "unlimited");

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Scheduled jobs</h1>
        <p>In production a scheduler runs these by itself: accruals every night, year-end on January 1. They're here so you can run them in the demo. Hours-worked accruals (like Sick Leave) come from payroll instead: see the <a href="#" data-goto="payroll">Payroll</a> page.</p>
      </div>
    </div>

    <div class="jobs">
      <form class="panel job-card" id="accrual-job">
        <div class="panel-body">
          <header><h2>Run time-based accruals</h2><p>Credits every monthly or yearly accrual that's due up to the date below. Running it twice is safe: anything already credited is skipped, and missed days are caught up. Hours-worked policies (like Sick Leave) are credited by payroll instead.</p></header>
          <div class="job-row">
            <label class="field"><span>Up to</span><input type="date" name="as_of" value="${todayISO()}"></label>
            <button class="btn" id="run-accruals">Run accruals</button>
          </div>
          <div class="job-result" hidden></div>
        </div>
      </form>

      <form class="panel job-card" id="year-end-job">
        <div class="panel-body">
          <header><h2>Run year-end</h2><p>Runs on Jan 1. It first makes sure every accrual of the year is posted, then expires anything above the policy's carryover cap. It refuses to run for a year that isn't over yet. In the demo you can pick the day it runs.</p></header>
          <div class="job-row">
            <label class="field"><span>Policy</span>
              <select name="policy_id">${yearEndPolicies.map((p) => `<option value="${esc(p.policy.id)}">${esc(p.policy.name)}</option>`).join("")}</select>
            </label>
            <label class="field"><span>Year to close</span><input type="number" name="year" value="${new Date().getFullYear()}" min="2000" max="2100"></label>
            <label class="field"><span>Run on</span><input type="date" name="run_date" value="${new Date().getFullYear() + 1}-01-01"></label>
            <button class="btn" id="run-year-end">Run year-end</button>
          </div>
          <div class="job-result" hidden></div>
        </div>
      </form>
    </div>`;

  $$("[data-goto]", main).forEach((a) =>
    a.addEventListener("click", (e) => {
      e.preventDefault();
      state.view = a.dataset.goto;
      render();
    })
  );

  function showResult(form, text, neutral) {
    const box = $(".job-result", form);
    box.hidden = false;
    box.className = `job-result ${neutral ? "neutral" : ""}`;
    box.textContent = text;
  }

  $("#accrual-job").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await withBusy($("#run-accruals"), () =>
        api("/jobs/run-accruals", { method: "POST", body: { as_of: e.target.as_of.value } })
      );
      showResult(e.target, r.posted ? `Credited ${r.posted} accrual${r.posted > 1 ? "s" : ""}.` : "Nothing new to credit. Everything up to this date was already posted.", !r.posted);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("#year-end-job").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await withBusy($("#run-year-end"), () =>
        api("/jobs/year-end", {
          method: "POST",
          body: { policy_id: e.target.policy_id.value, year: Number(e.target.year.value), run_date: e.target.run_date.value },
        })
      );
      if (!r.expired) {
        showResult(e.target, "Nothing expired. Everyone is under the cap, the policy lets everything carry over, or this year was already closed.", true);
      } else {
        showResult(e.target, `Expired: ${r.transactions.map((t) => `${nameOf(t.employee_id)} ${signed(t.amount_minutes)}`).join(", ")}.`);
      }
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

// =========================================================================
// Admin: People (data from the Employee and Company Services)
// =========================================================================

const EMPLOYMENT_LABELS = { full_time: "Full-time", part_time: "Part-time", contractor: "Contractor" };
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function payText(e) {
  if (e.pay_type === "salary") {
    return `${e.pay_rate.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })} a year`;
  }
  return `${money(e.pay_rate)} an hour`;
}

function weekText(days) {
  return days.join(",") === "0,1,2,3,4" ? "Mon-Fri" : days.map((d) => WEEKDAYS[d]).join(", ");
}

async function viewPeople(main) {
  const [company, policies] = await Promise.all([api(`/companies/${state.actor.company_id}`), api("/policies")]);
  const today = todayISO();
  const people = state.employees.filter((e) => e.company_id === company.id);
  const policiesOf = (id) =>
    policies
      .filter((p) => p.assignments.some((a) => a.employee_id === id && (!a.end_date || a.end_date >= today)))
      .map((p) => p.policy.name);

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>People</h1>
        <p>This data comes from the Employee Service and the Company Service. The Time Off Service only reads it: hire dates, work schedules and locations drive accruals, and pay data is used by payroll.</p>
      </div>
    </div>

    <section>
      <div class="panel panel-body">
        <h2>${esc(company.name)}</h2>
        <dl class="details">
          <div><dt>Locations</dt><dd>${esc(company.locations.join(", "))}</dd></div>
          <div><dt>Pay schedule</dt><dd>Twice a month (1st-15th, 16th-end)</dd></div>
          <div><dt>Standard work week</dt><dd>${weekText(company.work_weekdays)}, ${num(company.default_hours_per_day)}h a day</dd></div>
          <div><dt>Time zone</dt><dd>${esc(company.timezone)}</dd></div>
        </dl>
      </div>
    </section>

    <section>
      <div class="section-head"><h2>Employees</h2><p>${people.length} people</p></div>
      <div class="panel table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Type</th><th>Pay</th><th>Schedule</th><th>Hired</th><th>Time off policies</th></tr></thead>
          <tbody>
            ${people
              .map(
                (e) => `
              <tr>
                <td>${esc(e.name)}${e.is_admin ? ' <span class="tag">Admin</span>' : ""}<span class="cell-sub">${esc(e.title)}, ${esc(e.department)}</span></td>
                <td>${EMPLOYMENT_LABELS[e.employment_type]}<span class="cell-sub">${esc(e.location || "")}</span></td>
                <td>${payText(e)}<span class="cell-sub">${e.pay_type === "salary" ? "Salaried" : "Hourly"}</span></td>
                <td>${num(e.hours_per_day)}h a day<span class="cell-sub">${weekText(e.work_weekdays)}</span></td>
                <td>${fmtDate(e.hire_date)}</td>
                <td class="small">${policiesOf(e.id).map(esc).join("<br>") || '<span class="muted">None</span>'}</td>
              </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </section>`;
}

// =========================================================================
// Admin: Payroll (the simulated Payroll Service)
// =========================================================================

async function viewPayroll(main) {
  const companyId = state.actor.company_id;
  const [periods, runs] = await Promise.all([api("/payroll/periods?count=8"), api(`/payroll/runs?company_id=${companyId}`)]);
  const runFor = (start) => runs.find((r) => r.period_start === start);
  if (!periods.some((p) => p.period_start === state.payrollPeriod)) state.payrollPeriod = periods[0].period_start;
  const period = periods.find((p) => p.period_start === state.payrollPeriod);
  const existing = runFor(period.period_start);

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Payroll</h1>
        <p>A simplified stand-in for the Payroll Service. For each pay period it works out how many hours each person worked and what they're paid, then publishes <code>on_payroll_processed</code>. The Time Off Service listens and credits hours-worked policies like Sick Leave. Pay uses flat demo rates.</p>
      </div>
      <label class="field" style="min-width:240px">
        <span>Pay period</span>
        <select id="payroll-period">
          ${periods
            .map(
              (p) =>
                `<option value="${p.period_start}" ${p.period_start === period.period_start ? "selected" : ""}>${fmtRange(p.period_start, p.period_end)}${runFor(p.period_start) ? (runFor(p.period_start).is_outdated ? " (needs re-run)" : " (processed)") : ""}</option>`
            )
            .join("")}
        </select>
      </label>
    </div>

    <section>
      <div class="panel table-wrap" id="payroll-preview"><p class="loading" style="padding:20px">Calculating…</p></div>
      <div class="payroll-actions">
        <p class="muted small" id="payroll-status">${
          existing && existing.is_outdated
            ? `<span class="outdated">Time off or holidays in this period changed after it was processed. Re-run payroll to update.</span><br>`
            : ""
        }${
          existing
            ? `Processed by ${esc(existing.processed_by_name)} on ${fmtDateTime(existing.processed_at)}${existing.times_processed > 1 ? `, ${existing.times_processed} times` : ""}. Running it again re-processes the period: if hours changed, time off posts only the difference.`
            : "Not processed yet."
        }</p>
        <button class="btn" id="run-payroll">${existing ? "Re-run payroll" : "Run payroll"}</button>
      </div>
      <div class="job-result" id="payroll-result" hidden></div>
    </section>

    <section>
      <div class="section-head"><h2>Payroll history</h2><p>Each run published one on_payroll_processed event</p></div>
      <div class="panel table-wrap">
        <table>
          <thead><tr><th>Period</th><th>Processed</th><th class="num">People</th><th class="num">Gross</th><th class="num">Net</th></tr></thead>
          <tbody>
            ${runs
              .slice(0, 8)
              .map(
                (r) => `
              <tr>
                <td>${fmtRange(r.period_start, r.period_end)}${r.is_outdated ? ' <span class="badge pending">Needs re-run</span>' : ""}</td>
                <td>${fmtDateTime(r.processed_at)}<span class="cell-sub">by ${esc(r.processed_by_name)}${r.times_processed > 1 ? `, ${r.times_processed} times` : ""}</span></td>
                <td class="num">${r.lines.length}</td>
                <td class="num">${money(r.total_gross)}</td>
                <td class="num">${money(r.total_net)}</td>
              </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </section>`;

  $("#payroll-period").addEventListener("change", (e) => {
    state.payrollPeriod = e.target.value;
    render();
  });

  // Start from the hours saved in the last run, so edited hours stay edited
  const savedOverrides = existing
    ? Object.fromEntries(existing.lines.filter((l) => l.hours_overridden).map((l) => [l.employee_id, l.hours_worked]))
    : {};

  const body = () => {
    const inputs = $$("[data-hours]", main);
    const overrides = inputs.length ? {} : { ...savedOverrides };
    inputs.forEach((input) => {
      if (input.value !== input.dataset.calculated) overrides[input.dataset.hours] = Number(input.value);
    });
    return { company_id: companyId, period_start: period.period_start, period_end: period.period_end, hours_overrides: overrides };
  };

  async function loadPreview() {
    const lines = await api("/payroll/preview", { method: "POST", body: body() });
    const totals = lines.reduce((t, l) => ({ gross: t.gross + l.gross_pay, net: t.net + l.net_pay }), { gross: 0, net: 0 });
    $("#payroll-preview").innerHTML = `
      <table class="compact">
        <thead><tr>
          <th>Employee</th><th class="num">Scheduled</th><th class="num">Holidays</th><th class="num">Time off</th>
          <th class="num">Hours worked</th><th class="num">Paid hours</th><th class="num">Gross</th><th class="num">Deductions</th><th class="num">Net</th>
        </tr></thead>
        <tbody>
          ${lines
            .map(
              (l) => `
            <tr>
              <td>${esc(l.employee_name)}<span class="cell-sub">${EMPLOYMENT_LABELS[l.employment_type]}, ${l.pay_type === "salary" ? "salaried" : `${money(l.pay_rate)}/h`}</span></td>
              <td class="num">${num(l.scheduled_hours)}h</td>
              <td class="num">${l.holiday_hours ? `−${num(l.holiday_hours)}h` : "–"}</td>
              <td class="num">${
                l.paid_time_off_hours || l.unpaid_time_off_hours
                  ? `−${num(l.paid_time_off_hours + l.unpaid_time_off_hours)}h${l.unpaid_time_off_hours ? `<span class="cell-sub">${num(l.unpaid_time_off_hours)}h unpaid</span>` : ""}`
                  : "–"
              }</td>
              <td class="num"><input type="number" class="hours-input" min="0" step="0.5" data-hours="${esc(l.employee_id)}"
                    data-calculated="${l.hours_overridden ? "" : l.hours_worked}" value="${l.hours_worked}" aria-label="Hours worked for ${esc(l.employee_name)}"></td>
              <td class="num">${num(l.paid_hours)}h${l.pay_type === "salary" ? '<span class="cell-sub">salaried</span>' : ""}</td>
              <td class="num">${money(l.gross_pay)}</td>
              <td class="num">${
                l.benefits || l.taxes
                  ? `−${money(l.benefits + l.taxes)}<span class="cell-sub">benefits ${dollars(l.benefits)}<br>tax ${dollars(l.taxes)}</span>`
                  : `–<span class="cell-sub">contractor</span>`
              }</td>
              <td class="num"><b>${money(l.net_pay)}</b></td>
            </tr>`
            )
            .join("")}
          <tr class="totals"><td>Total</td><td colspan="5"></td><td class="num">${money(totals.gross)}</td><td></td><td class="num"><b>${money(totals.net)}</b></td></tr>
        </tbody>
      </table>
      <div class="hint payroll-notes">
        <p><b>Try it:</b> change someone's hours worked and press Enter or click away. The row recalculates right away. Nothing is saved until you run payroll.</p>
        <p><b>Hours worked</b> = scheduled − holidays − approved time off. Sick leave is earned on these hours (1 hour per 30 worked).</p>
        <p><b>Deductions</b> = benefits (flat per period) + tax (flat 20% demo rate). Contractors have neither. <b>Net</b> = gross − deductions.</p>
        <p><b>Paid hours</b> = hours worked + paid time off + holidays. Holidays and paid time off aren't worked, but they are paid. Contractors aren't paid for holidays, and unpaid time off isn't paid. Salaried people get the same pay every period.</p>
        <p><b>Salaried people</b> are paid the same every period, so changing their hours changes only their sick leave, not their pay. For hourly people it changes both. (This demo has no overtime rate.)</p>
        <p>Re-running a period with different hours is a correction: time off adds only the difference.</p>
      </div>`;
  }

  // Recalculate as soon as an hours box changes (Enter or leaving the box)
  $("#payroll-preview").addEventListener("change", async (e) => {
    if (!e.target.matches("[data-hours]")) return;
    const focusId = e.target.dataset.hours;
    try {
      await loadPreview();
    } catch (err) {
      toast(err.message, "error");
    }
    const again = $(`[data-hours="${focusId}"]`);
    if (again) again.classList.add("edited");
  });

  $("#run-payroll").addEventListener("click", async (e) => {
    try {
      const run = await withBusy(e.currentTarget, () => api("/payroll/runs", { method: "POST", body: body() }));
      state.payrollMessage = run.time_off_effects.length
        ? {
            neutral: false,
            text: `Payroll processed and on_payroll_processed published. Time Off credited: ${run.time_off_effects
              .map((t) => `${nameOf(t.employee_id)} ${signed(t.amount_minutes)}${t.type === "adjustment" ? " (correction)" : ""}`)
              .join(", ")}.`,
          }
        : {
            neutral: true,
            text: "Payroll processed and on_payroll_processed published. Time Off received it, but hours were the same as last time, so nothing changed.",
          };
      toast("Payroll processed");
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  if (state.payrollMessage) {
    const box = $("#payroll-result");
    box.hidden = false;
    box.className = `job-result ${state.payrollMessage.neutral ? "neutral" : ""}`;
    box.textContent = state.payrollMessage.text;
    state.payrollMessage = null;
  }

  try {
    await loadPreview();
  } catch (err) {
    $("#payroll-preview").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

// =========================================================================
// Admin: Audit log
// =========================================================================

const ENTITY_LABELS = {
  policy: "Policy",
  policy_assignment: "Assignment",
  time_off_request: "Request",
  holiday: "Holiday",
};

// Policy settings in the words an admin would use
const SETTING_LABELS = {
  accrual_method: "How it's earned",
  frequency: "Schedule",
  accrue_at: "Accrues",
  rate_amount: "Rate",
  rate_unit: "Unit",
  hours_worked_per_accrual: "Per hours worked",
  max_balance_hours: "Max balance",
  carryover_cap_hours: "Carryover",
  allow_negative: "Negative balance allowed",
  max_negative_hours: "How far below zero",
  proration_rule: "Joining mid-period",
  waiting_period_days: "Waiting period",
  tenure_tiers: "Rate by years at the company",
};

function settingValue(key, value, version) {
  if (value === null || value === undefined) {
    return { max_balance_hours: "No cap", carryover_cap_hours: "Everything carries over" }[key] || "Not set";
  }
  switch (key) {
    case "accrual_method":
    case "frequency":
    case "accrue_at":
    case "proration_rule":
      return LABELS[key][value] || value;
    case "rate_unit":
      return value === "days" ? "Days" : "Hours";
    case "rate_amount":
      return unitText(value, version.rate_unit);
    case "allow_negative":
      return value ? "Yes" : "No";
    case "carryover_cap_hours":
      return value === 0 ? "Use it or lose it" : `Up to ${num(value)}h`;
    case "waiting_period_days":
      return value ? `${value} days` : "None";
    case "tenure_tiers":
      return value.length ? tiersText({ ...version, tenure_tiers: value }) : "Same rate for everyone";
    default:
      return `${num(value)}h`;
  }
}

function changeLine(label, oldText, newText) {
  return `<div class="change"><span>${esc(label)}:</span> <del>${esc(oldText)}</del> <ins>${esc(newText)}</ins></div>`;
}

// Returns HTML describing one audit entry.
function auditDetails(e) {
  const { before, after } = e;
  const hours = (r) => hoursText(r.days.reduce((sum, d) => sum + d.minutes, 0));

  if (e.entity_type === "policy" && e.action === "settings_updated") {
    const lines = Object.keys(SETTING_LABELS)
      .filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]))
      .map((k) => changeLine(SETTING_LABELS[k], settingValue(k, before[k], before), settingValue(k, after[k], after)));
    return `<div class="change-head">Version ${after.version_number}, takes effect ${fmtDate(after.effective_from)}</div>${lines.join("")}`;
  }
  if (e.entity_type === "policy" && e.action === "created") return `Created “${esc(after.policy.name)}”`;
  if (e.entity_type === "policy" && e.action === "year_end_processed") {
    return `Year-end ${after.year}: ${after.expirations} balance${after.expirations === 1 ? "" : "s"} over the carryover cap expired`;
  }

  if (e.entity_type === "time_off_request") {
    if (e.action === "created") {
      return `${esc(nameOf(after.employee_id))} asked for ${hours(after)} off: “${esc(after.reason)}”`;
    }
    if (e.action === "approved" || e.action === "rejected") {
      const note = after.decision_note ? `, note: “${esc(after.decision_note)}”` : "";
      return `${capitalize(e.action)} ${esc(nameOf(after.employee_id))}'s request for ${hours(after)}${note}`;
    }
    if (e.action === "cancelled") {
      const byEmployee = after.cancelled_by === after.employee_id;
      const who = byEmployee ? "the employee" : "an admin";
      const was = before.status === "approved" ? "approved time off" : "a pending request";
      const reason = after.cancel_reason ? ` Reason: “${esc(after.cancel_reason)}”.` : "";
      return `${byEmployee ? "Cancelled" : "Revoked"} by ${who}: ${was} of ${hours(after)} was returned to ${esc(nameOf(after.employee_id))}'s balance.${reason}`;
    }
    if (e.action === "holiday_refund") return `A new holiday on ${fmtDate(after.day)} was refunded on this request`;
  }

  if (e.entity_type === "policy_assignment") {
    if (e.action === "created") return `${esc(nameOf(after.employee_id))} added to a policy from ${fmtDate(after.start_date)}`;
    if (e.action === "ended") return changeLine("Last day on policy", "No end date", fmtDate(after.end_date));
  }
  if (e.entity_type === "holiday") {
    return `${esc(after.name)} on ${fmtDate(after.day)}${after.location ? ` (${esc(after.location)} only)` : ""}`;
  }
  return "";
}

const AUDIT_ACTIONS = {
  created: "created",
  settings_updated: "changed",
  approved: "approved",
  rejected: "rejected",
  cancelled: "cancelled",
  ended: "ended",
  holiday_refund: "refunded for a holiday",
  year_end_processed: "year-end run",
};

async function viewAudit(main) {
  const query = state.auditFilter ? `?entity_type=${state.auditFilter}` : "";
  const entries = (await api(`/audit-log${query}`)).reverse().slice(0, 300);

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Audit log</h1>
        <p>Who changed what and when: policies, assignments, holidays and requests. Balance changes are recorded separately in each employee's balance history.</p>
      </div>
      <label class="field" style="min-width:200px">
        <span>Show</span>
        <select id="audit-filter">
          <option value="">Everything</option>
          ${Object.entries(ENTITY_LABELS)
            .map(([k, label]) => `<option value="${k}" ${state.auditFilter === k ? "selected" : ""}>${label}s</option>`)
            .join("")}
        </select>
      </label>
    </div>
    <div class="panel table-wrap">
      ${
        entries.length
          ? `<table>
              <thead><tr><th>When</th><th>Who</th><th>What</th><th>Details</th></tr></thead>
              <tbody>
                ${entries
                  .map(
                    (e) => `
                  <tr>
                    <td style="white-space:nowrap">${fmtDateTime(e.created_at)}</td>
                    <td style="white-space:nowrap">${esc(nameOf(e.actor_id))}</td>
                    <td>${esc(ENTITY_LABELS[e.entity_type] || e.entity_type)} ${esc(AUDIT_ACTIONS[e.action] || e.action)}<span class="cell-sub">${esc(e.entity_id)}</span></td>
                    <td class="small">${auditDetails(e)}</td>
                  </tr>`
                  )
                  .join("")}
              </tbody>
            </table>`
          : emptyState("No entries", "Changes will be recorded here.")
      }
    </div>`;

  $("#audit-filter").addEventListener("change", (e) => {
    state.auditFilter = e.target.value;
    render();
  });
}

// =========================================================================
// Boot and routing
// =========================================================================

const VIEWS = {
  overview: { label: "My time off", render: viewOverview },
  history: { label: "Balance history", render: viewHistory },
  approvals: { label: "Approvals", render: viewApprovals, admin: true },
  policies: { label: "Policies", render: viewPolicies, admin: true },
  holidays: { label: "Holidays", render: viewHolidays, admin: true },
  people: { label: "People", render: viewPeople, admin: true },
  payroll: { label: "Payroll", render: viewPayroll, admin: true },
  jobs: { label: "Scheduled jobs", render: viewJobs, admin: true },
  audit: { label: "Audit log", render: viewAudit, admin: true },
};

async function renderNav() {
  const item = (key, count = 0) => `
    <button class="nav-item" data-view="${key}" ${state.view === key ? 'aria-current="page"' : ""}>
      <span>${VIEWS[key].label}</span>${count ? `<span class="nav-count">${count}</span>` : ""}
    </button>`;

  let pendingCount = 0;
  if (state.actor.is_admin) {
    try {
      pendingCount = (await api("/time-off-requests?status=pending")).length;
    } catch {
      /* the count is only a hint */
    }
  }

  $("#nav").innerHTML = `
    <div class="nav-group">
      <div class="nav-title">Me</div>
      ${item("overview")}${item("history")}
    </div>
    ${
      state.actor.is_admin
        ? `<div class="nav-group">
            <div class="nav-title">Company</div>
            ${item("approvals", pendingCount)}${item("policies")}${item("holidays")}${item("people")}${item("payroll")}${item("jobs")}${item("audit")}
          </div>`
        : ""
    }`;
  $$(".nav-item").forEach((b) =>
    b.addEventListener("click", () => {
      state.view = b.dataset.view;
      render();
    })
  );
}

async function render() {
  if (VIEWS[state.view].admin && !state.actor.is_admin) state.view = "overview";
  history.replaceState(null, "", `#${state.actor.id}/${state.view}`);

  const me = state.actor;
  $("#actor-meta").textContent = `${me.is_admin ? "Admin" : "Employee"}, ${num(me.hours_per_day)}h days${me.location ? `, ${me.location}` : ""}`;

  renderNav();
  const main = $("#main");
  if (!main.children.length) main.innerHTML = `<p class="loading">Loading…</p>`;
  try {
    await VIEWS[state.view].render(main);
  } catch (err) {
    main.innerHTML = `
      <div class="page-head"><div><h1>Something went wrong</h1><p>${esc(err.message)}</p></div></div>
      <button class="btn secondary" onclick="location.reload()">Reload</button>`;
  }
}

async function boot() {
  try {
    state.employees = await api("/employees");
  } catch (err) {
    $("#main").innerHTML = `<p class="loading">Can't reach the API: ${esc(err.message)}. Is the server running?</p>`;
    return;
  }

  const [hashActor, hashView] = location.hash.slice(1).split("/");
  state.actor = employeeById(hashActor) || state.employees[0];
  state.view = VIEWS[hashView] ? hashView : "overview";

  try {
    const company = await api(`/companies/${state.actor.company_id}`);
    $("#company-name").textContent = company.name;
  } catch {
    /* only a label */
  }

  const select = $("#actor-select");
  select.innerHTML = state.employees
    .map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`)
    .join("");
  select.value = state.actor.id;
  select.addEventListener("change", () => {
    state.actor = employeeById(select.value);
    state.historyPolicyId = null;
    if (!state.actor.is_admin && VIEWS[state.view].admin) state.view = "overview";
    render();
  });

  $("#reset-demo").addEventListener("click", async () => {
    if (!confirm("Reset all data back to the demo starting point?")) return;
    try {
      await api("/demo/reset", { method: "POST" });
      state.employees = await api("/employees");
      toast("Demo data reset");
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  render();
}

boot();
