"""Scripted stand-in for ClaudeCodeAgent — a "shadow" test double.

Returns canned text instead of running a real claude_agent_sdk session, so
GraphFlow routing (verdict-token edges, rework loops, activation groups,
hard-stop termination) and checkpoint/resume can be verified deterministically,
instantly, and without spending any Claude Code session quota. Only the SDK
call itself is stubbed — save_state()/load_state() and the rest of
BaseChatAgent's plumbing are inherited unchanged, so a test using this still
exercises the real checkpoint/resume machinery.

Usage: call `set_script({"qa_engineer": [...], ...})` before each test, then
build the team with `build_team(workspace, agent_cls=ScriptedClaudeCodeAgent)`.
A role's response list is consumed in order; once exhausted, the last entry
repeats — so a role that should behave the same way every turn only needs
one entry (e.g. `{"qa_engineer": ["...\\nQA_PASS"]}` always passes).
"""

from collections import defaultdict
from typing import Sequence

from autogen_agentchat.base import Response
from autogen_agentchat.messages import BaseChatMessage, TextMessage
from autogen_core import CancellationToken

from aiteam.claude_code_agent import ClaudeCodeAgent

class _Crash:
    """Script sentinel: raise instead of responding, simulating a real
    mid-turn failure (e.g. the Claude Code session-limit RuntimeError).
    An exception is what genuinely halts GraphFlow's runtime — unlike a
    consumer simply stopping mid-`async for`, which does not: the runtime
    is a producer/consumer queue, and with near-instant scripted responses
    it races ahead of a paused consumer rather than pausing with it.
    """


CRASH = _Crash()

_scripts: dict[str, list[str | _Crash]] = {}
_call_counts: dict[str, int] = defaultdict(int)


def set_script(scripts: dict[str, list[str | _Crash]]) -> None:
    """Configure canned responses (or CRASH) per agent name. Call at the start of each test."""
    _scripts.clear()
    _scripts.update(scripts)
    _call_counts.clear()


def call_count(name: str) -> int:
    """How many times `name` has responded since the last set_script()."""
    return _call_counts[name]


class ScriptedClaudeCodeAgent(ClaudeCodeAgent):
    """Drop-in ClaudeCodeAgent replacement: on_messages() returns the next
    scripted response for this agent's name instead of calling claude_agent_sdk.

    Still calls the inherited `_emit()` at the same two turn-boundary points
    the real class does (turn_started / turn_completed / error) — so a test
    that wires `on_event` through `build_team()` exercises the actual event
    plumbing (server/pipeline_runner.py's DB persistence + SSE broadcast),
    not just routing. `_emit()` is a no-op when `on_event` isn't set, which
    is true for every existing routing/rework-loop test — this is additive,
    not a behavior change for them.
    """

    async def on_messages(
        self, messages: Sequence[BaseChatMessage], cancellation_token: CancellationToken
    ) -> Response:
        self._history.extend(messages)
        await self._emit("turn_started")
        responses = _scripts.get(self.name, ["(no script configured for this agent)"])
        idx = min(_call_counts[self.name], len(responses) - 1)
        _call_counts[self.name] += 1
        entry = responses[idx]
        if isinstance(entry, _Crash):
            await self._emit("error", "simulated crash (scripted)")
            raise RuntimeError(f"{self.name}: simulated crash (scripted)")
        await self._emit("turn_completed", entry, cost_usd=0.0, duration_ms=0)
        return Response(chat_message=TextMessage(content=entry, source=self.name))
