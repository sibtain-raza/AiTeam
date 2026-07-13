"""Unit tests for ClaudeCodeAgent's no-progress stall detection.

An engineer rework turn that produces zero workspace changes burns a full
paid Claude Code session AND one of the (max 3) QA rework iterations, and
before this feature the only evidence was QA re-discovering the same
defects a loop later. With `stall_watch_paths` set (FE/BE/OPS only, wired
in build_team()), the agent snapshots its watched dirs before and after
each real session and prepends an explicit "ZERO file changes" notice to
the turn's message when nothing changed — advisory, like artifact
validation: it annotates the message QA reads, nothing routes on it.

These patch `claude_agent_sdk.query` (imported by name into
claude_code_agent.py) with a fake that optionally touches the filesystem
mid-session — deliberately NOT covered by ScriptedClaudeCodeAgent
(mock_agent.py), which overrides on_messages() wholesale and therefore
never runs the snapshot/diff code at all (same reasoning as
test_skip_idle_engineer.py).

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

STALL_MARKER = "ZERO file changes"


def _success_result(text: str = "done") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=3,
        session_id="test-session",
        total_cost_usd=0.05,
        result=text,
    )


def make_agent(name: str, cwd: Path, **kwargs) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        name=name,
        description="test",
        system_prompt="test",
        cwd=cwd,
        max_turns=12,
        **kwargs,
    )


def design_message() -> list[TextMessage]:
    return [TextMessage(content="# TECH DESIGN\nT-1 [BE] one endpoint", source="solution_architect")]


class StallDetectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_stall_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    async def test_unchanged_workspace_gets_the_stall_notice_and_event(self) -> None:
        events: list[str] = []

        async def collect(source, event_type, detail, extra) -> None:
            events.append(event_type)

        agent = make_agent(
            "backend_engineer",
            self.workspace_dir,
            stall_watch_paths=[self.workspace_dir],
            on_event=collect,
        )

        async def _writes_nothing(prompt, options):
            yield _success_result("I fixed all the defects.")

        with mock.patch.object(cca, "query", _writes_nothing):
            response = await agent.on_messages(design_message(), CancellationToken())

        text = response.chat_message.to_text()
        self.assertIn(STALL_MARKER, text)
        self.assertIn("I fixed all the defects.", text, "the session's own text must be kept, not replaced")
        self.assertIn("stall_detected", events)

    async def test_a_written_file_suppresses_the_notice(self) -> None:
        agent = make_agent(
            "backend_engineer", self.workspace_dir, stall_watch_paths=[self.workspace_dir]
        )

        async def _writes_a_file(prompt, options):
            (self.workspace_dir / "app.py").write_text("print('hi')\n")
            yield _success_result()

        with mock.patch.object(cca, "query", _writes_a_file):
            response = await agent.on_messages(design_message(), CancellationToken())

        self.assertNotIn(STALL_MARKER, response.chat_message.to_text())

    async def test_a_deleted_file_suppresses_the_notice(self) -> None:
        """Deletion is a real change — guards the snapshot comparison
        against only noticing additions/modifications."""
        (self.workspace_dir / "stale.py").write_text("old\n")
        agent = make_agent(
            "backend_engineer", self.workspace_dir, stall_watch_paths=[self.workspace_dir]
        )

        async def _deletes_a_file(prompt, options):
            (self.workspace_dir / "stale.py").unlink()
            yield _success_result()

        with mock.patch.object(cca, "query", _deletes_a_file):
            response = await agent.on_messages(design_message(), CancellationToken())

        self.assertNotIn(STALL_MARKER, response.chat_message.to_text())

    async def test_changes_only_inside_an_excluded_dir_still_count_as_a_stall(self) -> None:
        """The OPS case: FE/BE write frontend//backend/ concurrently during
        the fan-out, so a root-watching agent must not let a sibling's diff
        mask its own idle turn."""
        sibling_dir = self.workspace_dir / "frontend"
        sibling_dir.mkdir()
        agent = make_agent(
            "devops_engineer",
            self.workspace_dir,
            stall_watch_paths=[self.workspace_dir],
            stall_exclude_paths=[sibling_dir],
        )

        async def _sibling_writes(prompt, options):
            (sibling_dir / "index.html").write_text("<h1>hi</h1>\n")
            yield _success_result()

        with mock.patch.object(cca, "query", _sibling_writes):
            response = await agent.on_messages(design_message(), CancellationToken())

        self.assertIn(STALL_MARKER, response.chat_message.to_text())

    async def test_disabled_by_default(self) -> None:
        """No stall_watch_paths (the default for every role except FE/BE/OPS,
        and for every pre-existing constructor call site) means no snapshot,
        no notice — even for a turn that changes nothing."""
        agent = make_agent("qa_engineer", self.workspace_dir)

        async def _writes_nothing(prompt, options):
            yield _success_result("Report only.\nQA_PASS")

        with mock.patch.object(cca, "query", _writes_nothing):
            response = await agent.on_messages(design_message(), CancellationToken())

        self.assertNotIn(STALL_MARKER, response.chat_message.to_text())


if __name__ == "__main__":
    unittest.main()
