from __future__ import annotations

import asyncio


def test_canonical_context_dispatches_without_legacy_edit_ids(monkeypatch) -> None:
    async def run() -> None:
        from app.bot.routers import posting_publish
        from app.bot.routers.utils import canonical_publication_edit

        calls: list[tuple[dict, dict]] = []

        async def fake_handle(callback, state, *, data: dict, payload: dict) -> None:
            calls.append((dict(data), dict(payload)))

        monkeypatch.setattr(
            canonical_publication_edit,
            "handle_canonical_publication_edit",
            fake_handle,
        )

        class FakeState:
            async def get_data(self) -> dict:
                return {
                    "canonical_edit_context": {
                        "publication_id": 42,
                        "expected_revision": 3,
                    },
                    "payload": {"type": "text", "text": "Canonical only"},
                    # The regression is specifically that canonical intent remains an
                    # edit even when these mutable legacy transport fields are absent.
                    "edit_chat_id": None,
                    "edit_msg_id": None,
                    "is_draft": False,
                }

        await posting_publish.cb_post_send(object(), FakeState())  # type: ignore[arg-type]

        assert len(calls) == 1
        data, payload = calls[0]
        assert data["canonical_edit_context"] == {
            "publication_id": 42,
            "expected_revision": 3,
        }
        assert payload == {"type": "text", "text": "Canonical only"}

    asyncio.run(run())
