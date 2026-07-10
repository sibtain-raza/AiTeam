"""Unit tests for ClaudeCodeAgent.on_messages()'s zero-turn-budget skip.

Regression coverage for a real, observed failure mode: ARCHITECT_PROMPT
used to presuppose all three engineers (FE/BE/OPS) always have real work
("a production-grade technical design that three engineers ... can
implement"), so the architect routinely invented busywork (a backend
validation toolchain, Terraform/CI) even for a goal as trivial as a single
static "Hi" page — burning a full paid Claude Code session per invented
role. The fix: ARCHITECT_PROMPT now tells the architect to assign a role a
turn budget of exactly 0 when it has zero tagged tasks, parse_turn_budget()
leaves 0 unclamped (see test_turn_budget.py), and on_messages() below
short-circuits BEFORE starting a real claude_agent_sdk session whenever a
tool-using role's turn budget is 0.

These patch `claude_agent_sdk.query` (imported by name into
claude_code_agent.py) to raise if it's ever called, so a regression here
fails loudly and instantly instead of silently spending real Claude Code
quota — this is deliberately NOT covered by ScriptedClaudeCodeAgent
(mock_agent.py), which overrides on_messages() wholesale and therefore
can't exercise this code path at all (same reasoning as
test_max_turns_handling.py's note on error_max_turns: SDK-facing behavior
needs either this kind of direct patch or a live check, not the mock).

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken

from looper import claude_code_agent as cca
from looper.claude_code_agent import ClaudeCodeAgent


def make_agent(name: str, cwd: Path, max_turns: int, allowed_tools=None) -> ClaudeCodeAgent:
    kwargs = {} if allowed_tools is None else {"allowed_tools": allowed_tools}
    return ClaudeCodeAgent(
        name=name,
        description="test",
        system_prompt="test",
        cwd=cwd,
        max_turns=max_turns,
        **kwargs,
    )


class SkipIdleEngineerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_skip_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    async def test_zero_budget_skips_the_real_session(self) -> None:
        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("query() must not be called when max_turns == 0")

        agent = make_agent("backend_engineer", self.workspace_dir, max_turns=0)
        with mock.patch.object(cca, "query", _must_not_be_called):
            response = await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nNo [BE] tasks.", source="solution_architect")],
                CancellationToken(),
            )

        text = response.chat_message.to_text()
        self.assertIn("skipped", text.lower())
        self.assertIn("BACKEND_ENGINEER IMPLEMENTATION", text)
        self.assertFalse(agent._turn_in_progress, "a skipped turn must not be left dangling as in-progress")

    async def test_zero_budget_does_not_skip_tool_less_reasoning_roles(self) -> None:
        """max_turns == 0 should never trigger the skip path for a
        tool-less role (allowed_tools=[]) — those roles use max_turns purely
        as a runaway guard (see pipeline.py), never as a work-assignment
        signal, and PM/Architect/UAT/Reporter are never given a turn budget
        derived from the architect's per-engineer estimate in the first
        place. Guards against the `self._allowed_tools and ...` condition
        being loosened to also match tool-less roles."""
        agent = make_agent("product_manager", self.workspace_dir, max_turns=0, allowed_tools=[])

        def _fake_query(prompt, options):
            # Not `async def`/an async generator on purpose: we only need
            # to prove query() was actually reached (not skipped) — raising
            # synchronously here does that without leaving an unawaited
            # coroutine behind.
            raise RuntimeError("would have started a real session (fine for this test, we just check it's reached)")

        with mock.patch.object(cca, "query", _fake_query):
            with self.assertRaises(RuntimeError):
                await agent.on_messages(
                    [TextMessage(content="goal", source="user")], CancellationToken()
                )

    async def test_nonzero_budget_does_not_skip(self) -> None:
        """Sanity/positive-control: a real (non-zero) budget must still
        reach claude_agent_sdk.query() as before — guards against the skip
        check being too broad and accidentally short-circuiting normal
        engineer turns."""
        reached = False

        async def _fake_query(prompt, options):
            nonlocal reached
            reached = True
            return
            yield  # pragma: no cover - makes this an async generator

        agent = make_agent("backend_engineer", self.workspace_dir, max_turns=12)
        with mock.patch.object(cca, "query", _fake_query):
            await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nT-1 [BE] one endpoint", source="solution_architect")],
                CancellationToken(),
            )
        self.assertTrue(reached, "query() should have been called for a non-zero turn budget")


if __name__ == "__main__":
    unittest.main()
