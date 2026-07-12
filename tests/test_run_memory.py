"""Unit tests for cross-run long-term memory (looper/run_memory.py).

Pure file/text logic over a synthetic SDK interaction log — no SDK, no
mock agent. Also covers the build_team() `architect_addendum` seam the
calibration hint is injected through.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from looper.pipeline import ARTIFACT_DIR_NAME, SDK_LOG_FILE_NAME, build_team
from looper.run_memory import (
    calibration_hint,
    load_runs,
    memory_path,
    record_run,
    summarize_run,
)
from tests.mock_agent import ScriptedClaudeCodeAgent


def _write_sdk_log(workspace: Path, entries: list[dict]) -> None:
    log_dir = workspace / ARTIFACT_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / SDK_LOG_FILE_NAME).write_text(
        "".join(json.dumps(e) + "\n" for e in entries)
    )


def _entry(agent: str, cost: float = 1.0, duration: int = 1000, error: str | None = None,
           response: str = "done", max_turns: int = 20) -> dict:
    return {
        "agent": agent,
        "cost_usd": cost,
        "duration_ms": duration,
        "error": error,
        "response": response,
        "max_turns": max_turns,
    }


class SummarizeRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="looper_memory_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_aggregates_cost_hits_and_qa_fail_rounds(self) -> None:
        _write_sdk_log(self.tmp, [
            _entry("product_manager", cost=0.2),
            _entry("backend_engineer", cost=2.0, error="hit max_turns before finishing", max_turns=28),
            _entry("qa_engineer", cost=1.5, response="# QA REPORT\n...\nQA_FAIL"),
            _entry("backend_engineer", cost=1.4, max_turns=28),
            _entry("qa_engineer", cost=0.5, response="# QA REPORT\n...\nQA_PASS"),
        ])
        record = summarize_run(self.tmp, "stamp-1", "a goal", "done")
        self.assertAlmostEqual(record["total_cost_usd"], 5.6)
        self.assertEqual(record["qa_fail_rounds"], 1)
        be = record["per_agent"]["backend_engineer"]
        self.assertEqual(be["sessions"], 2)
        self.assertEqual(be["max_turns_hits"], 1)
        self.assertEqual(be["last_max_turns"], 28)

    def test_missing_log_produces_zeroed_record(self) -> None:
        record = summarize_run(self.tmp, "stamp-2", "a goal", "done")
        self.assertEqual(record["total_cost_usd"], 0)
        self.assertEqual(record["per_agent"], {})


class RecordAndLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output_dir = Path(tempfile.mkdtemp(prefix="looper_memory_out_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_roundtrip(self) -> None:
        record_run(self.output_dir, {"stamp": "a", "goal": "g1"})
        record_run(self.output_dir, {"stamp": "b", "goal": "g2"})
        runs = load_runs(self.output_dir)
        self.assertEqual([r["stamp"] for r in runs], ["a", "b"])
        self.assertTrue(memory_path(self.output_dir).exists())

    def test_same_stamp_replaces_not_duplicates(self) -> None:
        """A crashed-then-resumed run finishing must supersede any earlier
        record for the same stamp, not double-count it."""
        record_run(self.output_dir, {"stamp": "a", "goal": "g", "total_cost_usd": 1})
        record_run(self.output_dir, {"stamp": "a", "goal": "g", "total_cost_usd": 5})
        runs = load_runs(self.output_dir)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["total_cost_usd"], 5)

    def test_load_from_empty_dir(self) -> None:
        self.assertEqual(load_runs(self.output_dir), [])


class CalibrationHintTests(unittest.TestCase):
    def test_no_history_means_no_hint(self) -> None:
        self.assertIsNone(calibration_hint([]))

    def test_no_hits_means_no_hint(self) -> None:
        runs = [{"per_agent": {"backend_engineer": {"sessions": 2, "max_turns_hits": 0}}}]
        self.assertIsNone(calibration_hint(runs))

    def test_hits_produce_a_hint_naming_the_worst_agent(self) -> None:
        runs = [{
            "per_agent": {
                "frontend_engineer": {"sessions": 1, "max_turns_hits": 1},
                "backend_engineer": {"sessions": 2, "max_turns_hits": 2},
                "qa_engineer": {"sessions": 2, "max_turns_hits": 2},  # not an engineer: ignored
            }
        }]
        hint = calibration_hint(runs)
        self.assertIsNotNone(hint)
        self.assertIn("3 of 3 engineer sessions", hint)
        self.assertIn("backend_engineer", hint)

    def test_window_limits_how_far_back_it_looks(self) -> None:
        old = {"per_agent": {"backend_engineer": {"sessions": 1, "max_turns_hits": 1}}}
        recent = {"per_agent": {"backend_engineer": {"sessions": 1, "max_turns_hits": 0}}}
        self.assertIsNone(calibration_hint([old, recent], window=1))


class ArchitectAddendumTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="looper_addendum_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_addendum_reaches_only_the_architect_prompt(self) -> None:
        _, agents = build_team(
            self.workspace,
            agent_cls=ScriptedClaudeCodeAgent,
            architect_addendum="CALIBRATION FROM PAST RUNS: size generously.",
        )
        self.assertIn("CALIBRATION FROM PAST RUNS", agents["solution_architect"]._system_prompt)
        self.assertNotIn("CALIBRATION FROM PAST RUNS", agents["product_manager"]._system_prompt)

    def test_no_addendum_leaves_prompt_unchanged(self) -> None:
        _, agents = build_team(self.workspace, agent_cls=ScriptedClaudeCodeAgent)
        self.assertNotIn("CALIBRATION FROM PAST RUNS", agents["solution_architect"]._system_prompt)


if __name__ == "__main__":
    unittest.main()
