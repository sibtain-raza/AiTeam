"""Unit tests for the max_turns auto-bump in ClaudeCodeAgent.on_messages().

Regression coverage for a real, observed failure mode (see a real run's
sdk-interactions.jsonl): the architect's per-engineer turn budget is only
ever set once, right after the architect's own turn. If an engineer then
hits max_turns on its first pass, the QA-rework turn that follows — which
has MORE work to do (fix the QA defects AND finish whatever the truncated
first pass never got to) — used to be handed the exact same budget that
had already proven insufficient, sometimes hitting the ceiling again.

Fix: `ClaudeCodeAgent._last_hit_max_turns` persists across calls (unlike
the local `hit_max_turns` variable in on_messages(), which used to be
discarded when the function returned). The next on_messages() call
consults and clears it, bumping `self._max_turns` before starting its
session.

These patch `claude_agent_sdk.query` (imported by name into
claude_code_agent.py) with fake async generators, same technique as
test_skip_idle_engineer.py — no real Claude Code session, no quota spent.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from claude_agent_sdk import ResultMessage

from looper import claude_code_agent as cca
from looper.claude_code_agent import ClaudeCodeAgent


def make_agent(name: str, cwd: Path, max_turns: int) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        name=name,
        description="test",
        system_prompt="test",
        cwd=cwd,
        max_turns=max_turns,
    )


def _error_max_turns_result(max_turns: int) -> ResultMessage:
    return ResultMessage(
        subtype="error_max_turns",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=True,
        num_turns=max_turns,
        session_id="test-session",
        total_cost_usd=1.23,
        result=None,
    )


class MaxTurnsBudgetBumpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_bump_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    async def test_hitting_max_turns_bumps_budget_for_the_next_turn(self) -> None:
        agent = make_agent("backend_engineer", self.workspace_dir, max_turns=20)

        async def _hits_max_turns(prompt, options):
            yield _error_max_turns_result(options.max_turns)

        with mock.patch.object(cca, "query", _hits_max_turns):
            response = await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nT-1 [BE] one endpoint", source="solution_architect")],
                CancellationToken(),
            )

        # The budget for the turn that just ran must be untouched (this run
        # already happened) — the bump applies to the NEXT turn.
        self.assertEqual(agent._max_turns, 20)
        self.assertTrue(agent._last_hit_max_turns)
        # A truncated turn's message is narration of unfinished work, and it
        # becomes the role's persisted artifact replayed to QA/downstream —
        # it must carry the explicit truncation warning.
        self.assertIn("cut off at its turn limit", response.chat_message.to_text())

        captured_max_turns = None

        async def _succeeds(prompt, options):
            nonlocal captured_max_turns
            captured_max_turns = options.max_turns
            return
            yield  # pragma: no cover - makes this an async generator

        with mock.patch.object(cca, "query", _succeeds):
            await agent.on_messages(
                [TextMessage(content="# QA REPORT\nD-1 ...\nQA_FAIL", source="qa_engineer")],
                CancellationToken(),
            )

        # +50%, floor +8: 20 + max(8, 10) = 30.
        self.assertEqual(captured_max_turns, 30)
        self.assertEqual(agent._max_turns, 30)
        self.assertFalse(agent._last_hit_max_turns, "the flag must be one-shot, not keep re-bumping")

    async def test_bump_is_clamped_to_the_ceiling(self) -> None:
        agent = make_agent("backend_engineer", self.workspace_dir, max_turns=45)

        async def _hits_max_turns(prompt, options):
            yield _error_max_turns_result(options.max_turns)

        with mock.patch.object(cca, "query", _hits_max_turns):
            await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nT-1 [BE] one endpoint", source="solution_architect")],
                CancellationToken(),
            )

        captured_max_turns = None

        async def _succeeds(prompt, options):
            nonlocal captured_max_turns
            captured_max_turns = options.max_turns
            return
            yield  # pragma: no cover - makes this an async generator

        with mock.patch.object(cca, "query", _succeeds):
            await agent.on_messages(
                [TextMessage(content="# QA REPORT\nD-1 ...\nQA_FAIL", source="qa_engineer")],
                CancellationToken(),
            )

        # 45 + max(8, 22) = 67, clamped down to the 50 ceiling.
        self.assertEqual(captured_max_turns, 50)

    async def test_a_clean_turn_does_not_bump_the_next_one(self) -> None:
        """Sanity/positive-control: a turn that finishes without hitting
        max_turns must leave the next turn's budget untouched — guards
        against the bump firing unconditionally instead of only after a
        genuine error_max_turns."""
        agent = make_agent("backend_engineer", self.workspace_dir, max_turns=20)

        async def _succeeds_first(prompt, options):
            return
            yield  # pragma: no cover - makes this an async generator

        with mock.patch.object(cca, "query", _succeeds_first):
            response = await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nT-1 [BE] one endpoint", source="solution_architect")],
                CancellationToken(),
            )

        self.assertFalse(agent._last_hit_max_turns)
        # ...and a clean turn must NOT carry the truncation warning.
        self.assertNotIn("cut off at its turn limit", response.chat_message.to_text())

        captured_max_turns = None

        async def _succeeds_second(prompt, options):
            nonlocal captured_max_turns
            captured_max_turns = options.max_turns
            return
            yield  # pragma: no cover - makes this an async generator

        with mock.patch.object(cca, "query", _succeeds_second):
            await agent.on_messages(
                [TextMessage(content="# QA REPORT\nD-1 ...\nQA_FAIL", source="qa_engineer")],
                CancellationToken(),
            )

        self.assertEqual(captured_max_turns, 20)


if __name__ == "__main__":
    unittest.main()
