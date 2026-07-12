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
    to `run_team()`.

    Also the run-level budget governor: every `turn_completed` event
    carries that turn's real `cost_usd`, so this is the one place that
    sees the whole run's spend as it happens. When `max_run_budget_usd`
    is set and cumulative spend crosses it, the run is stopped through
    the exact same kill switch a crashed agent uses — checkpointed,
    resumable, with a clear reason — rather than a new mechanism.
    Deliberately checked AFTER a turn completes, not before it starts:
    a turn already in flight has already spent its money, and its output
    is on disk either way; the cap prevents the NEXT session, which is
    the only spend still preventable. `seed_spent()` lets a resumed run
    start the counter from what the interrupted run already spent
    (main.py derives it from the workspace's SDK log) instead of zero.
    """

    def __init__(self, inner: OnEvent | None = None, max_run_budget_usd: float | None = None) -> None:
        self._inner = inner
        self._abort = asyncio.Event()
        self.failure: AgentFailure | None = None
        self._max_run_budget_usd = max_run_budget_usd
        self.spent_usd = 0.0

    def seed_spent(self, amount_usd: float) -> None:
        """Start the cumulative counter from a prior partial run's spend
        (resume path) so the run-level cap covers the whole run, not just
        the portion after the latest resume."""
        self.spent_usd = amount_usd

    def reset(self) -> None:
        """Re-arm after a handled failure so the same monitor can watch
        the auto-resumed continuation (main.py's recovery loop). Clears
        the kill switch and the recorded failure; deliberately does NOT
        clear `spent_usd` — the run-level budget is cumulative across
        resumes (seed_spent() re-derives it authoritatively anyway)."""
        self.failure = None
        self._abort = asyncio.Event()

    async def on_event(self, source: str, event_type: str, detail: str, extra: dict) -> None:
        if self._inner is not None:
            await self._inner(source, event_type, detail, extra)
        if event_type == "turn_completed":
            self.spent_usd += extra.get("cost_usd") or 0.0
            if (
                self._max_run_budget_usd is not None
                and self.spent_usd > self._max_run_budget_usd
                and not self._abort.is_set()
            ):
                self.failure = AgentFailure(
                    "run_budget_governor",
                    f"run budget exceeded: ${self.spent_usd:.2f} spent > "
                    f"${self._max_run_budget_usd:.2f} cap (LOOPER_MAX_RUN_BUDGET_USD) — "
                    f"run stopped after the completed turn; checkpoint is resumable",
                )
                self._abort.set()
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
