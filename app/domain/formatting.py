"""Small helpers so ledger notes read like "Sep 1, 2026" instead of "2026-09-01"."""

from __future__ import annotations

from datetime import date


def fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def fmt_range(start: date, end: date) -> str:
    if start == end:
        return fmt_date(start)
    if start.year == end.year:
        return f"{start:%b} {start.day} - {fmt_date(end)}"
    return f"{fmt_date(start)} - {fmt_date(end)}"


def fmt_hours(minutes: float) -> str:
    return f"{round(minutes / 60, 2):g}h"
