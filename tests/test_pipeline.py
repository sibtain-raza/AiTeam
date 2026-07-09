"""Pipeline orchestration tests, run against ScriptedClaudeCodeAgent.

These verify GraphFlow's routing/rework-loops/termination/checkpoint logic —
the part of this project that isn't the LLM's judgment — deterministically
and without spending any Claude Code session quota. They do NOT exercise the
real claude_agent_sdk integration; see README "Evidence this works" for how
that was verified (live, against real Claude Code sessions).

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import BaseChatMessage

from aiteam.pipeline import apply_turn_budget_from_architect, build_team, parse_turn_budget
from aiteam.runner import AgentFailure, FailFastMonitor, run_team

from .mock_agent import CRASH, ScriptedClaudeCodeAgent, set_script

PM_TEXT = "# PRD\n## North Star Goal\nBuild a thing.\n"
ARCHITECT_TEXT = "# TECH DESIGN\n## Task Breakdown\nT-1 [FE] thing\nT-2 [BE] thing\nT-3 [OPS] thing\n"
FE_TEXT = "# FE IMPLEMENTATION\nDone.\n"
BE_TEXT = "# BE IMPLEMENTATION\nDone.\n"
OPS_TEXT = "# OPS IMPLEMENTATION\nDone.\n"
QA_PASS_TEXT = "# QA REPORT\n## Verdict\nAll good.\nQA_PASS"
QA_FAIL_TEXT = "# QA REPORT\n## Defects\nD-1 something is wrong\nQA_FAIL"
UAT_APPROVED_TEXT = "# UAT REPORT\n## Final Summary\nShipped.\nUAT_APPROVED"
UAT_REJECTED_TEXT = "# UAT REPORT\n## Gaps\nMissed the point.\nUAT_REJECTED"
REPORTER_TEXT = "# FINAL DELIVERY REPORT\nDone.\n"

BASE_SCRIPT = {
    "product_manager": [PM_TEXT],
    "solution_architect": [ARCHITECT_TEXT],
    "frontend_engineer": [FE_TEXT],
    "backend_engineer": [BE_TEXT],
    "devops_engineer": [OPS_TEXT],
    "release_reporter": [REPORTER_TEXT],
}


def sources(messages: list) -> list[str]:
    return [m.source for m in messages if isinstance(m, BaseChatMessage)]


class PipelineOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="aiteam_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    async def _run(self, script: dict[str, list[str]]) -> TaskResult:
        set_script(script)
        team, _agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        result = None
        async for message in team.run_stream(task="Build a thing."):
            if isinstance(message, TaskResult):
                result = message
        assert result is not None
        return result

    async def test_happy_path_no_rework(self) -> None:
        """QA passes and UAT approves first try — every role runs exactly once."""
        result = await self._run(
            {**BASE_SCRIPT, "qa_engineer": [QA_PASS_TEXT], "uat_reviewer": [UAT_APPROVED_TEXT]}
        )
        src = sources(result.messages)
        self.assertEqual(src.count("qa_engineer"), 1)
        self.assertEqual(src.count("uat_reviewer"), 1)
        self.assertEqual(src.count("release_reporter"), 1)
        self.assertEqual(src.count("frontend_engineer"), 1)
        self.assertIn("release_reporter", src)

    async def test_qa_rework_loop_then_pass(self) -> None:
        """QA_FAIL once routes back to the engineers; QA_PASS on retry proceeds to UAT."""
        result = await self._run(
            {
                **BASE_SCRIPT,
                "qa_engineer": [QA_FAIL_TEXT, QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )
        src = sources(result.messages)
        self.assertEqual(src.count("qa_engineer"), 2)
        self.assertEqual(src.count("frontend_engineer"), 2, "FE should rework after QA_FAIL")
        self.assertEqual(src.count("backend_engineer"), 2)
        self.assertEqual(src.count("devops_engineer"), 2)
        self.assertEqual(src.count("uat_reviewer"), 1)
        self.assertEqual(src.count("release_reporter"), 1)
        # PM/Architect only ran once — the rework loop must not restart grooming/design.
        self.assertEqual(src.count("product_manager"), 1)
        self.assertEqual(src.count("solution_architect"), 1)

    async def test_qa_hard_fail_terminates_pipeline(self) -> None:
        """3 consecutive QA_FAILs hard-stop the run with PIPELINE_FAILED, never reaching UAT."""
        result = await self._run({**BASE_SCRIPT, "qa_engineer": [QA_FAIL_TEXT]})
        self.assertIn("PIPELINE_FAILED", result.stop_reason or "")
        src = sources(result.messages)
        self.assertEqual(src.count("qa_engineer"), 3)
        self.assertNotIn("uat_reviewer", src)
        self.assertNotIn("release_reporter", src)

    async def test_uat_rejection_loop_then_approve(self) -> None:
        """UAT_REJECTED once loops back to PM grooming; UAT_APPROVED on retry proceeds to done."""
        result = await self._run(
            {
                **BASE_SCRIPT,
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_REJECTED_TEXT, UAT_APPROVED_TEXT],
            }
        )
        src = sources(result.messages)
        self.assertEqual(src.count("product_manager"), 2, "PM should re-groom after UAT_REJECTED")
        self.assertEqual(src.count("uat_reviewer"), 2)
        self.assertEqual(src.count("release_reporter"), 1)
        # Confirm ordering: the final release_reporter comes after the second uat_reviewer call.
        self.assertLess(src.index("release_reporter"), len(src))

    async def test_checkpoint_resume_recovers_from_a_crashed_turn(self) -> None:
        """A turn that raises (simulating a real Claude Code session-limit
        crash) stops the run right after the PM's completed turn. Resuming
        from the last checkpoint on a fresh team/process should retry the
        crashed solution_architect turn — not re-run product_manager — and
        continue to completion. Mirrors what was verified live against the
        real pipeline (see README "Checkpoint & resume"): we killed a real
        run right after product_manager finished and confirmed resume
        picked up at solution_architect without re-running PM.

        Note: deliberately does NOT use `break` to simulate the interruption.
        GraphFlow's runtime is a producer/consumer queue; with near-instant
        scripted responses it races ahead of a paused consumer rather than
        pausing with it, so a `break`-based version of this test raced the
        whole pipeline to completion in the background before save_state()
        was ever called (confirmed via a debug run). A real exception is
        what actually halts the runtime, matching the crash we're modeling.
        """
        set_script(
            {
                **BASE_SCRIPT,
                "solution_architect": [CRASH],
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )

        team1, _agents1 = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        seen: list[str] = []
        checkpoint_state = None
        with self.assertRaises(Exception):
            async for message in team1.run_stream(task="Build a thing."):
                if isinstance(message, BaseChatMessage):
                    seen.append(message.source)
                    # Mirrors main.py: checkpoint after every completed turn.
                    checkpoint_state = await team1.save_state()
        self.assertEqual(seen, ["user", "product_manager"])
        assert checkpoint_state is not None

        # "Fix" the crash for the retry — like a human re-running after a
        # transient failure — then resume on a fresh team/agent instances.
        set_script(
            {**BASE_SCRIPT, "qa_engineer": [QA_PASS_TEXT], "uat_reviewer": [UAT_APPROVED_TEXT]}
        )
        team2, _agents2 = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        await team2.load_state(checkpoint_state)

        resumed: list[str] = []
        async for message in team2.run_stream(task=None):
            if isinstance(message, BaseChatMessage):
                resumed.append(message.source)

        self.assertNotIn(
            "product_manager", resumed, "resume must not re-run the already-completed PM turn"
        )
        self.assertEqual(resumed[0], "solution_architect", "resume should retry the crashed turn")
        self.assertIn("release_reporter", resumed)

    async def test_fail_fast_stops_the_run_when_one_of_several_parallel_engineers_crashes(self) -> None:
        """frontend_engineer, backend_engineer, and devops_engineer all run
        concurrently off the same architect fan-out. Live testing against
        the real pipeline found that when exactly one of them crashes (hit
        the turn cap) while the others are still mid-turn, the installed
        AutoGen version does NOT cleanly raise that failure out of
        `team.run_stream()` — it degrades into an unhandled internal
        message-routing error in a background asyncio task, and the run
        sits reporting "running" forever (see runner.py's module docstring
        and CLAUDE.md). This is the regression test for the fix: `run_team`
        must detect the crash via the on_event hook (independent of
        GraphFlow's own propagation) and raise AgentFailure promptly rather
        than hang — `asyncio.wait_for` below turns a reintroduced hang into
        a fast, clear test failure instead of a stuck test run.
        """
        set_script(
            {
                **BASE_SCRIPT,
                "frontend_engineer": [CRASH],
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )
        monitor = FailFastMonitor()
        team, _agents = build_team(
            self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent, on_event=monitor.on_event
        )

        async def on_message(_message: BaseChatMessage) -> None:
            pass

        with self.assertRaises(AgentFailure) as ctx:
            await asyncio.wait_for(run_team(team, "Build a thing.", on_message, monitor), timeout=10)
        self.assertEqual(ctx.exception.source, "frontend_engineer")


if __name__ == "__main__":
    unittest.main()
