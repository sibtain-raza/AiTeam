"""Tests for the dynamic per-engineer turn budget: the architect estimates
how many tool-call turns FE/BE/OPS will each need (see ARCHITECT_PROMPT's
"Turn Budget Estimate" section), and pipeline.py applies that estimate to
override the static ENGINEER_MAX_TURNS default once the architect's TECH
DESIGN actually exists — before which the real complexity of the task isn't
known. Exists because a fixed 20-turn budget starved real production runs
(see test_max_turns_handling.py and CLAUDE.md for the incident this and the
error_max_turns fix both trace back to).

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from autogen_agentchat.messages import TextMessage

from looper.pipeline import (
    ARTIFACT_DIR_NAME,
    MAX_ENGINEER_TURNS,
    MIN_ENGINEER_TURNS,
    apply_turn_budget_from_architect,
    build_team,
    parse_turn_budget,
    reapply_turn_budget_on_resume,
)

from .mock_agent import ScriptedClaudeCodeAgent, set_script

ARCHITECT_TEXT_WITH_BUDGET = (
    "# TECH DESIGN\n"
    "## Task Breakdown\n"
    "T-1 [FE] three pages\n"
    "T-2 [BE] five endpoints plus auth\n"
    "T-3 [OPS] docker + ci\n"
    "## Turn Budget Estimate\n"
    "FE: 18 — three pages, moderate client-side validation\n"
    "BE: 30 — five endpoints, DB migrations, auth middleware\n"
    "OPS: 14 — two Dockerfiles, compose, GitHub Actions CI\n"
)


class ParseTurnBudgetTests(unittest.TestCase):
    def test_parses_all_three_roles(self) -> None:
        self.assertEqual(
            parse_turn_budget(ARCHITECT_TEXT_WITH_BUDGET), {"FE": 18, "BE": 30, "OPS": 14}
        )

    def test_missing_section_returns_empty(self) -> None:
        self.assertEqual(parse_turn_budget("# TECH DESIGN\nno budget section here\n"), {})

    def test_partial_section_returns_only_parsed_roles(self) -> None:
        text = "## Turn Budget Estimate\nFE: 20 — reason\n(BE and OPS estimates omitted by mistake)\n"
        self.assertEqual(parse_turn_budget(text), {"FE": 20})

    def test_clamps_below_minimum(self) -> None:
        text = "## Turn Budget Estimate\nFE: 1 — trivial\nBE: 2 — trivial\nOPS: 3 — trivial\n"
        result = parse_turn_budget(text)
        self.assertEqual(result, {"FE": MIN_ENGINEER_TURNS, "BE": MIN_ENGINEER_TURNS, "OPS": MIN_ENGINEER_TURNS})

    def test_zero_is_not_clamped_up_it_signals_skip(self) -> None:
        """A budget of exactly 0 means "no tasks assigned to this role" (see
        ARCHITECT_PROMPT) and must survive parsing as 0 — ClaudeCodeAgent
        .on_messages() reads `_max_turns == 0` as "skip this engineer's
        session entirely". Clamping it up to MIN_ENGINEER_TURNS like any
        other low estimate would silently turn every "nothing to do" signal
        back into a real, wasted session."""
        text = "## Turn Budget Estimate\nFE: 15 — normal\nBE: 0 — no server-side logic needed\nOPS: 0 — nothing to deploy\n"
        self.assertEqual(parse_turn_budget(text), {"FE": 15, "BE": 0, "OPS": 0})

    def test_clamps_above_maximum(self) -> None:
        text = "## Turn Budget Estimate\nFE: 500 — way overestimated\nBE: 9999 — way overestimated\nOPS: 60 — way overestimated\n"
        result = parse_turn_budget(text)
        self.assertEqual(result, {"FE": MAX_ENGINEER_TURNS, "BE": MAX_ENGINEER_TURNS, "OPS": MAX_ENGINEER_TURNS})

    def test_is_insensitive_to_surrounding_whitespace_and_dashes(self) -> None:
        text = "##Turn Budget Estimate\n  FE:22\nBE : 25 - db work\nOPS:10\n"
        self.assertEqual(parse_turn_budget(text), {"FE": 22, "BE": 25, "OPS": 10})


class ApplyTurnBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_budget_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    def _agents(self):
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        return agents

    def test_ignores_messages_from_other_sources(self) -> None:
        agents = self._agents()
        before = agents["frontend_engineer"]._max_turns
        message = TextMessage(content=ARCHITECT_TEXT_WITH_BUDGET, source="product_manager")
        applied = apply_turn_budget_from_architect(message, agents)
        self.assertEqual(applied, {})
        self.assertEqual(agents["frontend_engineer"]._max_turns, before)

    def test_applies_parsed_budget_to_the_right_agents(self) -> None:
        agents = self._agents()
        message = TextMessage(content=ARCHITECT_TEXT_WITH_BUDGET, source="solution_architect")
        applied = apply_turn_budget_from_architect(message, agents)
        self.assertEqual(applied, {"FE": 18, "BE": 30, "OPS": 14})
        self.assertEqual(agents["frontend_engineer"]._max_turns, 18)
        self.assertEqual(agents["backend_engineer"]._max_turns, 30)
        self.assertEqual(agents["devops_engineer"]._max_turns, 14)

    def test_does_not_touch_unrelated_agents(self) -> None:
        agents = self._agents()
        qa_before = agents["qa_engineer"]._max_turns
        pm_before = agents["product_manager"]._max_turns
        apply_turn_budget_from_architect(
            TextMessage(content=ARCHITECT_TEXT_WITH_BUDGET, source="solution_architect"), agents
        )
        self.assertEqual(agents["qa_engineer"]._max_turns, qa_before)
        self.assertEqual(agents["product_manager"]._max_turns, pm_before)

    def test_applies_a_zero_budget_meaning_skip_this_engineer(self) -> None:
        agents = self._agents()
        text = (
            "# TECH DESIGN\n## Task Breakdown\nT-1 [FE] one page\n"
            "## Turn Budget Estimate\nFE: 12 — one page\nBE: 0 — no server-side logic\nOPS: 0 — nothing to deploy\n"
        )
        applied = apply_turn_budget_from_architect(
            TextMessage(content=text, source="solution_architect"), agents
        )
        self.assertEqual(applied, {"FE": 12, "BE": 0, "OPS": 0})
        self.assertEqual(agents["frontend_engineer"]._max_turns, 12)
        self.assertEqual(agents["backend_engineer"]._max_turns, 0)
        self.assertEqual(agents["devops_engineer"]._max_turns, 0)

    def test_no_budget_section_leaves_defaults_unchanged(self) -> None:
        agents = self._agents()
        before = agents["frontend_engineer"]._max_turns
        applied = apply_turn_budget_from_architect(
            TextMessage(content="# TECH DESIGN\nno budget section\n", source="solution_architect"), agents
        )
        self.assertEqual(applied, {})
        self.assertEqual(agents["frontend_engineer"]._max_turns, before)


class ReapplyTurnBudgetOnResumeTests(unittest.TestCase):
    """Backs the resume path in main.py: set_max_turns() overrides aren't
    captured by save_state()/load_state(), so a resumed run re-derives the
    budget by reading the architect's TECH DESIGN back from its on-disk
    pointer file. Reading from disk (rather than searching some specific
    engineer's replayed `_history`, the previous approach) is what this
    covers — a real, reproduced race showed the history-based version could
    silently find nothing: `_history` is only populated when `on_messages()`
    is actually called, and a checkpoint saved right after the architect's
    turn but before ANY engineer had been dispatched left every engineer's
    `_history` without the architect's message at all, even though
    GraphFlow's own resume correctly re-ran all three."""

    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_budget_resume_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    def _write_design(self, text: str) -> None:
        docs = self.workspace_dir / ARTIFACT_DIR_NAME
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "solution_architect.md").write_text(text)

    def test_reapplies_budget_from_the_pointer_file(self) -> None:
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        self._write_design(ARCHITECT_TEXT_WITH_BUDGET)

        applied = reapply_turn_budget_on_resume(self.workspace_dir, agents)

        self.assertEqual(applied, {"FE": 18, "BE": 30, "OPS": 14})
        self.assertEqual(agents["frontend_engineer"]._max_turns, 18)
        self.assertEqual(agents["backend_engineer"]._max_turns, 30)
        self.assertEqual(agents["devops_engineer"]._max_turns, 14)

    def test_works_even_when_no_engineer_history_has_the_design(self) -> None:
        """The exact race this replaces the old approach for: none of the
        engineers' _history contains the architect's message (on_messages()
        was never called for any of them on this checkpoint), yet the
        pointer file — written unconditionally the instant the architect's
        turn completed — still has it."""
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        for name in ("frontend_engineer", "backend_engineer", "devops_engineer"):
            self.assertEqual(agents[name]._history, [])
        self._write_design(ARCHITECT_TEXT_WITH_BUDGET)

        applied = reapply_turn_budget_on_resume(self.workspace_dir, agents)
        self.assertEqual(applied, {"FE": 18, "BE": 30, "OPS": 14})

    def test_no_pointer_file_is_a_no_op(self) -> None:
        """Architect hasn't completed a turn on this checkpoint yet — must
        not raise, must not touch any agent's budget."""
        _team, agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        before = agents["frontend_engineer"]._max_turns
        applied = reapply_turn_budget_on_resume(self.workspace_dir, agents)
        self.assertEqual(applied, {})
        self.assertEqual(agents["frontend_engineer"]._max_turns, before)


if __name__ == "__main__":
    unittest.main()
