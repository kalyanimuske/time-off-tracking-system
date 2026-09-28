"""Working days and holidays."""

from __future__ import annotations

from datetime import date

from app.domain.models import Employee
from app.storage.memory import InMemoryStore


class WorkCalendar:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    def holidays_for(self, employee: Employee, start: date, end: date) -> set[date]:
        """Company holidays that apply to this employee between start and end.
        A holiday with a location (e.g. "CA") only applies to employees there."""
        return {
            h.day
            for h in self.store.holidays.values()
            if h.company_id == employee.company_id
            and start <= h.day <= end
            and (h.location is None or h.location == employee.location)
        }

    @staticmethod
    def is_working_day(employee: Employee, day: date, holidays: set[date]) -> bool:
        return day.weekday() in employee.work_weekdays and day not in holidays
