"""The Approver: request, decide, execute at most once."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import (
    DECIDED,
    ExecutionFailed,
    HashMismatch,
    NotApproved,
    Record,
    State,
    UnknownAction,
    canonical_json,
    content_hash,
    now_iso,
)
from .store import Store
from .transport import Attachment, Transport

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_.~-]{1,40}$")
_MAX_TEXT = 3500
_ROLES = ("approve", "edit", "reject")


@dataclass
class Draft:
    """A revised draft produced by the `on_edit` handler."""

    title: str
    body: str
    payload: Any
    attachments: list[Attachment] | None = None


OnEdit = Callable[["Approval", str], "Draft | Awaitable[Draft]"]
OnDecision = Callable[["Approval"], "Awaitable[None] | None"]


class Approval:
    """Handle for one request. `state` and audit fields are refreshed on every decision."""

    def __init__(self, approver: Approver, record: Record, on_decision: OnDecision | None):
        self._approver = approver
        self._on_decision = on_decision
        self._event = asyncio.Event()
        self.successor: Approval | None = None
        self._set(record)

    def _set(self, record: Record) -> None:
        self.record = record
        if State(record.state) in DECIDED:
            self._event.set()

    @property
    def action_id(self) -> str:
        return self.record.action_id

    @property
    def content_hash(self) -> str:
        return self.record.content_hash

    @property
    def state(self) -> State:
        return State(self.record.state)

    @property
    def decided_by(self) -> str | None:
        return self.record.decided_by

    @property
    def decided_at(self) -> str | None:
        return self.record.decided_at

    @property
    def note(self) -> str | None:
        return self.record.decision_note

    @property
    def payload(self) -> Any:
        return self.record.payload

    async def wait(self, timeout: float | None = None) -> Approval:
        """Wait for a decision. On timeout the request expires (pending -> expired)."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            await self._approver._expire(self)
        return self


