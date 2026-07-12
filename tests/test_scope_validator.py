"""Orchestration tests for the scope_validator gate (PM → validator →
architect, annotate-only). Scripted agents, no Claude Code sessions.

The validator has no conditional edges and no loop — the properties worth
guarding are: it runs between PM and architect on the first pass, it runs
AGAIN on a UAT re-scope loop (the re-groomed PRD passes back through the
same gate), it never blocks the pipeline, and its SCOPE REVIEW is routed
into the context of exactly the roles whose prompts consume it
(architect/QA/UAT — not the engineers).

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import BaseChatMessage

from looper.pipeline import build_team

from .mock_agent import ScriptedClaudeCodeAgent, set_script
from .test_pipeline import (
    BASE_SCRIPT,
    QA_PASS_TEXT,
    UAT_APPROVED_TEXT,
    UAT_REJECTED_TEXT,
    sources,
)


class ScopeValidatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_scope_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    async def _run(self, script: dict) -> tuple[TaskResult, dict]:
        set_script(script)
        team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        result = None
        async for message in team.run_stream(task="Build a thing."):
            if isinstance(message, TaskResult):
                result = message
        assert result is not None
        return result, agents

    async def test_validator_runs_between_pm_and_architect(self) -> None:
        result, _ = await self._run(
            {**BASE_SCRIPT, "qa_engineer": [QA_PASS_TEXT], "uat_reviewer": [UAT_APPROVED_TEXT]}
        )
        src = sources(result.messages)
        self.assertEqual(src.count("scope_validator"), 1)
        self.assertLess(src.index("product_manager"), src.index("scope_validator"))
        self.assertLess(src.index("scope_validator"), src.index("solution_architect"))

    async def test_validator_reruns_on_uat_rescope_loop(self) -> None:
        result, _ = await self._run(
            {
                **BASE_SCRIPT,
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_REJECTED_TEXT, UAT_APPROVED_TEXT],
            }
        )
        src = sources(result.messages)
        # Re-groomed PRD passes back through the same gate.
        self.assertEqual(src.count("product_manager"), 2)
        self.assertEqual(src.count("scope_validator"), 2)
        self.assertEqual(src.count("solution_architect"), 2)
        self.assertEqual(src.count("release_reporter"), 1)

    async def test_validator_does_not_rerun_on_qa_rework_loop(self) -> None:
        """The QA rework loop routes to engineers only — grooming and the
        scope gate must not re-run."""
        from .test_pipeline import QA_FAIL_TEXT

        result, _ = await self._run(
            {
                **BASE_SCRIPT,
                "qa_engineer": [QA_FAIL_TEXT, QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )
        src = sources(result.messages)
        self.assertEqual(src.count("scope_validator"), 1)
        self.assertEqual(src.count("qa_engineer"), 2)

    async def test_scope_review_reaches_the_roles_that_consume_it(self) -> None:
        """context_sources must route the SCOPE REVIEW to architect/QA/UAT
        (their prompts reference it) and NOT to the engineers (theirs
        don't — they get scope via the TECH DESIGN)."""
        _, agents = await self._run(
            {**BASE_SCRIPT, "qa_engineer": [QA_PASS_TEXT], "uat_reviewer": [UAT_APPROVED_TEXT]}
        )
        for role in ("solution_architect", "qa_engineer", "uat_reviewer"):
            self.assertIn(
                "scope_validator",
                agents[role]._context_sources,
                f"{role}'s prompt consumes the SCOPE REVIEW",
            )
        for role in ("frontend_engineer", "backend_engineer", "devops_engineer"):
            self.assertNotIn("scope_validator", agents[role]._context_sources)


if __name__ == "__main__":
    unittest.main()
