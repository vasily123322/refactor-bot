from __future__ import annotations

import asyncio


def test_editor_back_restores_canonical_publication_card(monkeypatch) -> None:
    async def run() -> None:
        from app.bot.routers import content_plan_publication
        from app.bot.routers.post_editor import _cb_back_to_cp_if_needed

        opened: list[str] = []

        async def fake_open(callback, state) -> None:
            opened.append(str(callback.data))

        monkeypatch.setattr(
            content_plan_publication,
            "cb_cp_open_publication",
            fake_open,
        )

        class FakeCallback:
            def __init__(self, data: str = "post_back") -> None:
                self.data = data

            def model_copy(self, *, update: dict) -> "FakeCallback":
                return FakeCallback(str(update["data"]))

            async def answer(self, *args, **kwargs) -> None:
                return None

        class FakeState:
            def __init__(self) -> None:
                self.data = {
                    "prev_editor_restore": {
                        "type": "cp_publication_card",
                        "publication_id": 77,
                        "date": "2026-08-10",
                    }
                }

            async def get_data(self) -> dict:
                return dict(self.data)

            async def update_data(self, **kwargs) -> None:
                self.data.update(kwargs)

        state = FakeState()
        handled = await _cb_back_to_cp_if_needed(FakeCallback(), state)  # type: ignore[arg-type]

        assert handled is True
        assert opened == ["cp_open_pub:77:2026-08-10"]
        assert state.data["prev_editor_restore"] is None

    asyncio.run(run())
