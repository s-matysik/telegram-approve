# telegram-approve

Human-in-the-loop approvals for AI agents, over Telegram.

An agent drafts an action (an email, a post, a payment, a deletion). You get a Telegram message with
inline buttons: Approve, Edit, Reject. The action runs only after a yes from a person you allowed,
only with the exact content you saw, and at most once.

## Why

- An agent should never send, publish, pay or delete without a human yes.
- A yes must apply to exact content. If the draft changes after you approved it, the approval is void.
- A send must happen at most once. If the process crashes halfway through a send, the library does
  not retry on its own, because a duplicate email or payment is worse than a delayed one.

The only runtime dependency is `python-telegram-bot` (v21+, async). State lives in SQLite (stdlib) or
memory, behind a small `Store` interface.

## Install

```bash
pip install git+https://github.com/s-matysik/telegram-approve
```

Python 3.10 or newer.

## Quick start

An agent drafts an email and sends it only after Approve.

```python
import asyncio
import os

from telegram_approve import Approver, Draft, SQLiteStore


async def send_email(payload: dict) -> str:
    ...  # your SMTP / API call; receives exactly the approved payload
    return "sent"


async def redraft(approval, notes: str) -> Draft:
    # Called when you press Edit and reply with corrections. Ask your agent for a new draft.
    payload = {**approval.payload, "body": f"{approval.payload['body']}\n(revised: {notes})"}
    return Draft(title="Email to Anna (v2)", body=payload["body"], payload=payload)


async def main() -> None:
    approver = Approver(
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        allowed_user_ids=[int(os.environ["TELEGRAM_APPROVER_ID"])],
        store=SQLiteStore("approvals.sqlite3"),
        on_edit=redraft,
    )
    async with approver:  # starts long polling
        payload = {"to": "anna@example.com", "subject": "Offer", "body": "Hello Anna, ..."}
        approval = await approver.request(
            action_id="email-2026-001",
            title="Send email to anna@example.com",
            body=payload["body"],
            payload=payload,
            expires_in=3600,
        )
        await approval.wait()
        while approval.successor:  # the user edited the draft: wait for the new request
            approval = await approval.successor.wait()

        if approval.state.value == "approved":
            await approver.execute(approval.action_id, approval.payload, send_email)


asyncio.run(main())
```

Instead of `wait()` you can pass `on_decision=callback` to `request()`; it is called (sync or async)
after each decision with the `Approval`.

## API

| Call | Purpose |
|---|---|
| `Approver(bot_token, allowed_user_ids, store, *, transport=None, chat_ids=None, on_edit=None)` | Create the approver. `chat_ids` defaults to the allowed user ids (private chats). |
| `await request(action_id, title, body, payload, attachments=None, buttons=("Approve","Edit","Reject"), *, expires_in=None, on_decision=None)` | Send the message and store the request with `sha256(canonical_json(payload))`. `buttons` is 1 to 3 labels (approve, edit, reject in that order for 3; approve, reject for 2) or a dict keyed by role. `action_id` matches `[A-Za-z0-9_.~-]{1,40}`. |
| `await approval.wait(timeout=None)` | Wait for a decision. On timeout the request expires. |
| `await execute(action_id, payload, fn)` | Run `fn(payload)` (sync or async) at most once, only if approved and the payload hash matches. |
| `stuck()` / `resolve(action_id, "done" \| "failed" \| "retry", by, note)` | List and manually settle actions left in `executing`. |
| `await expire_overdue()` | Expire pending requests past `expires_in`. Call it periodically. |
| `get(action_id)` | Load an `Approval` handle from the store (for example after a restart). |
| `Draft(title, body, payload, attachments=None)` | Return value of the `on_edit` handler. |

Attachments are file paths or `(filename, bytes)` tuples, sent as documents after the message.

### States

```
pending -> approved -> executing -> done
   |                        \-> failed
   |-> rejected
   |-> expired
   |-> superseded   (Edit produced a new draft; the new request has a new hash)
   '-> revise       (Edit pressed and no on_edit handler: your notes are in approval.note)
```

Audit fields on every record: `decided_by`, `decided_at`, `decision_note`, `executed_at`,
`finished_at`, `result_json`, `error`, `supersedes`, `superseded_by`, `resolution_note`.

## Invariants

1. Only user ids in `allowed_user_ids` can decide. The check runs on callback queries and on text
   messages, not only on messages. Everyone else is ignored (callbacks get "Not allowed.").
2. A decision is bound to the content hash. Callback data carries a hash prefix, and `execute()`
   raises `HashMismatch` when the payload differs from the approved one.
3. Execution is at most once. State changes use an atomic compare-and-set in the store, so
   concurrent `execute()` calls run `fn` once and the rest get `NotApproved`.
4. An action left in `executing` (crash during the side effect) is never retried automatically. It
   shows up in `stuck()` and needs `resolve()` by an operator.
5. A failed `fn` marks the action `failed`. It is not retried either.
6. The first decision wins. Later presses, late presses after expiry, and presses on a superseded
   request change nothing.
7. Edit creates a new request (`<id>~r2`, `~r3`, ...) with a new hash and supersedes the old one.
   The edited draft needs a fresh approval.
8. Every decision records who decided and when.

## Store

`Store` is a small protocol: `create`, `get`, `transition` (atomic compare-and-set on state),
`update`, `list_by_state`. Included:

- `SQLiteStore(path)` on stdlib `sqlite3`, WAL mode, one table.
- `MemoryStore()` for tests.

Implement the protocol to use Postgres or Redis. `transition` must be atomic.

## Testing your agent

`Approver` accepts any object implementing the `Transport` protocol, so your tests can run without
network. See `tests/conftest.py` for a fake transport. The test suite covers every invariant above.

## Security notes

- Keep the bot token in the environment or a secret manager, never in code or the repository.
- `allowed_user_ids` must be non-empty and is the whole access control. Presses from anyone else are ignored.
- Use private chats. In a group, any member could see the draft; only allowed ids can decide.
- Messages are sent as plain text (no markup parsing), so draft content cannot inject formatting.
- Payloads and drafts are stored in the database in clear text. Protect the file.

## Limitations

- Long polling, one process. Waiting handles (`wait()`) live in memory. After a restart, button
  presses still work and are stored, but use `get(action_id)` to read the outcome.
- Run one polling process per bot token.
- The payload must be JSON-serializable. The hash covers the payload only, so put everything that
  matters (recipient, subject, body, amount) into it.
- Exactly-once is not possible across a crash. The library gives at-most-once and makes the
  ambiguous case visible and manual.
- Message text is truncated at 3500 characters; send long content as an attachment.

## License

MIT. See `LICENSE`.
