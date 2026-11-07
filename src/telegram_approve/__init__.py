"""telegram-approve: human-in-the-loop approvals for AI agents via Telegram."""

from .approver import Approval, Approver, Draft
from .models import (
    ApprovalError,
    DuplicateAction,
    ExecutionFailed,
    HashMismatch,
    NotApproved,
    Record,
    State,
    UnknownAction,
    content_hash,
)
from .store import MemoryStore, SQLiteStore, Store
from .transport import TelegramTransport, Transport

__all__ = [
    "Approval", "ApprovalError", "Approver", "Draft", "DuplicateAction", "ExecutionFailed",
    "HashMismatch", "MemoryStore", "NotApproved", "Record", "SQLiteStore", "State", "Store",
    "TelegramTransport", "Transport", "UnknownAction", "content_hash",
]  # fmt: skip

__version__ = "0.1.0"
