"""Tests for the two dollar-budget caps.

Per-SESSION cap (LOOPER_MAX_SESSION_BUDGET_USD → ClaudeAgentOptions
.max_budget_usd): the SDK stops an exceeded session with an
`error_max_budget_usd` ResultMessage, which ClaudeCodeAgent must treat
like error_max_turns — partial output + warning banner, never a
run-killing crash. The presumed CLI double-exception after it (symmetric
with max_turns' "for shell-script consumers" exit) is NOT covered here —
that's SDK-transport-level, live-verification territory, per the same
note on tests/test_max_turns_handling.py.

Run-level cap (LOOPER_MAX_RUN_BUDGET_USD → runner.FailFastMonitor): the
monitor accumulates cost from turn_completed events and trips the same
kill switch a crashed agent uses once cumulative spend crosses the cap.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import os
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
from looper.pipeline import build_team
from looper.runner import FailFastMonitor
from tests.mock_agent import ScriptedClaudeCodeAgent


def _budget_result() -> ResultMessage:
    return ResultMessage(
        subtype="error_max_budget_usd",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=True,
        num_turns=7,
        session_id="test-session",
        total_cost_usd=5.01,
        result=None,
    )


class SessionBudgetCapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cwd = Path(tempfile.mkdtemp(prefix="looper_budget_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.cwd, ignore_errors=True)

    def _agent(self, max_budget_usd: float | None) -> ClaudeCodeAgent:
        return ClaudeCodeAgent(
            name="backend_engineer",
            description="test",
            system_prompt="test",
            cwd=self.cwd,
            max_turns=20,
            max_budget_usd=max_budget_usd,
        )

    async def test_cap_reaches_claude_agent_options(self) -> None:
        captured = None

        async def _fake_query(prompt, options):
            nonlocal captured
            captured = options.max_budget_usd
            return
            yield  # pragma: no cover - makes this an async generator

        agent = self._agent(max_budget_usd=5.0)
        with mock.patch.object(cca, "query", _fake_query):
            await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nT-1 [BE]", source="solution_architect")],
                CancellationToken(),
            )
        self.assertEqual(captured, 5.0)

    async def test_budget_exceeded_degrades_instead_of_crashing(self) -> None:
        async def _hits_budget(prompt, options):
            yield _budget_result()

        agent = self._agent(max_budget_usd=5.0)
        with mock.patch.object(cca, "query", _hits_budget):
            response = await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nT-1 [BE]", source="solution_architect")],
                CancellationToken(),
            )
        text = response.chat_message.to_text()
        self.assertIn("cut off at its session budget cap", text)
        # A dollar-capped session must NOT arm the turn-budget auto-bump —
        # more turns can't help a session whose constraint is money.
        self.assertFalse(agent._last_hit_max_turns)

    async def test_no_cap_passes_none_through(self) -> None:
        captured = "sentinel"

        async def _fake_query(prompt, options):
            nonlocal captured
            captured = options.max_budget_usd
            return
            yield  # pragma: no cover

        agent = self._agent(max_budget_usd=None)
        with mock.patch.object(cca, "query", _fake_query):
            await agent.on_messages(
                [TextMessage(content="# TECH DESIGN\nT-1 [BE]", source="solution_architect")],
                CancellationToken(),
            )
        self.assertIsNone(captured)


class BuildTeamSessionBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="looper_budget_env_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_env_var_reaches_every_agent(self) -> None:
        with mock.patch.dict(os.environ, {"LOOPER_MAX_SESSION_BUDGET_USD": "3.5"}):
            _, agents = build_team(self.workspace, agent_cls=ScriptedClaudeCodeAgent)
        for agent in agents.values():
            self.assertEqual(agent._max_budget_usd, 3.5)

    def test_unset_env_means_uncapped(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "LOOPER_MAX_SESSION_BUDGET_USD"}
        with mock.patch.dict(os.environ, env, clear=True):
            _, agents = build_team(self.workspace, agent_cls=ScriptedClaudeCodeAgent)
        self.assertIsNone(agents["qa_engineer"]._max_budget_usd)


class RunBudgetGovernorTests(unittest.IsolatedAsyncioTestCase):
    async def _turn(self, monitor: FailFastMonitor, cost: float) -> None:
        await monitor.on_event("backend_engineer", "turn_completed", "done", {"cost_usd": cost})

    async def test_trips_once_cumulative_spend_crosses_cap(self) -> None:
        monitor = FailFastMonitor(max_run_budget_usd=5.0)
        await self._turn(monitor, 2.0)
        self.assertIsNone(monitor.failure)
        await self._turn(monitor, 2.0)
        self.assertIsNone(monitor.failure)
        await self._turn(monitor, 2.0)  # 6.0 > 5.0
        self.assertIsNotNone(monitor.failure)
        self.assertEqual(monitor.failure.source, "run_budget_governor")
        self.assertIn("resumable", monitor.failure.detail)

    async def test_no_cap_never_trips(self) -> None:
        monitor = FailFastMonitor()
        await self._turn(monitor, 1000.0)
        self.assertIsNone(monitor.failure)
        self.assertEqual(monitor.spent_usd, 1000.0)

    async def test_seed_spent_counts_prior_partial_run(self) -> None:
        """Resume path: the cap covers the whole run across resumes, not
        each resume separately."""
        monitor = FailFastMonitor(max_run_budget_usd=5.0)
        monitor.seed_spent(4.5)
        await self._turn(monitor, 1.0)  # 5.5 > 5.0
        self.assertIsNotNone(monitor.failure)

    async def test_missing_cost_in_event_is_tolerated(self) -> None:
        monitor = FailFastMonitor(max_run_budget_usd=5.0)
        await monitor.on_event("x", "turn_completed", "done", {"cost_usd": None})
        await monitor.on_event("x", "turn_completed", "done", {})
        self.assertEqual(monitor.spent_usd, 0.0)
        self.assertIsNone(monitor.failure)

    async def test_error_events_still_trip_the_kill_switch(self) -> None:
        """Sanity: the budget addition must not break the original
        crash-detection behavior."""
        monitor = FailFastMonitor(max_run_budget_usd=5.0)
        await monitor.on_event("frontend_engineer", "error", "boom", {})
        self.assertIsNotNone(monitor.failure)
        self.assertEqual(monitor.failure.source, "frontend_engineer")


if __name__ == "__main__":
    unittest.main()
