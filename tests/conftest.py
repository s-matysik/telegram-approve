from __future__ import annotations

import pytest

from telegram_approve import Approver, MemoryStore, SQLiteStore

ALLOWED = 1001
STRANGER = 9999


class FakeTransport:
    """Records everything instead of talking to Telegram."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple[int, int, str]] = []
        self.replies: list[tuple[int, str]] = []
        self.answers: list[tuple[str, str]] = []
        self._mid = 100
        self.started = False

    def bind(self, approver) -> None:
        self.approver = approver

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send(self, chat_id, text, buttons, attachments) -> int:
        self._mid += 1
        self.sent.append(
            {"chat_id": chat_id, "text": text, "buttons": buttons, "attachments": attachments}
        )
        return self._mid

    async def edit(self, chat_id, message_id, text) -> None:
        self.edits.append((chat_id, message_id, text))

    async def reply(self, chat_id, text) -> int:
        self._mid += 1
        self.replies.append((chat_id, text))
        return self._mid

    async def answer(self, callback_id, text) -> None:
        self.answers.append((callback_id, text))

    def data(self, role: str, n: int = -1) -> str:
        """callback_data of the button with this role on the n-th sent message."""
        for _label, d in self.sent[n]["buttons"]:
            if d.split(":")[1] == role:
                return d
        raise KeyError(role)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
        return
    s = SQLiteStore(str(tmp_path / "a.sqlite3"))
    yield s
    s.close()


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def approver(store, transport):
    return Approver(None, [ALLOWED], store, transport=transport)