class Approver:
    def __init__(
        self,
        bot_token: str | None,
        allowed_user_ids: Iterable[int],
        store: Store,
        *,
        transport: Transport | None = None,
        chat_ids: Iterable[int] | None = None,
        on_edit: OnEdit | None = None,
    ) -> None:
        self.allowed = frozenset(int(u) for u in allowed_user_ids)
        if not self.allowed:
            raise ValueError("allowed_user_ids must not be empty")
        self.store = store
        self.chat_ids = list(chat_ids) if chat_ids is not None else sorted(self.allowed)
        self.on_edit = on_edit
        if transport is None:
            if not bot_token:
                raise ValueError("bot_token or transport is required")
            from .transport import TelegramTransport

            transport = TelegramTransport(bot_token)
        self.transport = transport
        self.transport.bind(self)
        self._live: dict[str, Approval] = {}
        self._awaiting_edit: dict[int, str] = {}
        self._started = False

    # lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        await self.transport.start()
        self._started = True

    async def stop(self) -> None:
        """Stop the transport. A no-op if `start()` was never called (or already stopped)."""
        if not self._started:
            return
        self._started = False
        await self.transport.stop()

    async def __aenter__(self) -> Approver:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # requests --------------------------------------------------------------
    async def request(
        self,
        action_id: str,
        title: str,
        body: str,
        payload: Any,
        attachments: Sequence[Attachment] | None = None,
        buttons: Sequence[str] | Mapping[str, str] = ("Approve", "Edit", "Reject"),
        *,
        expires_in: float | None = None,
        on_decision: OnDecision | None = None,
        _supersedes: str | None = None,
    ) -> Approval:
        if not _ID_RE.match(action_id):
            raise ValueError("action_id must match [A-Za-z0-9_.~-]{1,40}")
        labels = _button_labels(buttons)
        expires_at = None
        if expires_in is not None:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        record = Record(
            action_id=action_id,
            content_hash=content_hash(payload),
            title=title,
            payload_json=canonical_json(payload),
            expires_at=expires_at,
            supersedes=_supersedes,
        )
        self.store.create(record)
        approval = Approval(self, record, on_decision)
        self._live[action_id] = approval
        text = _render(title, body, record.content_hash)
        h8 = record.content_hash[:8]
        cb = [(label, f"ta:{role}:{action_id}:{h8}") for role, label in labels.items()]
        refs: list[list[int]] = []
        for chat_id in self.chat_ids:
            mid = await self.transport.send(chat_id, text, cb, list(attachments or []))
            refs.append([chat_id, mid])
        self.store.update(action_id, messages_json=json.dumps(refs))
        approval._set(self.store.get(action_id))  # type: ignore[arg-type]
        return approval

    async def revise(self, old: Approval, draft: Draft, by: str | None = None) -> Approval:
        """Supersede `old` with a new request for the edited draft (new hash, new approval).

        `old` must be pending or already in 'revise'. It is first claimed as 'revise' (so
        no parallel decision can land on it) and becomes 'superseded' only after the new
        request was sent successfully. If sending fails, `old` stays in 'revise', the
        unsent new request is expired, and the error is raised.
        """
        current = self.store.get(old.action_id)
        if current is None:
            raise UnknownAction(old.action_id)
        if current.state == State.PENDING.value:
            ok = self.store.transition(
                old.action_id, [State.PENDING.value], State.REVISE.value,
                decided_by=by, decided_at=now_iso(),
            )  # fmt: skip
            if not ok:
                raise NotApproved(f"{old.action_id} is no longer pending")
        elif current.state != State.REVISE.value:
            raise NotApproved(f"{old.action_id} is '{current.state}', not pending")
        new_id = _next_revision_id(old.action_id)
        while self.store.get(new_id) is not None:  # skip ids left by failed sends
            new_id = _next_revision_id(new_id)
        try:
            new = await self.request(
                new_id, draft.title, draft.body, draft.payload, draft.attachments,
                _supersedes=old.action_id, on_decision=old._on_decision,
            )  # fmt: skip
        except Exception:
            if self.store.get(new_id) is not None:
                self.store.transition(
                    new_id, [State.PENDING.value], State.EXPIRED.value,
                    decided_by="system:send_failed", decided_at=now_iso(),
                )  # fmt: skip
            await self._finish(old, "edit requested, the revised draft could not be sent")
            raise
        self.store.transition(
            old.action_id, [State.REVISE.value], State.SUPERSEDED.value, superseded_by=new_id
        )
        old.successor = new
        await self._finish(old, "superseded by a new draft")
        return new

    def get(self, action_id: str) -> Approval:
        live = self._live.get(action_id)
        record = self.store.get(action_id)
        if record is None:
            raise UnknownAction(action_id)
        if live is None:
            live = Approval(self, record, None)
            self._live[action_id] = live
        else:
            live._set(record)
        return live

    # execution -------------------------------------------------------------
    async def execute(
        self, action_id: str, payload: Any, fn: Callable[[Any], Any | Awaitable[Any]]
    ) -> Any:
        """Run `fn(payload)` at most once, only if `payload` is exactly what was approved."""
        record = self.store.get(action_id)
        if record is None:
            raise UnknownAction(action_id)
        if content_hash(payload) != record.content_hash:
            raise HashMismatch(f"{action_id}: payload differs from the approved content")
        ok = self.store.transition(
            action_id, [State.APPROVED.value], State.EXECUTING.value, executed_at=now_iso()
        )
        if not ok:
            current = self.store.get(action_id)
            raise NotApproved(f"{action_id} is '{current.state if current else '?'}', not approved")
        try:
            result = fn(payload)
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            self.store.transition(
                action_id, [State.EXECUTING.value], State.FAILED.value,
                finished_at=now_iso(), error=f"{type(exc).__name__}: {exc}",
            )  # fmt: skip
            if isinstance(exc, Exception):
                raise ExecutionFailed(f"{action_id}: {exc}") from exc
            raise
        self.store.transition(
            action_id, [State.EXECUTING.value], State.DONE.value,
            finished_at=now_iso(), result_json=_dump_result(result),
        )  # fmt: skip
        return result

    def stuck(self) -> list[Record]:
        """Actions left in 'executing' (crash mid-send). They are never retried automatically."""
        return self.store.list_by_state(State.EXECUTING.value)

    def resolve(self, action_id: str, outcome: str, by: str, note: str = "") -> None:
        """Manually settle an 'executing' action: outcome is 'done', 'failed' or 'retry'.

        'retry' puts it back to 'approved' (the operator checked the side effect did not happen).
        """
        target = {"done": State.DONE, "failed": State.FAILED, "retry": State.APPROVED}.get(outcome)
        if target is None:
            raise ValueError("outcome must be 'done', 'failed' or 'retry'")
        ok = self.store.transition(
            action_id, [State.EXECUTING.value], target.value,
            resolution_note=f"{by}: {note}".strip(": "), finished_at=now_iso(),
        )  # fmt: skip
        if not ok:
            raise NotApproved(f"{action_id} is not in 'executing'")

    async def expire_overdue(self) -> list[str]:
        """Expire pending requests whose expires_at has passed."""
        out = []
        for rec in self.store.list_by_state(State.PENDING.value):
            if _is_overdue(rec):
                if self._expire_record(rec.action_id):
                    out.append(rec.action_id)
                    await self._finish(self.get(rec.action_id), "expired")
        return out

    # decisions (transport-independent core) --------------------------------
    async def handle_callback(
        self,
        user_id: int,
        data: str,
        *,
        callback_id: str | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> None:
        if int(user_id) not in self.allowed:
            await self._answer(callback_id, "Not allowed.")
            return
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "ta" or parts[1] not in _ROLES:
            await self._answer(callback_id, "Unknown action.")
            return
        _, role, action_id, h8 = parts
        record = self.store.get(action_id)
        if record is None or not record.content_hash.startswith(h8):
            await self._answer(callback_id, "Unknown or changed request.")
            return
        if _is_overdue(record) and record.state == State.PENDING.value:
            if self._expire_record(action_id):
                await self._finish(self.get(action_id), "expired")
        approval = self.get(action_id)
        if approval.state is not State.PENDING:
            await self._answer(callback_id, f"Already {approval.state.value}.")
            return
        who = str(user_id)
        if role == "edit":
            self._awaiting_edit[int(user_id)] = action_id
            await self._answer(callback_id, "Send your corrections as the next message.")
            await self.transport.reply(
                chat_id if chat_id is not None else int(user_id),
                f"Reply with your corrections for {action_id}.",
            )
            return
        new = State.APPROVED if role == "approve" else State.REJECTED
        ok = self.store.transition(
            action_id, [State.PENDING.value], new.value, decided_by=who, decided_at=now_iso()
        )
        if not ok:
            await self._answer(callback_id, "Already decided.")
            return
        await self._answer(callback_id, new.value.capitalize() + ".")
        await self._finish(self.get(action_id), f"{new.value} by {who}")

    async def handle_text(self, user_id: int, text: str, *, chat_id: int | None = None) -> None:
        """Corrections following an Edit press. Ignored unless the user is allowed and editing."""
        if int(user_id) not in self.allowed:
            return
        action_id = self._awaiting_edit.pop(int(user_id), None)
        if action_id is None:
            return
        approval = self.get(action_id)
        if approval.state is not State.PENDING:
            return
        who = str(user_id)
        if self.on_edit is None:
            ok = self.store.transition(
                action_id, [State.PENDING.value], State.REVISE.value,
                decided_by=who, decided_at=now_iso(), decision_note=text,
            )  # fmt: skip
            if ok:
                await self._finish(self.get(action_id), "edit requested")
            return
        try:
            draft = self.on_edit(approval, text)
            if inspect.isawaitable(draft):
                draft = await draft
            await self.revise(approval, draft, by=who)
        except NotApproved:
            log.info("corrections for %s dropped: decided in parallel", action_id)
            await self._safe_reply(chat_id, user_id, f"{action_id} was already decided.")
        except Exception:  # noqa: BLE001 - never let a handler error escape into the bot
            log.exception("applying corrections for %s failed", action_id)
            await self._safe_reply(
                chat_id, user_id, f"Could not apply your corrections for {action_id}."
            )

    async def _safe_reply(self, chat_id: int | None, user_id: int, text: str) -> None:
        try:
            await self.transport.reply(chat_id if chat_id is not None else int(user_id), text)
        except Exception:  # noqa: BLE001 - best effort notice
            log.exception("could not send a reply to %s", user_id)

    # internals -------------------------------------------------------------
    def _expire_record(self, action_id: str) -> bool:
        return self.store.transition(
            action_id, [State.PENDING.value], State.EXPIRED.value, decided_at=now_iso(),
            decided_by="system:timeout",
        )  # fmt: skip

    async def _expire(self, approval: Approval) -> None:
        if self._expire_record(approval.action_id):
            await self._finish(self.get(approval.action_id), "expired")
        else:
            approval._set(self.store.get(approval.action_id))  # type: ignore[arg-type]

    async def _answer(self, callback_id: str | None, text: str) -> None:
        if callback_id is not None:
            await self.transport.answer(callback_id, text)

    async def _finish(self, approval: Approval, status: str) -> None:
        """Refresh the handle, update the chat messages, wake waiters, fire on_decision."""
        record = self.store.get(approval.action_id)
        assert record is not None
        approval._set(record)
        text = _render(record.title, "", record.content_hash) + f"\n\nStatus: {status}"
        for chat_id, message_id in record.messages:
            try:
                await self.transport.edit(chat_id, message_id, text)
            except Exception:  # noqa: BLE001 - cosmetic, the decision is already stored
                pass
        cb = approval._on_decision
        if cb is not None:
            res = cb(approval)
            if inspect.isawaitable(res):
                await res


def _button_labels(buttons: Sequence[str] | Mapping[str, str]) -> dict[str, str]:
    if isinstance(buttons, Mapping):
        out = {r: buttons[r] for r in _ROLES if r in buttons}
        if set(buttons) - set(_ROLES):
            raise ValueError(f"button roles must be among {_ROLES}")
    else:
        labels = list(buttons)
        roles = {1: ("approve",), 2: ("approve", "reject"), 3: _ROLES}.get(len(labels))
        if roles is None:
            raise ValueError("buttons must have 1 to 3 labels")
        out = dict(zip(roles, labels, strict=False))
    if "approve" not in out:
        raise ValueError("an approve button is required")
    return out


def _render(title: str, body: str, digest: str) -> str:
    if len(body) > _MAX_TEXT:
        body = body[:_MAX_TEXT] + "\n[truncated]"
    parts = [title]
    if body:
        parts.append(body)
    parts.append(f"hash {digest[:8]}")
    return "\n\n".join(parts)


def _next_revision_id(action_id: str) -> str:
    m = re.match(r"^(.*)~r(\d+)$", action_id)
    base, n = (m.group(1), int(m.group(2)) + 1) if m else (action_id, 2)
    return f"{base[:32]}~r{n}"


def _is_overdue(rec: Record) -> bool:
    return bool(rec.expires_at) and rec.expires_at < now_iso()  # type: ignore[operator]


def _dump_result(result: Any) -> str | None:
    try:
        return json.dumps(result)
    except (TypeError, ValueError):
        return json.dumps(repr(result))
