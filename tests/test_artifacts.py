"""Unit tests for the artifact schema / message protocol (looper/artifacts.py).

Pure text logic — no SDK, no mock agent needed (same reasoning as
tests/test_max_turns_handling.py). Covers: conforming artifacts validate
clean, missing sections/title/verdict are reported, the zero-budget skip
message validates clean despite its different title and missing
role-specific sections, and BLOCKER escalation lines are extracted.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import unittest

from looper.artifacts import ARTIFACT_SPECS, classify, find_blockers, validate_artifact


def _sections(*names: str) -> str:
    return "\n".join(f"## {name}\ncontent\n" for name in names)


GOOD_PRD = "# PRD\n" + _sections(
    "North Star Goal",
    "Personas & Problem",
    "User Stories",
    "Acceptance Criteria",
    "Non-Functional Requirements",
    "Out of Scope",
    "Definition of Done",
    "Assumptions",
)

GOOD_QA_PASS = (
    "# QA REPORT\n"
    + _sections("Traceability", "Defects", "Verdict")
    + "\nQA_PASS\n"
)


class ValidateArtifactTests(unittest.TestCase):
    def test_conforming_prd_validates_clean(self) -> None:
        self.assertEqual(validate_artifact("product_manager", GOOD_PRD), [])

    def test_conforming_qa_report_validates_clean(self) -> None:
        self.assertEqual(validate_artifact("qa_engineer", GOOD_QA_PASS), [])

    def test_missing_section_is_reported(self) -> None:
        text = "# PRD\n" + _sections("North Star Goal", "User Stories")
        problems = validate_artifact("product_manager", text)
        self.assertTrue(any("Acceptance Criteria" in p for p in problems))
        self.assertTrue(any("Out of Scope" in p for p in problems))

    def test_missing_title_is_reported(self) -> None:
        problems = validate_artifact("solution_architect", _sections("Stack", "Architecture"))
        self.assertTrue(any("title" in p for p in problems))

    def test_missing_verdict_token_is_reported(self) -> None:
        text = "# QA REPORT\n" + _sections("Traceability", "Defects", "Verdict")
        problems = validate_artifact("qa_engineer", text + "\nAll good, passing.\n")
        self.assertTrue(any("last line" in p for p in problems))

    def test_section_match_is_prefix_and_case_insensitive(self) -> None:
        # The UAT prompt writes "## Gaps (if any)" — a "Gaps" requirement
        # must accept it; heading case must not matter either.
        text = (
            "# UAT REPORT\n"
            "## STORY WALKTHROUGH\nok\n"
            "## Gaps (if any)\nnone\n"
            "## Final Summary\nok\n"
            "\nUAT_APPROVED\n"
        )
        self.assertEqual(validate_artifact("uat_reviewer", text), [])

    def test_skip_message_validates_clean_for_ops(self) -> None:
        """The zero-budget skip path emits the shared engineer skeleton
        under a '# DEVOPS_ENGINEER IMPLEMENTATION' title with no Runbook —
        that is a legitimate protocol message, not drift."""
        text = (
            "# DEVOPS_ENGINEER IMPLEMENTATION\n\n"
            "No tasks were assigned to this role in the TECH DESIGN "
            "(turn budget: 0) — skipped without starting a Claude Code session.\n\n"
            "## Files Written\n(none)\n\n"
            "## Tasks Completed\n(none)\n\n"
            "## Assumptions\nThe architect's Task Breakdown assigned zero tasks.\n"
        )
        self.assertEqual(validate_artifact("devops_engineer", text), [])

    def test_real_ops_artifact_requires_runbook(self) -> None:
        text = "# OPS IMPLEMENTATION\n" + _sections("Files Written", "Tasks Completed", "Assumptions")
        problems = validate_artifact("devops_engineer", text)
        self.assertTrue(any("Runbook" in p for p in problems))

    def test_unknown_source_validates_clean(self) -> None:
        self.assertEqual(validate_artifact("user", "build me a website"), [])

    def test_optional_sections_are_not_required(self) -> None:
        # Engineers only emit "Defects Fixed" on rework loops — its absence
        # on a first pass must not be a problem.
        text = "# FE IMPLEMENTATION\n" + _sections("Files Written", "Tasks Completed", "Assumptions")
        self.assertEqual(validate_artifact("frontend_engineer", text), [])


class ClassifyTests(unittest.TestCase):
    def test_all_nine_roles_have_specs_and_kinds(self) -> None:
        self.assertEqual(len(ARTIFACT_SPECS), 9)
        self.assertEqual(classify("product_manager"), "PRD")
        self.assertEqual(classify("scope_validator"), "SCOPE_REVIEW")
        self.assertEqual(classify("qa_engineer"), "QA_REPORT")

    def test_unknown_source_has_no_kind(self) -> None:
        self.assertIsNone(classify("user"))


class FindBlockersTests(unittest.TestCase):
    def test_extracts_blocker_lines(self) -> None:
        text = (
            "# BE IMPLEMENTATION\n"
            "## Files Written\n...\n"
            "BLOCKER: the design's auth contract names an endpoint the PRD scoped out. "
            "Proceeding with the PRD's version.\n"
            "- **BLOCKER**: port 4000 conflicts with the FE dev server per the config table.\n"
        )
        blockers = find_blockers(text)
        self.assertEqual(len(blockers), 2)
        self.assertIn("auth contract", blockers[0])
        self.assertIn("port 4000", blockers[1])

    def test_prose_mentioning_blockers_does_not_match(self) -> None:
        text = "No BLOCKER or MAJOR defects remain open; D-1 was a blocker: now fixed."
        self.assertEqual(find_blockers(text), [])


if __name__ == "__main__":
    unittest.main()
