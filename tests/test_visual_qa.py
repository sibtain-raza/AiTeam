"""Tests for the architect-gated Visual QA budget: the architect decides
whether a goal's frontend has enough custom visual/animation design that a
Playwright-based render-and-screenshot pass is warranted (see
ARCHITECT_PROMPT's "Visual QA" section), and pipeline.py bumps
qa_engineer's turn budget accordingly — most goals get VISUAL_QA: NO and
QA's budget is untouched. Pure text/state logic, no SDK — the actual live
Playwright workflow (QA_PROMPT step 4) needs live verification the same
way the error_max_turns SDK-transport behavior did; a mock can't exercise
a real browser.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from autogen_agentchat.messages import TextMessage

from looper.pipeline import (
    ARTIFACT_DIR_NAME,
    MAX_VISUAL_QA_EXTRA_TURNS,
    apply_visual_qa_budget,
    build_team,
    parse_visual_qa_extra_turns,
    reapply_turn_budget_on_resume,
)

from .mock_agent import ScriptedClaudeCodeAgent, set_script


class PersistentBrowserCacheTests(unittest.TestCase):
    """QA's Playwright browser download (see QA_PROMPT step 4) is expensive
    to repeat every run/container-restart — build_team(output_dir=...)
    points PLAYWRIGHT_BROWSERS_PATH at a location OUTSIDE the per-run
    workspace so it survives across runs, instead of relying on the host's
    own (often ephemeral, e.g. in a fresh Docker container) default cache
    dir. Only QA needs this; every other role's Bash/tool usage has no
    reason to know or care about it."""

    def setUp(self) -> None:
        self.output_dir = Path(tempfile.mkdtemp(prefix="looper_browser_cache_test_"))
        self.workspace_dir = self.output_dir / "workspace" / "stamp"

    def tearDown(self) -> None:
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_qa_gets_a_persistent_cache_path_outside_the_workspace(self) -> None:
        _team, agents = build_team(
            self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent, output_dir=self.output_dir
        )
        expected = str(self.output_dir / ".playwright-browsers")
        self.assertEqual(agents["qa_engineer"]._extra_env.get("PLAYWRIGHT_BROWSERS_PATH"), expected)
        # Outside workspace/stamp/ specifically — a fresh run/stamp must
        # still find the SAME cache, not a per-run one.
        self.assertNotIn(str(self.workspace_dir), expected)

    def test_only_qa_gets_the_extra_env(self) -> None:
        _team, agents = build_team(
            self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent, output_dir=self.output_dir
        )
        for role in (
            "product_manager",
            "scope_validator",
            "solution_architect",
            "frontend_engineer",
            "backend_engineer",
            "devops_engineer",
            "uat_reviewer",
            "release_reporter",
        ):
            self.assertEqual(
                agents[role]._extra_env, {}, f"{role} has no reason to know about Playwright's cache"
            )

    def test_no_output_dir_means_no_extra_env(self) -> None:
        """Backward-compatible default: existing callers that don't pass
        output_dir (every test in this suite, historically) must see no
        behavior change."""
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        self.assertEqual(agents["qa_engineer"]._extra_env, {})

DESIGN_WITH_VISUAL_QA = (
    "# TECH DESIGN\n"
    "## Task Breakdown\n"
    "T-1 [FE] hero + inventory + inquiry form\n"
    "## Turn Budget Estimate\n"
    "FE: 38 — cinematic hero, scroll reveals, custom hooks\n"
    "BE: 20 — inquiry API\n"
    "OPS: 15 — docker + ci\n"
    "## Visual QA\n"
    "VISUAL_QA: YES: 15 — bespoke animation/interaction design the goal explicitly demands\n"
)

DESIGN_WITHOUT_VISUAL_QA = (
    "# TECH DESIGN\n"
    "## Turn Budget Estimate\n"
    "FE: 12 — a simple form\n"
    "## Visual QA\n"
    "VISUAL_QA: NO — standard internal form, no custom animation requested\n"
)


class ParseVisualQaExtraTurnsTests(unittest.TestCase):
    def test_parses_yes_with_a_number(self) -> None:
        self.assertEqual(parse_visual_qa_extra_turns(DESIGN_WITH_VISUAL_QA), 15)

    def test_no_yields_zero(self) -> None:
        self.assertEqual(parse_visual_qa_extra_turns(DESIGN_WITHOUT_VISUAL_QA), 0)

    def test_missing_section_yields_zero(self) -> None:
        self.assertEqual(parse_visual_qa_extra_turns("# TECH DESIGN\nno visual qa line here\n"), 0)

    def test_yes_with_no_number_yields_zero(self) -> None:
        """Never silently grant extra turns without an explicit, parseable
        count — a malformed YES line degrades to "no extra budget", not to
        a guessed default."""
        text = "## Visual QA\nVISUAL_QA: YES — forgot the number\n"
        self.assertEqual(parse_visual_qa_extra_turns(text), 0)

    def test_is_case_and_whitespace_insensitive(self) -> None:
        text = "##Visual QA\n  visual_qa:yes:20  \n"
        self.assertEqual(parse_visual_qa_extra_turns(text), 20)

    def test_clamps_to_the_ceiling(self) -> None:
        text = "## Visual QA\nVISUAL_QA: YES: 999 — way overestimated\n"
        self.assertEqual(parse_visual_qa_extra_turns(text), MAX_VISUAL_QA_EXTRA_TURNS)

    def test_zero_is_a_valid_explicit_yes_value(self) -> None:
        text = "## Visual QA\nVISUAL_QA: YES: 0 — negligible extra work needed\n"
        self.assertEqual(parse_visual_qa_extra_turns(text), 0)


class ApplyVisualQaBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_visual_qa_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    def _agents(self):
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        return agents

    def test_bumps_qa_budget_on_top_of_the_given_base(self) -> None:
        agents = self._agents()
        message = TextMessage(content=DESIGN_WITH_VISUAL_QA, source="solution_architect")
        extra = apply_visual_qa_budget(message, agents, base_turns=20)
        self.assertEqual(extra, 15)
        self.assertEqual(agents["qa_engineer"].max_turns, 35)

    def test_visual_qa_no_leaves_budget_untouched(self) -> None:
        agents = self._agents()
        before = agents["qa_engineer"].max_turns
        message = TextMessage(content=DESIGN_WITHOUT_VISUAL_QA, source="solution_architect")
        extra = apply_visual_qa_budget(message, agents, base_turns=before)
        self.assertEqual(extra, 0)
        self.assertEqual(agents["qa_engineer"].max_turns, before)

    def test_ignores_messages_from_other_sources(self) -> None:
        agents = self._agents()
        before = agents["qa_engineer"].max_turns
        message = TextMessage(content=DESIGN_WITH_VISUAL_QA, source="qa_engineer")
        extra = apply_visual_qa_budget(message, agents, base_turns=before)
        self.assertEqual(extra, 0)
        self.assertEqual(agents["qa_engineer"].max_turns, before)

    def test_no_op_when_qa_engineer_missing_from_agents(self) -> None:
        message = TextMessage(content=DESIGN_WITH_VISUAL_QA, source="solution_architect")
        extra = apply_visual_qa_budget(message, {}, base_turns=20)
        self.assertEqual(extra, 0)


class ReapplyOnResumeIncludesVisualQaTests(unittest.TestCase):
    """reapply_turn_budget_on_resume() (see test_turn_budget.py for its
    core FE/BE/OPS coverage) must also carry QA's visual budget forward —
    it's the same in-memory-only set_max_turns() override, subject to the
    identical resume gap."""

    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_visual_qa_resume_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    def _write_design(self, text: str) -> None:
        docs = self.workspace_dir / ARTIFACT_DIR_NAME
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "solution_architect.md").write_text(text)

    def test_visual_qa_budget_is_reapplied_from_disk(self) -> None:
        set_script({})
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        before = agents["qa_engineer"].max_turns
        self._write_design(DESIGN_WITH_VISUAL_QA)

        applied = reapply_turn_budget_on_resume(self.workspace_dir, agents)

        self.assertEqual(applied.get("QA_VISUAL"), 15)
        self.assertEqual(agents["qa_engineer"].max_turns, before + 15)

    def test_visual_qa_no_is_not_reported(self) -> None:
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        self._write_design(DESIGN_WITHOUT_VISUAL_QA)

        applied = reapply_turn_budget_on_resume(self.workspace_dir, agents)

        self.assertNotIn("QA_VISUAL", applied)


if __name__ == "__main__":
    unittest.main()
