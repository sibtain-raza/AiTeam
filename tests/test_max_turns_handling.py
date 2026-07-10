"""Unit tests for ClaudeCodeAgent._apply_verdict_safety_net().

Regression coverage for a real production incident: two runs of
"build a url shortner with custom alies and click analytics" both died
after solution_architect because one of FE/BE/OPS hit its 20-turn budget
mid-task, and ClaudeCodeAgent treated `error_max_turns` identically to a
genuine crash — killing the whole pipeline instead of returning partial
work. Fixed in two parts:
  1. on_messages() no longer raises on error_max_turns specifically (SDK
     -facing; not exercised here — see the live check run when this was
     fixed, and tests/test_pipeline.py's crash-recovery test for the
     already-covered "raise on other errors" path).
  2. _apply_verdict_safety_net() guarantees QA/UAT always end in a
     routable verdict token, even from truncated/partial text — that's
     pure text logic and is what these tests cover directly.

These construct a real ClaudeCodeAgent and call the method directly — no
Claude Code session is ever started, so they're free and instant.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path

from looper.claude_code_agent import ClaudeCodeAgent


def make_agent(name: str) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        name=name,
        description="test",
        system_prompt="test",
        cwd=Path(tempfile.gettempdir()) / "looper_verdict_safety_test",
    )


class VerdictSafetyNetTests(unittest.TestCase):
    def test_qa_valid_pass_is_unchanged(self) -> None:
        agent = make_agent("qa_engineer")
        text = "# QA REPORT\n## Verdict\nAll good.\nQA_PASS"
        self.assertEqual(agent._apply_verdict_safety_net(text), text)

    def test_qa_valid_fail_is_unchanged(self) -> None:
        agent = make_agent("qa_engineer")
        text = "# QA REPORT\n## Defects\nD-1 ...\nQA_FAIL"
        self.assertEqual(agent._apply_verdict_safety_net(text), text)

    def test_qa_truncated_text_forces_qa_fail(self) -> None:
        """The real-world case: max_turns hit mid-report, no verdict line at all."""
        agent = make_agent("qa_engineer")
        truncated = "# QA REPORT\n## Traceability\nAC-1 | reverse.go | still checking..."
        result = agent._apply_verdict_safety_net(truncated)
        self.assertTrue(result.endswith("QA_FAIL"))
        self.assertIn("INCOMPLETE", result)
        self.assertIn(truncated, result, "original partial text must be preserved, not discarded")

    def test_qa_mentions_pass_mid_text_but_not_as_last_line_still_fails_safe(self) -> None:
        """Guards against the exact bug verdict_is()'s last-line matching
        exists to prevent: a token mentioned in prose, not as the real verdict."""
        agent = make_agent("qa_engineer")
        text = "# QA REPORT\nIf all criteria hold this would be QA_PASS, but I ran out of turns before confirming AC-9."
        result = agent._apply_verdict_safety_net(text)
        self.assertTrue(result.endswith("QA_FAIL"))

    def test_uat_valid_approved_is_unchanged(self) -> None:
        agent = make_agent("uat_reviewer")
        text = "# UAT REPORT\n## Final Summary\nShipped.\nUAT_APPROVED"
        self.assertEqual(agent._apply_verdict_safety_net(text), text)

    def test_uat_truncated_text_forces_uat_rejected(self) -> None:
        agent = make_agent("uat_reviewer")
        truncated = "# UAT REPORT\n## Story Walkthrough\nChecking story 1..."
        result = agent._apply_verdict_safety_net(truncated)
        self.assertTrue(result.endswith("UAT_REJECTED"))

    def test_non_verdict_roles_are_never_modified(self) -> None:
        for name in ("product_manager", "solution_architect", "frontend_engineer", "backend_engineer", "devops_engineer", "release_reporter"):
            agent = make_agent(name)
            text = "# SOME REPORT\nincomplete, no verdict token, doesn't matter"
            self.assertEqual(
                agent._apply_verdict_safety_net(text), text, f"{name} must never get a verdict appended"
            )


if __name__ == "__main__":
    unittest.main()
