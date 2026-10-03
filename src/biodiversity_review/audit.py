"""审计日志：状态流中发生的每一次动作都追加、永不修改或删除。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_seq = 0


def _next_seq() -> int:
    # 进程内单调序号；与时间戳共同保证事件顺序可重放。
    global _seq
    _seq += 1
    return _seq


def reset_sequence() -> None:
    """仅供测试：重置事件序号。"""
    global _seq
    _seq = 0


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "detail": self.detail,
        }


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            seq=_next_seq(),
            at=datetime.now(timezone.utc),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
        )
        self._events.append(event)
        return event

    def for_entity(self, entity_type: str, entity_id: str) -> list[AuditEvent]:
        return [
            e
            for e in self._events
            if e.entity_type == entity_type and e.entity_id == entity_id
        ]

    def all_events(self) -> list[AuditEvent]:
        return list(self._events)

    def dump_jsonl(self) -> str:
        return "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in self._events)
