"""Drives one GraphFlow run to completion (or failure), shared by
`main.py` (CLI) and `server/pipeline_runner.py` (web UI) so this
fail-fast behavior lives in exactly one place.

Why this exists: when a `ClaudeCodeAgent` hits a real failure (e.g. the
20-turn cap), it raises inside `on_messages()`, which is *supposed* to
stop the whole `GraphFlow` run. Confirmed live that the installed AutoGen
version does not do this cleanly: the failure surfaced as an unhandled
internal message-routing error in a background asyncio task (a
`GroupChatError` published to a container with no handler for it,
immediately followed by the runtime's message queue shutting down while a
second, unrelated engineer was still mid-turn) — none of which ever
raised back out of `team.run_stream()`. Net effect without this module:
one engineer's crash went unnoticed, the other two kept running
independently (and also eventually crashed, unnoticed), and the run sat
reporting "running" forever with no way to tell it was already dead.

The fix doesn't depend on GraphFlow's own error propagation at all: every
`ClaudeCodeAgent` already calls its `on_event` hook with
`event_type="error"` synchronously, immediately before it raises. This
module uses that hook as a kill switch — the instant any agent reports an
error, it cancels the task driving `team.run_stream()` outright, rather
than waiting for a graph-level exception that may never come. Cancelling
that task is best-effort, not a guarantee: cancellation propagates to
whatever that task is currently awaiting, which in practice is the agent
runtime's own cooperative processing, but a sibling engineer's in-flight
`claude` CLI subprocess finishing anyway before noticing is a possible
outcome and is harmless — its result is simply discarded, since the run
is already marked failed.
"""

import asyncio
import contextlib
from typing import Awaitable, Callable

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import GraphFlow

from .claude_code_agent import OnEvent


class AgentFailure(Exception):
    """Raised by run_team the instant any agent reports an "error" event
    — a genuine Claude Code session failure (hit max_turns, a real SDK
    error), not a QA_FAIL verdict (a normal, already-handled routing
    outcome, not a crash)."""

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


class FailFastMonitor:
    """Wraps a caller-supplied `on_event` so a crash is detected from the
    agent's *own* error report, independent of whether GraphFlow itself
    notices. Pass `.on_event` to `build_team()`; pass the monitor itself
    to `run_team()`."""

    def __init__(self, inner: OnEvent | None = None) -> None:
        self._inner = inner
        self._abort = asyncio.Event()
        self.failure: AgentFailure | None = None

    async def on_event(self, source: str, event_type: str, detail: str, extra: dict) -> None:
        if self._inner is not None:
            await self._inner(source, event_type, detail, extra)
        if event_type == "error" and not self._abort.is_set():
            self.failure = AgentFailure(source, detail)
            self._abort.set()

    async def wait(self) -> None:
        await self._abort.wait()


async def run_team(
    team: GraphFlow,
    task: str | None,
    on_message: Callable[[BaseChatMessage], Awaitable[None]],
    monitor: FailFastMonitor,
) -> str:
    """Drives `team.run_stream(task)` to completion, calling `on_message`
    for every chat message produced. Returns the stop reason on success.
    Raises `AgentFailure` (or whatever `team.run_stream()` itself raised,
    if GraphFlow's own propagation happens to work) the instant any agent
    reports an error — having first cancelled the run outright so no
    other agent keeps working, and spending real Claude Code quota,
    against a pipeline that's already doomed."""

    stop_reason = ""

    async def consume() -> None:
        nonlocal stop_reason
        async for message in team.run_stream(task=task):
            if isinstance(message, TaskResult):
                stop_reason = message.stop_reason or ""
                continue
            if isinstance(message, BaseChatMessage):
                await on_message(message)

    consume_task = asyncio.create_task(consume())
    abort_task = asyncio.create_task(monitor.wait())
    try:
        await asyncio.wait({consume_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)

        if monitor.failure is not None:
            consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await consume_task
            raise monitor.failure

        # No agent reported an error — consume_task is the one that ended,
        # either normally (return stop_reason) or with a genuine exception
        # (the case where GraphFlow's own propagation does work).
        exc = consume_task.exception()
        if exc is not None:
            raise exc
        return stop_reason
    finally:
        for t in (consume_task, abort_task):
            if not t.done():
                t.cancel()
