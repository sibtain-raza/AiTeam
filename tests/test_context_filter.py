"""Unit tests for ClaudeCodeAgent's context_sources prompt filtering.

These construct real ClaudeCodeAgent instances and call _build_prompt()
directly — no Claude Code session is ever started, so they're as free and
instant as the orchestration tests.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path

from autogen_agentchat.messages import TextMessage

from aiteam.claude_code_agent import ClaudeCodeAgent


def make_agent(
    context_sources: dict[str, str] | None,
    pointer_files: dict[str, Path] | None = None,
) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        name="test_agent",
        description="test",
        system_prompt="test",
        cwd=Path(tempfile.gettempdir()) / "aiteam_ctx_filter_test",
        context_sources=context_sources,
        pointer_files=pointer_files,
    )


def msg(source: str, text: str) -> TextMessage:
    return TextMessage(content=text, source=source)


class ContextFilterTests(unittest.TestCase):
    def test_none_replays_everything(self) -> None:
        agent = make_agent(None)
        agent._history = [msg("user", "GOAL"), msg("a", "ONE"), msg("b", "TWO")]
        prompt = agent._build_prompt()
        for expected in ("GOAL", "ONE", "TWO"):
            self.assertIn(expected, prompt)

    def test_unlisted_sources_are_dropped(self) -> None:
        """An engineer must not see the other engineers' summaries."""
        agent = make_agent({"user": "all", "solution_architect": "latest", "qa_engineer": "latest"})
        agent._history = [
            msg("user", "GOAL"),
            msg("product_manager", "PRD-TEXT"),
            msg("solution_architect", "DESIGN-TEXT"),
            msg("backend_engineer", "BE-SUMMARY"),
            msg("devops_engineer", "OPS-SUMMARY"),
            msg("qa_engineer", "QA-REPORT"),
        ]
        prompt = agent._build_prompt()
        self.assertIn("GOAL", prompt)
        self.assertIn("DESIGN-TEXT", prompt)
        self.assertIn("QA-REPORT", prompt)
        self.assertNotIn("PRD-TEXT", prompt)
        self.assertNotIn("BE-SUMMARY", prompt)
        self.assertNotIn("OPS-SUMMARY", prompt)

    def test_latest_keeps_only_most_recent_per_source(self) -> None:
        """On loops, a superseded PRD/QA report must not be replayed alongside
        its replacement."""
        agent = make_agent({"user": "all", "qa_engineer": "latest"})
        agent._history = [
            msg("user", "GOAL"),
            msg("qa_engineer", "QA-REPORT-ROUND-1"),
            msg("qa_engineer", "QA-REPORT-ROUND-2"),
        ]
        prompt = agent._build_prompt()
        self.assertIn("QA-REPORT-ROUND-2", prompt)
        self.assertNotIn("QA-REPORT-ROUND-1", prompt)

    def test_all_keeps_every_message_from_that_source(self) -> None:
        agent = make_agent({"user": "all"})
        agent._history = [msg("user", "GOAL-ONE"), msg("user", "GOAL-TWO")]
        prompt = agent._build_prompt()
        self.assertIn("GOAL-ONE", prompt)
        self.assertIn("GOAL-TWO", prompt)

    def test_chronological_order_is_preserved(self) -> None:
        agent = make_agent({"user": "all", "product_manager": "latest", "solution_architect": "latest"})
        agent._history = [
            msg("user", "GOAL"),
            msg("product_manager", "PRD"),
            msg("solution_architect", "DESIGN"),
        ]
        prompt = agent._build_prompt()
        self.assertLess(prompt.index("GOAL"), prompt.index("PRD"))
        self.assertLess(prompt.index("PRD"), prompt.index("DESIGN"))


class PointerFileTests(unittest.TestCase):
    """The pointer mechanism: unchanged-on-disk artifacts replay as a
    one-line pointer instead of full text — but only when the artifact did
    NOT arrive this turn (first sight is always full inline) and the file
    actually exists (missing file falls back to inline)."""

    def setUp(self) -> None:
        self.docs = Path(tempfile.mkdtemp(prefix="aiteam_ptr_test_"))
        self.design_file = self.docs / "solution_architect.md"
        self.context = {"user": "all", "solution_architect": "latest", "qa_engineer": "latest"}
        self.pointers = {"solution_architect": self.design_file}
        self.history = [
            msg("user", "GOAL"),
            msg("solution_architect", "DESIGN-FULL-TEXT"),
            msg("qa_engineer", "QA-REPORT"),
        ]

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.docs, ignore_errors=True)

    def test_pointer_replaces_unchanged_artifact_on_rework_turn(self) -> None:
        self.design_file.write_text("DESIGN-FULL-TEXT")
        agent = make_agent(self.context, self.pointers)
        agent._history = self.history
        # Rework turn: only the QA report arrived this turn.
        prompt = agent._build_prompt(new_sources={"qa_engineer"})
        self.assertNotIn("DESIGN-FULL-TEXT", prompt)
        self.assertIn(str(self.design_file), prompt)
        self.assertIn("QA-REPORT", prompt)  # new arrivals always inline

    def test_first_sight_is_always_full_inline(self) -> None:
        self.design_file.write_text("DESIGN-FULL-TEXT")
        agent = make_agent(self.context, self.pointers)
        agent._history = self.history
        # First pass: the design itself arrived this turn -> full text,
        # even though the file exists on disk.
        prompt = agent._build_prompt(new_sources={"solution_architect", "qa_engineer"})
        self.assertIn("DESIGN-FULL-TEXT", prompt)
        self.assertNotIn(str(self.design_file), prompt)

    def test_missing_file_falls_back_to_inline(self) -> None:
        # File never written (e.g. build_team used without main.py) ->
        # pointer would be a dead end, so full text is replayed instead.
        agent = make_agent(self.context, self.pointers)
        agent._history = self.history
        prompt = agent._build_prompt(new_sources={"qa_engineer"})
        self.assertIn("DESIGN-FULL-TEXT", prompt)

    def test_no_delta_info_disables_pointers(self) -> None:
        self.design_file.write_text("DESIGN-FULL-TEXT")
        agent = make_agent(self.context, self.pointers)
        agent._history = self.history
        # _build_prompt() without new_sources (tests/direct use) must never
        # pointer-ize: without delta info, a pointer could hide an artifact
        # the agent has never seen.
        prompt = agent._build_prompt()
        self.assertIn("DESIGN-FULL-TEXT", prompt)


if __name__ == "__main__":
    unittest.main()
