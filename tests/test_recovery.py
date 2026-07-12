"""Tests for autonomous crash recovery (looper/recovery.py + main.py's
auto-resume loop + runner.FailFastMonitor.reset()).

The parse/policy/lock pieces are pure logic. The loop itself is covered by
an end-to-end integration test that drives main.run() with scripted agents:
a mid-run crash, a zero-length recovery delay (patched policy — the real
one waits minutes/hours), and an automatic resume to completion, with no
manual --resume invocation anywhere.

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import functools
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from looper import main as main_module
from looper import recovery
from looper.pipeline import build_team
from looper.recovery import (
    FALLBACK_SESSION_LIMIT_WAIT_S,
    MAX_AUTO_WAIT_S,
    RESET_BUFFER_S,
    TRANSIENT_BACKOFF_S,
    acquire_run_lock,
    compute_retry_delay,
    parse_session_reset_wait,
    release_run_lock,
)
from looper.runner import FailFastMonitor
from tests.mock_agent import CRASH, ScriptedClaudeCodeAgent, set_script
from tests.test_pipeline import BASE_SCRIPT, QA_PASS_TEXT, UAT_APPROVED_TEXT

# The exact live-observed failure text this module was built from.
REAL_SESSION_LIMIT_ERROR = (
    "devops_engineer: You've hit your session limit · resets 3:30am (Asia/Calcutta)"
)


class ParseSessionResetWaitTests(unittest.TestCase):
    def test_parses_the_real_observed_format(self) -> None:
        # 2:00am IST → reset at 3:30am IST = 90 min away, plus buffer.
        now = datetime(2026, 7, 12, 2, 0, tzinfo=ZoneInfo("Asia/Calcutta"))
        wait = parse_session_reset_wait(REAL_SESSION_LIMIT_ERROR, now=now)
        self.assertEqual(wait, 90 * 60 + RESET_BUFFER_S)

    def test_past_time_means_tomorrow(self) -> None:
        # 4:00am IST, reset "3:30am" → tomorrow, 23.5h away.
        now = datetime(2026, 7, 12, 4, 0, tzinfo=ZoneInfo("Asia/Calcutta"))
        wait = parse_session_reset_wait(REAL_SESSION_LIMIT_ERROR, now=now)
        self.assertEqual(wait, 23.5 * 3600 + RESET_BUFFER_S)

    def test_pm_and_missing_minutes(self) -> None:
        now = datetime(2026, 7, 12, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        wait = parse_session_reset_wait("resets 4pm (America/New_York)", now=now)
        self.assertEqual(wait, 6 * 3600 + RESET_BUFFER_S)

    def test_12am_is_midnight(self) -> None:
        now = datetime(2026, 7, 12, 23, 0, tzinfo=ZoneInfo("UTC"))
        wait = parse_session_reset_wait("resets 12:00am (UTC)", now=now)
        self.assertEqual(wait, 3600 + RESET_BUFFER_S)

    def test_unknown_timezone_or_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_session_reset_wait("resets 3:30am (Not/AZone)"))
        self.assertIsNone(parse_session_reset_wait("some unrelated error"))


class ComputeRetryDelayTests(unittest.TestCase):
    def test_session_limit_uses_parsed_wait(self) -> None:
        now = datetime(2026, 7, 12, 2, 0, tzinfo=ZoneInfo("Asia/Calcutta"))
        delay = compute_retry_delay(REAL_SESSION_LIMIT_ERROR, attempt=0, now=now)
        self.assertEqual(delay, 90 * 60 + RESET_BUFFER_S)

    def test_session_limit_with_unparseable_time_uses_fallback(self) -> None:
        delay = compute_retry_delay("You've hit your session limit · resets soon", attempt=0)
        self.assertEqual(delay, FALLBACK_SESSION_LIMIT_WAIT_S)

    def test_session_limit_wait_is_capped(self) -> None:
        now = datetime(2026, 7, 12, 4, 0, tzinfo=ZoneInfo("Asia/Calcutta"))
        delay = compute_retry_delay(REAL_SESSION_LIMIT_ERROR, attempt=0, now=now)
        self.assertEqual(delay, MAX_AUTO_WAIT_S)

    def test_other_failures_get_bounded_backoff_then_none(self) -> None:
        self.assertEqual(compute_retry_delay("connection reset by peer", 0), TRANSIENT_BACKOFF_S[0])
        self.assertEqual(compute_retry_delay("connection reset by peer", 1), TRANSIENT_BACKOFF_S[1])
        self.assertIsNone(compute_retry_delay("connection reset by peer", 2))


class RunLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="looper_lock_test_"))
        self.lock = self.tmp / "run.lock"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_and_release(self) -> None:
        self.assertTrue(acquire_run_lock(self.lock))
        self.assertEqual(self.lock.read_text(), str(os.getpid()))
        release_run_lock(self.lock)
        self.assertFalse(self.lock.exists())

    def test_refuses_when_a_live_process_holds_it(self) -> None:
        self.lock.write_text("12345")
        with mock.patch.object(recovery, "_pid_alive", return_value=True):
            self.assertFalse(acquire_run_lock(self.lock))

    def test_takes_over_a_stale_lock(self) -> None:
        """The holder crashed without cleanup — exactly the scenario after
        which a resume MUST be allowed to proceed."""
        self.lock.write_text("12345")
        with mock.patch.object(recovery, "_pid_alive", return_value=False):
            self.assertTrue(acquire_run_lock(self.lock))
        self.assertEqual(self.lock.read_text(), str(os.getpid()))

    def test_reacquire_by_same_pid_is_fine(self) -> None:
        self.assertTrue(acquire_run_lock(self.lock))
        self.assertTrue(acquire_run_lock(self.lock))

    def test_release_never_removes_someone_elses_lock(self) -> None:
        self.lock.write_text("12345")
        release_run_lock(self.lock)
        self.assertTrue(self.lock.exists())


class MonitorResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_rearms_after_failure_but_keeps_spend(self) -> None:
        monitor = FailFastMonitor(max_run_budget_usd=100.0)
        await monitor.on_event("x", "turn_completed", "done", {"cost_usd": 3.0})
        await monitor.on_event("frontend_engineer", "error", "boom", {})
        self.assertIsNotNone(monitor.failure)

        monitor.reset()

        self.assertIsNone(monitor.failure)
        self.assertEqual(monitor.spent_usd, 3.0, "budget accumulation must survive a reset")
        # The re-armed kill switch still works.
        await monitor.on_event("backend_engineer", "error", "boom again", {})
        self.assertIsNotNone(monitor.failure)


class AutoResumeEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """Drives main.run() itself: solution_architect crashes on its first
    attempt, the recovery loop (with the wait patched to zero — the real
    policy waits minutes to hours) auto-resumes from the checkpoint, the
    retried architect succeeds, and the run completes with a release
    report — no manual --resume anywhere."""

    def setUp(self) -> None:
        self.output_dir = Path(tempfile.mkdtemp(prefix="looper_auto_resume_e2e_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.output_dir, ignore_errors=True)

    async def test_crash_then_automatic_resume_to_completion(self) -> None:
        set_script(
            {
                **BASE_SCRIPT,
                "solution_architect": [CRASH, BASE_SCRIPT["solution_architect"][0]],
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )
        scripted_build_team = functools.partial(build_team, agent_cls=ScriptedClaudeCodeAgent)

        with (
            mock.patch.object(main_module, "build_team", scripted_build_team),
            mock.patch.object(main_module, "compute_retry_delay", lambda *a, **k: 0.0),
            mock.patch.dict(os.environ, {"LOOPER_AUTO_RESUME": "1"}),
        ):
            await asyncio.wait_for(
                main_module.run("Build a thing.", self.output_dir, resume=None), timeout=30
            )

        # Completion evidence: the run recorded itself into cross-run
        # memory (completion-path only) and the transcript reached the
        # terminal release_reporter turn.
        self.assertTrue((self.output_dir / "memory" / "runs.jsonl").exists())
        transcripts = list(self.output_dir.glob("run-*.md"))
        self.assertEqual(len(transcripts), 1)
        self.assertIn("release_reporter", transcripts[0].read_text())
        # The lock was released on the way out.
        self.assertEqual(list((self.output_dir / "locks").glob("*.lock")), [])

    async def test_disabled_auto_resume_fails_immediately(self) -> None:
        """LOOPER_AUTO_RESUME=0 restores the pre-autonomous behavior: the
        crash propagates after printing --resume instructions."""
        set_script({**BASE_SCRIPT, "solution_architect": [CRASH]})
        scripted_build_team = functools.partial(build_team, agent_cls=ScriptedClaudeCodeAgent)

        with (
            mock.patch.object(main_module, "build_team", scripted_build_team),
            mock.patch.dict(os.environ, {"LOOPER_AUTO_RESUME": "0"}),
        ):
            with self.assertRaises(Exception):
                await asyncio.wait_for(
                    main_module.run("Build a thing.", self.output_dir, resume=None), timeout=30
                )


if __name__ == "__main__":
    unittest.main()
