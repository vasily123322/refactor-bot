from __future__ import annotations

from app.services.canonical_publication_delivery_runtime import (
    build_canonical_publication_delivery_runtime,
)


class _Bot:
    def __getattr__(self, name):
        raise AssertionError(f"runtime composition must not call bot method {name}")


def test_runtime_composition_wires_exact_plain_delivery_dependencies_without_io() -> None:
    bot = _Bot()
    session_factory = object()

    runtime = build_canonical_publication_delivery_runtime(
        bot=bot,
        session_factory=session_factory,  # type: ignore[arg-type]
        holder="runtime-composition-test",
        lease_seconds=1,
        heartbeat_interval_seconds=120,
    )

    assert runtime.sender.bot is bot
    assert runtime.sender.session_factory is session_factory
    assert runtime.result_link_resolver.bot is bot
    assert runtime.auxiliary_executor.bot is bot
    assert runtime.post_send_hook.executor is runtime.auxiliary_executor
    assert runtime.post_send_hook.session_factory is session_factory

    assert runtime.executor.session_factory is session_factory
    assert runtime.executor.sender is runtime.sender
    assert runtime.executor.result_link_resolver is runtime.result_link_resolver
    assert runtime.executor.post_send_hook is runtime.post_send_hook
    assert runtime.executor.holder == "runtime-composition-test"
    assert runtime.executor.lease_seconds == 30
    assert runtime.executor.heartbeat_interval_seconds == 15.0


def test_runtime_composition_does_not_share_mutable_execution_objects() -> None:
    bot = _Bot()
    session_factory = object()

    first = build_canonical_publication_delivery_runtime(
        bot=bot,
        session_factory=session_factory,  # type: ignore[arg-type]
    )
    second = build_canonical_publication_delivery_runtime(
        bot=bot,
        session_factory=session_factory,  # type: ignore[arg-type]
    )

    assert first is not second
    assert first.executor is not second.executor
    assert first.sender is not second.sender
    assert first.result_link_resolver is not second.result_link_resolver
    assert first.auxiliary_executor is not second.auxiliary_executor
    assert first.post_send_hook is not second.post_send_hook
