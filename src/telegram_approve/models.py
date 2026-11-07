"""Records, states and errors."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class State(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    REVISE = "revise"  # edit requested but no on_edit handler produced a new draft


#: States in which nothing more can happen to the request without manual action.
TERMINAL = frozenset(
    {State.DONE, State.FAILED, State.REJECTED, State.EXPIRED, State.SUPERSEDED, State.REVISE}
)
#: States that end a `wait()`.
DECIDED = frozenset(
    {State.APPROVED, State.REJECTED, State.EXPIRED, State.SUPERSEDED, State.REVISE}
    | {State.EXECUTING, State.DONE, State.FAILED}
)


class ApprovalError(Exception):
    """Base class for library errors."""


class DuplicateAction(ApprovalError):
    """An action_id was requested twice."""


class UnknownAction(ApprovalError):
    """No request with this action_id."""


class NotApproved(ApprovalError):
    """The action is not in the 'approved' state (never approved, rejected, already run...)."""


class HashMismatch(ApprovalError):
    """The payload differs from the one that was approved."""


class ExecutionFailed(ApprovalError):
    """The executed function raised. The action is marked 'failed' and is not retried."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: Any) -> str:
    """sha256 hex digest of the canonical JSON form of `payload`."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class Record:
    """One approval request as persisted by a store."""

    action_id: str
    content_hash: str
    title: str
    payload_json: str
    state: str = State.PENDING.value
    requested_at: str = field(default_factory=now_iso)
    expires_at: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    executed_at: str | None = None
    finished_at: str | None = None
    result_json: str | None = None
    error: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    messages_json: str = "[]"  # [[chat_id, message_id], ...]
    resolution_note: str | None = None

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @property
    def payload(self) -> Any:
        return json.loads(self.payload_json)

    @property
    def messages(self) -> list[tuple[int, int]]:
        return [(int(c), int(m)) for c, m in json.loads(self.messages_json)]
