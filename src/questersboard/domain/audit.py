from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditLog
from .actor import Actor


def record(s: Session, actor: Actor, action: str, target_type: str, target_id, **detail) -> None:
    s.add(AuditLog(actor_discord_id=actor.discord_id, source=actor.source, action=action,
                   target_type=target_type, target_id=str(target_id), detail=detail))
