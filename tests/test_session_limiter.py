"""Unit tests for the bounded-concurrency session scheduler
(ClaudeCodeAgent's `session_limiter` + build_team's
LOOPER_MAX_PARALLEL_SESSIONS wiring).

Patches `claude_agent_sdk.query` (imported by name into
claude_code_agent.py) with a slow fake that tracks concurrent executions —
same technique as test_skip_idle_engineer.py, no real SDK sessions. The
scheduler's contract: with a shared Semaphore(1), two agents' sessions may
never overlap; with no limiter (the default), they genuinely do.

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

from looper import claude_code_agent as cca
from looper.claude_code_agent import ClaudeCodeAgent
from looper.pipeline import build_team
from tests.mock_agent import ScriptedClaudeCodeAgent


class _ConcurrencyProbe:
    """A fake `query` that records the peak number of overlapping calls."""

    def __init__(self, hold_seconds: float = 0.05) -> None:
        self.hold_seconds = hold_seconds
        self.active = 0
        self.peak = 0

    def __call__(self, prompt, options):
        async def _gen():
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(self.hold_seconds)
            finally:
                self.active -= 1
            return
            yield  # pragma: no cover - makes this an async generator

        return _gen()


def make_agent(name: str, cwd: Path, limiter: asyncio.Semaphore | None) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        name=name,
        description="test",
        system_prompt="test",
        cwd=cwd,
        max_turns=10,
        session_limiter=limiter,
    )


class SessionLimiterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="looper_limiter_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    async def _run_pair(self, limiter: asyncio.Semaphore | None) -> int:
        probe = _ConcurrencyProbe()
        fe = make_agent("frontend_engineer", self.workspace / "frontend", limiter)
        be = make_agent("backend_engineer", self.workspace / "backend", limiter)
        design = TextMessage(content="# TECH DESIGN\ntasks", source="solution_architect")
        with mock.patch.object(cca, "query", probe):
            await asyncio.gather(
                fe.on_messages([design], CancellationToken()),
                be.on_messages([design], CancellationToken()),
            )
        return probe.peak

    async def test_shared_semaphore_prevents_overlap(self) -> None:
        peak = await self._run_pair(asyncio.Semaphore(1))
        self.assertEqual(peak, 1)

    async def test_no_limiter_allows_overlap(self) -> None:
        """Positive control: without a limiter the two sessions really do
        overlap — proves the serialized result above is the semaphore's
        doing, not an accident of test scheduling."""
        peak = await self._run_pair(None)
        self.assertEqual(peak, 2)

    async def test_limiter_released_after_a_session_crash(self) -> None:
        """A crashed session must release its slot, or every later agent
        in the run would deadlock waiting on a semaphore held by a corpse."""
        limiter = asyncio.Semaphore(1)
        agent = make_agent("frontend_engineer", self.workspace / "frontend", limiter)

        def _crashes(prompt, options):
            async def _gen():
                raise ValueError("boom")
                yield  # pragma: no cover

            return _gen()

        with mock.patch.object(cca, "query", _crashes):
            with self.assertRaises(RuntimeError):
                await agent.on_messages(
                    [TextMessage(content="# TECH DESIGN\ntasks", source="solution_architect")],
                    CancellationToken(),
                )
        self.assertFalse(limiter.locked(), "the crashed turn must have released its slot")


class BuildTeamWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="looper_limiter_wiring_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_env_var_creates_one_shared_semaphore(self) -> None:
        with mock.patch.dict(os.environ, {"LOOPER_MAX_PARALLEL_SESSIONS": "2"}):
            _, agents = build_team(self.workspace, agent_cls=ScriptedClaudeCodeAgent)
        limiters = {id(a._session_limiter) for a in agents.values()}
        self.assertEqual(len(limiters), 1, "every agent must share the same semaphore")
        self.assertIsInstance(agents["frontend_engineer"]._session_limiter, asyncio.Semaphore)

    def test_unset_env_var_means_no_limiter(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOOPER_MAX_PARALLEL_SESSIONS", None)
            _, agents = build_team(self.workspace, agent_cls=ScriptedClaudeCodeAgent)
        self.assertIsNone(agents["frontend_engineer"]._session_limiter)


if __name__ == "__main__":
    unittest.main()
