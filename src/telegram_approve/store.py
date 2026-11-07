"""Store interface plus in-memory and SQLite implementations.

All state changes go through `transition`, an atomic compare-and-set on the state.
That single primitive is what makes decisions and executions race-free.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import asdict, replace
from typing import Any, Protocol, runtime_checkable

from .models import DuplicateAction, Record

_UPDATABLE = {n for n in Record.field_names() if n not in ("action_id", "content_hash")}


@runtime_checkable
class Store(Protocol):
    def create(self, record: Record) -> None:
        """Insert a record. Raise DuplicateAction if the action_id exists."""

    def get(self, action_id: str) -> Record | None: ...

    def transition(
        self, action_id: str, expected: Iterable[str], new_state: str, **fields: Any
    ) -> bool:
        """Atomically set state (and fields) if the current state is in `expected`."""

    def update(self, action_id: str, **fields: Any) -> None:
        """Set non-state fields (e.g. message refs) without changing the state."""

    def list_by_state(self, *states: str) -> list[Record]: ...


def _check_fields(fields: dict[str, Any]) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")


class MemoryStore:
    """Process-local store, meant for tests."""

    def __init__(self) -> None:
        self._rows: dict[str, Record] = {}
        self._lock = threading.Lock()

    def create(self, record: Record) -> None:
        with self._lock:
            if record.action_id in self._rows:
                raise DuplicateAction(record.action_id)
            self._rows[record.action_id] = replace(record)

    def get(self, action_id: str) -> Record | None:
        with self._lock:
            row = self._rows.get(action_id)
            return replace(row) if row else None

    def transition(
        self, action_id: str, expected: Iterable[str], new_state: str, **fields: Any
    ) -> bool:
        _check_fields(fields)
        with self._lock:
            row = self._rows.get(action_id)
            if row is None or row.state not in set(expected):
                return False
            self._rows[action_id] = replace(row, state=new_state, **fields)
            return True

    def update(self, action_id: str, **fields: Any) -> None:
        _check_fields(fields)
        with self._lock:
            self._rows[action_id] = replace(self._rows[action_id], **fields)

    def list_by_state(self, *states: str) -> list[Record]:
        with self._lock:
            return [replace(r) for r in self._rows.values() if r.state in states]


class SQLiteStore:
    """Durable store on stdlib sqlite3 (one table, WAL mode)."""

    def __init__(self, path: str = "approvals.sqlite3") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        cols = ", ".join(
            f"{n} TEXT PRIMARY KEY" if n == "action_id" else f"{n} TEXT"
            for n in Record.field_names()
        )
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS approvals ({cols})")
        self._conn.execute("CREATE INDEX IF NOT EXISTS approvals_state ON approvals(state)")

    def close(self) -> None:
        self._conn.close()

    def create(self, record: Record) -> None:
        data = asdict(record)
        names = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._lock:
            try:
                self._conn.execute(
                    f"INSERT INTO approvals ({names}) VALUES ({marks})", tuple(data.values())
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateAction(record.action_id) from exc

    def get(self, action_id: str) -> Record | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE action_id = ?", (action_id,)
            ).fetchone()
        return Record(**dict(row)) if row else None

    def transition(
        self, action_id: str, expected: Iterable[str], new_state: str, **fields: Any
    ) -> bool:
        _check_fields(fields)
        exp = tuple(expected)
        sets = ", ".join(["state = ?", *(f"{k} = ?" for k in fields)])
        marks = ", ".join("?" for _ in exp)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE approvals SET {sets} WHERE action_id = ? AND state IN ({marks})",
                (new_state, *fields.values(), action_id, *exp),
            )
            return cur.rowcount == 1

    def update(self, action_id: str, **fields: Any) -> None:
        _check_fields(fields)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE approvals SET {sets} WHERE action_id = ?", (*fields.values(), action_id)
            )

    def list_by_state(self, *states: str) -> list[Record]:
        marks = ", ".join("?" for _ in states)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM approvals WHERE state IN ({marks}) ORDER BY requested_at", states
            ).fetchall()
        return [Record(**dict(r)) for r in rows]
