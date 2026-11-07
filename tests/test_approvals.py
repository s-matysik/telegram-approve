from __future__ import annotations

import asyncio

import pytest
from conftest import ALLOWED, STRANGER

from telegram_approve import (
    Approver,
    Draft,
    DuplicateAction,
    ExecutionFailed,
    HashMismatch,
    MemoryStore,
    NotApproved,
    State,
    content_hash,
)

PAYLOAD = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}


async def ask(approver, action_id="act1", payload=PAYLOAD, **kw):
    return await approver.request(action_id, "Send email", "Hello body", payload, **kw)


async def press(approver, transport, role, user=ALLOWED, n=-1):
    await approver.handle_callback(user, transport.data(role, n), callback_id="cb")


def test_hash_is_canonical():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})
    assert len(content_hash("x")) == 64


async def test_request_sends_message_with_buttons_and_hash(approver, transport):
    ap = await ask(approver, attachments=[("draft.txt", b"x")])
    msg = transport.sent[0]
    assert [b[0] for b in msg["buttons"]] == ["Approve", "Edit", "Reject"]
    assert msg["attachments"] == [("draft.txt", b"x")]
    assert ap.content_hash == content_hash(PAYLOAD)
    assert ap.content_hash[:8] in msg["text"]
    assert ap.state is State.PENDING


async def test_duplicate_action_id_rejected(approver):
    await ask(approver)
    with pytest.raises(DuplicateAction):
        await ask(approver)


async def test_custom_buttons(approver, transport):
    await ask(approver, buttons=("Yes", "No"))
    assert [b[0] for b in transport.sent[0]["buttons"]] == ["Yes", "No"]
    assert transport.data("reject")


async def test_approve_records_audit_and_wakes_waiter(approver, transport):
    ap = await ask(approver)
    waiter = asyncio.create_task(ap.wait())
    await asyncio.sleep(0)
    assert not waiter.done()
    await press(approver, transport, "approve")
    await asyncio.wait_for(waiter, 1)
    assert ap.state is State.APPROVED
    assert ap.decided_by == str(ALLOWED)
    assert ap.decided_at
    assert transport.edits and "approved" in transport.edits[-1][2]


async def test_on_decision_callback(approver, transport):
    seen = []
    ap = await ask(approver, on_decision=lambda a: seen.append(a.state))
    await press(approver, transport, "reject")
    assert seen == [State.REJECTED]
    assert ap.state is State.REJECTED


async def test_stranger_cannot_decide_via_callback(approver, transport):
    ap = await ask(approver)
    await press(approver, transport, "approve", user=STRANGER)
    assert ap.state is State.PENDING
    assert ap.decided_by is None
    assert transport.answers[-1][1] == "Not allowed."
    assert approver.store.get("act1").state == "pending"


async def test_stranger_text_is_ignored(store, transport):
    ap_ = Approver(None, [ALLOWED], store, transport=transport, on_edit=lambda a, t: 1 / 0)
    ap = await ask(ap_)
    await ap_.handle_callback(ALLOWED, transport.data("edit"))
    await ap_.handle_text(STRANGER, "corrections")  # would crash on_edit if honored
    assert ap.state is State.PENDING


async def test_forged_callback_with_wrong_hash_is_refused(approver, transport):
    ap = await ask(approver)
    await approver.handle_callback(ALLOWED, "ta:approve:act1:deadbeef", callback_id="c")
    assert ap.state is State.PENDING
    await approver.handle_callback(ALLOWED, "garbage", callback_id="c")
    assert ap.state is State.PENDING


async def test_first_decision_wins(approver, transport):
    ap = await ask(approver)
    await press(approver, transport, "approve")
    await press(approver, transport, "reject")
    assert ap.state is State.APPROVED
    assert "Already" in transport.answers[-1][1]


async def test_execute_requires_approval(approver, transport):
    await ask(approver)
    with pytest.raises(NotApproved):
        await approver.execute("act1", PAYLOAD, lambda p: "sent")
    await press(approver, transport, "reject")
    with pytest.raises(NotApproved):
        await approver.execute("act1", PAYLOAD, lambda p: "sent")


async def test_execute_refuses_changed_payload(approver, transport):
    await ask(approver)
    await press(approver, transport, "approve")
    calls = []
    with pytest.raises(HashMismatch):
        await approver.execute("act1", {**PAYLOAD, "to": "evil@example.com"}, calls.append)
    assert calls == []
    assert approver.get("act1").state is State.APPROVED  # still usable with the right payload
    assert await approver.execute("act1", PAYLOAD, lambda p: "sent") == "sent"


async def test_execute_at_most_once(approver, transport):
    await ask(approver)
    await press(approver, transport, "approve")
    calls = []
    await approver.execute("act1", PAYLOAD, calls.append)
    with pytest.raises(NotApproved):
        await approver.execute("act1", PAYLOAD, calls.append)
    assert len(calls) == 1
    assert approver.get("act1").state is State.DONE


async def test_concurrent_execute_runs_once(approver, transport):
    await ask(approver)
    await press(approver, transport, "approve")
    calls = []

    async def slow(p):
        calls.append(1)
        await asyncio.sleep(0.01)

    res = await asyncio.gather(
        *[approver.execute("act1", PAYLOAD, slow) for _ in range(5)], return_exceptions=True
    )
    assert len(calls) == 1
    assert sum(isinstance(r, NotApproved) for r in res) == 4


