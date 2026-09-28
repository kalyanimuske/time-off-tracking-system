"""Helpers for writing to the audit log."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from app.domain.models import AuditLogEntry, new_id
from app.storage.memory import InMemoryStore


def snapshot(obj: Any) -> dict[str, Any]:
    """Turn a dataclass into a plain dict so we can store before/after states."""
    if is_dataclass(obj):
        return asdict(obj)
    return dict(obj)


def record_audit(
    store: InMemoryStore,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    actor_id: str,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
) -> AuditLogEntry:
    entry = AuditLogEntry(
        id=new_id("aud"),
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        before=before,
        after=after,
    )
    store.audit_log.append(entry)
    return entry
