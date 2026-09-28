"""Simulated Company Service (an existing service, per the prompt).

Returns company information. The Time Off Service uses it to check that a
company exists, which locations it has (for location-specific holidays), and
its pay schedule.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.domain.errors import NotFoundError
from app.domain.models import Company


class CompanyService:
    def __init__(self, companies: Optional[Iterable[Company]] = None) -> None:
        self._companies: dict[str, Company] = {c.id: c for c in companies or []}

    def get_company(self, company_id: str) -> Company:
        company = self._companies.get(company_id)
        if company is None:
            raise NotFoundError(f"Company {company_id} not found")
        return company

    def list_companies(self) -> list[Company]:
        return list(self._companies.values())