async def test_failed_execution_is_not_retried(approver, transport):
    await ask(approver)
    await press(approver, transport, "approve")

    def boom(p):
        raise RuntimeError("smtp down")

    with pytest.raises(ExecutionFailed):
        await approver.execute("act1", PAYLOAD, boom)
    rec = approver.store.get("act1")
    assert rec.state == "failed" and "smtp down" in rec.error
    with pytest.raises(NotApproved):
        await approver.execute("act1", PAYLOAD, lambda p: "x")


async def test_crash_in_executing_is_never_auto_retried(store, transport):
    first = Approver(None, [ALLOWED], store, transport=transport)
    await ask(first)
    await press(first, transport, "approve")
    # simulate a crash: state moved to executing, process died before done/failed
    assert store.transition("act1", ["approved"], "executing")
    restarted = Approver(None, [ALLOWED], store, transport=transport)
    with pytest.raises(NotApproved):
        await restarted.execute("act1", PAYLOAD, lambda p: "sent twice")
    assert [r.action_id for r in restarted.stuck()] == ["act1"]
    # only explicit manual resolution releases it
    restarted.resolve("act1", "retry", by="ops", note="checked outbox, nothing sent")
    assert restarted.get("act1").state is State.APPROVED
    assert await restarted.execute("act1", PAYLOAD, lambda p: "sent") == "sent"


async def test_resolve_only_from_executing(approver, transport):
    await ask(approver)
    with pytest.raises(NotApproved):
        approver.resolve("act1", "done", by="ops")
    with pytest.raises(ValueError):
        approver.resolve("act1", "bogus", by="ops")


async def test_wait_timeout_expires_and_blocks_later_approval(approver, transport):
    ap = await ask(approver)
    await ap.wait(timeout=0.01)
    assert ap.state is State.EXPIRED
    await press(approver, transport, "approve")
    assert ap.state is State.EXPIRED
    with pytest.raises(NotApproved):
        await approver.execute("act1", PAYLOAD, lambda p: "x")


async def test_expires_in_blocks_late_callback(approver, transport):
    ap = await ask(approver, expires_in=-1)
    await press(approver, transport, "approve")
    assert ap.state is State.EXPIRED
    assert ap.state is not State.APPROVED


async def test_expire_overdue_sweep(approver):
    await ask(approver, "a", expires_in=-1)
    await ask(approver, "b", payload={"x": 1})
    assert await approver.expire_overdue() == ["a"]
    assert approver.get("b").state is State.PENDING


async def test_edit_flow_supersedes_with_new_hash(store, transport):
    async def on_edit(approval, notes):
        return Draft("Send email v2", "Hello again", {**approval.payload, "body": notes})

    ap_ = Approver(None, [ALLOWED], store, transport=transport, on_edit=on_edit)
    old = await ask(ap_)
    waiter = asyncio.create_task(old.wait())
    await ap_.handle_callback(ALLOWED, transport.data("edit"), callback_id="c", chat_id=ALLOWED)
    assert transport.replies  # asked for corrections
    await ap_.handle_text(ALLOWED, "Shorter please")
    await waiter
    assert old.state is State.SUPERSEDED
    new = old.successor
    assert new is not None and new.state is State.PENDING
    assert new.content_hash != old.content_hash
    assert new.record.supersedes == "act1" and old.record.superseded_by == new.action_id
    # the old approval cannot be used any more, even if it had been pressed late
    await ap_.handle_callback(ALLOWED, transport.data("approve", 0), callback_id="c")
    assert old.state is State.SUPERSEDED
    with pytest.raises(NotApproved):
        await ap_.execute("act1", PAYLOAD, lambda p: "x")
    # the new draft needs its own approval, and only its own payload runs
    await ap_.handle_callback(ALLOWED, transport.data("approve"), callback_id="c")
    with pytest.raises(HashMismatch):
        await ap_.execute(new.action_id, PAYLOAD, lambda p: "x")
    new_payload = {**PAYLOAD, "body": "Shorter please"}
    assert await ap_.execute(new.action_id, new_payload, lambda p: p["body"]) == "Shorter please"


async def test_edit_without_handler_marks_revise_with_notes(approver, transport):
    ap = await ask(approver)
    await approver.handle_callback(ALLOWED, transport.data("edit"), callback_id="c")
    await approver.handle_text(ALLOWED, "make it formal")
    assert ap.state is State.REVISE
    assert ap.note == "make it formal"
    assert ap.decided_by == str(ALLOWED)


async def test_invalid_action_id_and_buttons(approver):
    with pytest.raises(ValueError):
        await approver.request("bad id:", "t", "b", {})
    with pytest.raises(ValueError):
        await approver.request("ok", "t", "b", {}, buttons=("a", "b", "c", "d"))


def test_allowed_users_required():
    with pytest.raises(ValueError):
        Approver(None, [], MemoryStore(), transport=object())  # type: ignore[arg-type]


async def test_sqlite_persists_across_instances(tmp_path, transport):
    from telegram_approve import SQLiteStore

    path = str(tmp_path / "p.sqlite3")
    s1 = SQLiteStore(path)
    a1 = Approver(None, [ALLOWED], s1, transport=transport)
    await ask(a1)
    await press(a1, transport, "approve")
    s1.close()
    s2 = SQLiteStore(path)
    a2 = Approver(None, [ALLOWED], s2, transport=transport)
    assert a2.get("act1").state is State.APPROVED
    assert await a2.execute("act1", PAYLOAD, lambda p: 1) == 1
    s2.close()
