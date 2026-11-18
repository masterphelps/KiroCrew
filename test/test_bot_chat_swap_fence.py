"""A Bot-chat app stop sees an in-place conversation swap as a start in flight.

Every way a chat gets a new native session must be one the stop's fence sees:

* a cold start: ``SessionAllocationService.spawn_in_flight``;
* an in-place swap (``swap_conversation``, which Edit & Resend and the dashboard
  rewind take, and which runs ``AcpProvider.new_conversation`` and from it the
  runtime's ``create_session``): ``bot_chat_ending_fence.begin_swap`` / ``end_swap``.

Each test names the change that turns it red. ``create_session`` is held on an
event, so the stop lands inside the swap without a sleep.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.bot_chat_ending_fence import discard_with_ending_fence
from kiro_crew.session import SessionManager

KEY = "dashboard:bot-chat-swap"


async def _bounded(awaitable):
    """Every handshake and join: a ceiling at the wait-discipline floor, which only
    names a hang, so a broken handshake fails the test instead of the worker."""
    return await asyncio.wait_for(awaitable, timeout=30)


async def _end(task: asyncio.Task) -> None:
    """Cancel *task* if a failed assertion left it waiting, and reap it."""
    if not task.done():
        task.cancel()
    try:
        await _bounded(task)
    except (asyncio.CancelledError, Exception):
        pass


SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


async def _setup(cfg):
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await _bounded(mgr.get_or_create(KEY))
    mgr.release(mgr._fold_key(KEY))
    provider.swaps_conversation_in_place = True
    provider.session_id = "old-sid"
    runtime = object()
    provider._client = SimpleNamespace(_runtime=runtime)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _new_conversation() -> None:
        entered.set()
        await _bounded(release.wait())  # the runtime's create_session, held
        provider.session_id = "fresh-sid"

    provider.new_conversation = AsyncMock(side_effect=_new_conversation)
    return mgr, provider, runtime, entered, release


@pytest.mark.asyncio
async def test_a_stop_inside_a_swap_is_unconfirmed_and_the_replacement_is_kept(cfg):
    """Drop the swap check from ``discard_with_ending_fence`` and the stop reads
    confirmed; drop the outrun branch from ``swap_conversation`` and the
    replacement carrying the app's servers is never registered for its close."""
    mgr, provider, runtime, entered, release = await _setup(cfg)
    recorded: list[tuple] = []
    with patch(
        "kiro_crew.bot_chat_app_tools.note_unconfirmed_close",
        side_effect=lambda key, rt, sid: recorded.append((key, rt, sid)),
    ):
        swap = asyncio.create_task(mgr.swap_conversation(KEY))
        try:
            await _bounded(entered.wait())
            _confirmed, in_flight = await _bounded(discard_with_ending_fence(mgr, KEY, []))
            release.set()
            outcome = await _bounded(swap)
        finally:
            release.set()
            await _end(swap)

    assert in_flight is not None and "in-place swap" in in_flight
    assert outcome == "unavailable"
    assert (mgr._fold_key(KEY), runtime, "fresh-sid") in recorded


@pytest.mark.asyncio
async def test_a_swap_is_refused_while_a_stop_runs(cfg):
    """Let ``begin_swap`` admit a swap under a running stop and the fresh session
    starts on the spec the stop is withdrawing."""
    mgr, provider, _runtime, _entered, release = await _setup(cfg)
    release.set()
    discarding = asyncio.Event()
    finish = asyncio.Event()
    real_discard = mgr.discard_conversation

    async def _held_discard(key, **kw):
        discarding.set()
        await _bounded(finish.wait())
        return await _bounded(real_discard(key, **kw))

    mgr.discard_conversation = _held_discard  # type: ignore[method-assign]
    stop = asyncio.create_task(discard_with_ending_fence(mgr, KEY, []))
    try:
        await _bounded(discarding.wait())
        assert await _bounded(mgr.swap_conversation(KEY)) == "unavailable"
        finish.set()
        await _bounded(stop)
    finally:
        finish.set()
        await _end(stop)
    provider.new_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_swap_with_no_stop_beside_it_swaps(cfg):
    mgr, provider, _runtime, _entered, release = await _setup(cfg)
    release.set()
    assert await _bounded(mgr.swap_conversation(KEY)) == "swapped"
    assert provider.session_id == "fresh-sid"


@pytest.mark.asyncio
async def test_a_background_fresh_conversation_under_a_running_stop_takes_the_reset():
    """Call ``new_conversation`` without ``begin_swap`` and a background turn's fresh
    session starts while a Bot-chat stop is withdrawing the app."""
    from kiro_crew import bot_chat_ending_fence as fence
    from kiro_crew.llm_helpers_parts.background_turns import _start_fresh_conversation

    sessions = SimpleNamespace(
        _fold_key=lambda k: k, end_children_for=AsyncMock(), reset=AsyncMock()
    )
    client = SimpleNamespace(new_conversation=AsyncMock())
    fence._STOPPING.add(KEY)
    await _bounded(_start_fresh_conversation(sessions, KEY, client))
    client.new_conversation.assert_not_awaited()
    sessions.reset.assert_awaited_once()


def _new_conversation_callers() -> set[str]:
    callers = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "new_conversation" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            named = isinstance(func, ast.Attribute) and func.attr == "new_conversation"
            looked_up = (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "new_conversation"
            )
            if named or looked_up:
                callers.add(path.relative_to(SRC).as_posix())
    return callers


def test_every_in_place_new_conversation_is_fenced():
    """A new caller of ``new_conversation`` would start a native session the stop
    cannot see; add it here only with the ``begin_swap`` / ``end_swap`` record."""
    assert _new_conversation_callers() == {
        "session_lifecycle_parts/teardown.py",  # swap_conversation: fenced
        "llm_helpers_parts/background_turns.py",  # fenced
        "providers/acp.py",  # AcpProvider.new_conversation, reached from the two above
        "workflows/agent_pool.py",  # a workflow pool's own sessions, never a Bot chat
    }
    rewind = (SRC / "dashboard" / "chat_rewind.py").read_text(encoding="utf-8")
    assert "sessions.swap_conversation(" in rewind and "new_conversation(" not in rewind
