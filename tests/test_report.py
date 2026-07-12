"""Unit tests for defect parsing (artifacts.parse_defects) and the KPI
report (looper/report.py). Pure file/text logic — no SDK.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from looper.artifacts import parse_defects
from looper.pipeline import ARTIFACT_DIR_NAME, SDK_LOG_FILE_NAME
from looper.report import build_report, format_report
from looper.run_memory import record_run


class ParseDefectsTests(unittest.TestCase):
    def test_parses_pipe_table_style(self) -> None:
        # The style QA's own prompt specifies.
        text = (
            "## Defects\n"
            "**D-1 | BLOCKER | NFR-6/T-19 (CI/build gate) | [BE]**\n"
            "evidence...\n"
            "**D-2 | MAJOR | AC-12 | [FE]/[OPS]**\n"
        )
        defects = parse_defects(text)
        self.assertEqual(
            [(d.id, d.severity, d.owners) for d in defects],
            [("D-1", "BLOCKER", ("BE",)), ("D-2", "MAJOR", ("FE", "OPS"))],
        )

    def test_parses_bullet_reverification_style(self) -> None:
        # The style real rework-loop QA reports actually used.
        text = "- **D-3** MAJOR [BE] — **FIXED**. Tests now exist.\n"
        defects = parse_defects(text)
        self.assertEqual(defects[0].id, "D-3")
        self.assertEqual(defects[0].severity, "MAJOR")
        self.assertEqual(defects[0].owners, ("BE",))

    def test_dedupes_by_id_keeping_first(self) -> None:
        text = (
            "**D-1 | BLOCKER | ... | [BE]**\n"
            "later re-verification: - **D-1** BLOCKER [BE] — FIXED\n"
        )
        self.assertEqual(len(parse_defects(text)), 1)

    def test_mentions_without_severity_do_not_match(self) -> None:
        self.assertEqual(parse_defects("D-1 was fixed in the previous loop."), [])

    def test_clean_report_yields_nothing(self) -> None:
        self.assertEqual(parse_defects("# QA REPORT\nNo defects.\nQA_PASS"), [])


def _sdk_entry(agent: str, **overrides) -> dict:
    entry = {
        "agent": agent,
        "cost_usd": 1.0,
        "duration_ms": 60000,
        "max_turns": 20,
        "num_turns": None,
        "error": None,
        "response": "done",
    }
    entry.update(overrides)
    return entry


class BuildReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output_dir = Path(tempfile.mkdtemp(prefix="looper_report_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def _make_run(self, stamp: str, entries: list[dict]) -> None:
        workspace = self.output_dir / "workspace" / stamp
        log_dir = workspace / ARTIFACT_DIR_NAME
        log_dir.mkdir(parents=True)
        (log_dir / SDK_LOG_FILE_NAME).write_text(
            "".join(json.dumps(e) + "\n" for e in entries)
        )
        # Minimal memory record pointing at the workspace; per_agent stats
        # as summarize_run would compute them for these entries.
        record_run(
            self.output_dir,
            {
                "stamp": stamp,
                "goal": "test goal",
                "workspace": str(workspace),
                "total_cost_usd": sum(e["cost_usd"] for e in entries),
                "total_duration_ms": sum(e["duration_ms"] for e in entries),
                "qa_fail_rounds": 0,
                "per_agent": {
                    "backend_engineer": {"sessions": 1, "cost_usd": 1.0, "max_turns_hits": 1}
                },
            },
        )

    def test_aggregates_calibration_and_defects(self) -> None:
        self._make_run(
            "run-1",
            [
                _sdk_entry("backend_engineer", num_turns=20, max_turns=20,
                           error="hit max_turns before finishing"),
                _sdk_entry("frontend_engineer", num_turns=10, max_turns=20),
                _sdk_entry(
                    "qa_engineer",
                    response="**D-1 | BLOCKER | AC-1 | [BE]**\n**D-2 | MINOR | AC-2 | [FE]**\nQA_FAIL",
                ),
                _sdk_entry(
                    "qa_engineer",
                    # Rework pass re-verifies D-1: must not double-count.
                    response="- **D-1** BLOCKER [BE] — FIXED\nQA_PASS",
                ),
            ],
        )
        report = build_report(self.output_dir)

        self.assertEqual(len(report["runs"]), 1)
        cal = report["budget_calibration"]
        self.assertEqual(cal["sessions_measured"], 2)
        self.assertEqual(cal["exhausted"], 1)
        self.assertAlmostEqual(cal["avg_used_over_budget"], 0.75)  # (1.0 + 0.5) / 2
        self.assertEqual(report["defects_by_severity"], {"BLOCKER": 1, "MINOR": 1})
        self.assertEqual(report["defects_by_owner"], {"BE": 1, "FE": 1})

    def test_empty_memory_formats_gracefully(self) -> None:
        text = format_report(build_report(self.output_dir))
        self.assertIn("No recorded runs", text)

    def test_format_smoke(self) -> None:
        self._make_run("run-1", [_sdk_entry("backend_engineer", num_turns=5, max_turns=20)])
        text = format_report(build_report(self.output_dir))
        self.assertIn("KPI report", text)
        self.assertIn("backend_engineer", text)


if __name__ == "__main__":
    unittest.main()
