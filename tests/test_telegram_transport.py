"""Wiring check for the python-telegram-bot transport (no network)."""

from __future__ import annotations

from types import SimpleNamespace

from conftest import ALLOWED, STRANGER

from telegram_approve import Approver, MemoryStore, State, TelegramTransport


async def test_ptb_handlers_route_to_core_and_enforce_whitelist():
    transport = TelegramTransport("123456:TEST_TOKEN_NOT_REAL")
    approver = Approver(None, [ALLOWED], MemoryStore(), transport=transport, chat_ids=[1])
    sent = []

    async def fake_send(chat_id, text, buttons, attachments):
        sent.append(buttons)
        return 5

    transport.send = fake_send  # type: ignore[method-assign]
    ap = await approver.request("a1", "t", "b", {"x": 1})
    data = sent[0][0][1]
    answers = []

    async def fake_answer(cb, text):
        answers.append(text)

    transport.answer = fake_answer  # type: ignore[method-assign]

    def update(uid):
        q = SimpleNamespace(
            id="q", data=data, from_user=SimpleNamespace(id=uid),
            message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=5),
        )  # fmt: skip
        return SimpleNamespace(callback_query=q)

    async def fake_edit(*a):
        pass

    transport.edit = fake_edit  # type: ignore[method-assign]
    await transport._on_callback(update(STRANGER), None)
    assert ap.state is State.PENDING
    await transport._on_callback(update(ALLOWED), None)
    assert ap.state is State.APPROVED
