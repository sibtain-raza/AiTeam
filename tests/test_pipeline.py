"""Pipeline orchestration tests, run against ScriptedClaudeCodeAgent.

These verify GraphFlow's routing/rework-loops/termination/checkpoint logic —
the part of this project that isn't the LLM's judgment — deterministically
and without spending any Claude Code session quota. They do NOT exercise the
real claude_agent_sdk integration; see README "Evidence this works" for how
that was verified (live, against real Claude Code sessions).

Run with:  PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import BaseChatMessage

from looper.pipeline import (
    apply_turn_budget_from_architect,
    build_team,
    parse_turn_budget,
    recover_stuck_agents,
    reset_pending_activation_flags,
)
from looper.runner import AgentFailure, FailFastMonitor, run_team

from .mock_agent import CRASH, ScriptedClaudeCodeAgent, set_script

PM_TEXT = "# PRD\n## North Star Goal\nBuild a thing.\n"
SCOPE_TEXT = "# SCOPE REVIEW\n## Verdict\nPROPORTIONATE\n## Must-Cut Items\n(none)\n"
ARCHITECT_TEXT = "# TECH DESIGN\n## Task Breakdown\nT-1 [FE] thing\nT-2 [BE] thing\nT-3 [OPS] thing\n"
FE_TEXT = "# FE IMPLEMENTATION\nDone.\n"
BE_TEXT = "# BE IMPLEMENTATION\nDone.\n"
OPS_TEXT = "# OPS IMPLEMENTATION\nDone.\n"
QA_PASS_TEXT = "# QA REPORT\n## Verdict\nAll good.\nQA_PASS"
QA_FAIL_TEXT = "# QA REPORT\n## Defects\nD-1 something is wrong\nQA_FAIL"
UAT_APPROVED_TEXT = "# UAT REPORT\n## Final Summary\nShipped.\nUAT_APPROVED"
UAT_REJECTED_TEXT = "# UAT REPORT\n## Gaps\nMissed the point.\nUAT_REJECTED"
REPORTER_TEXT = "# FINAL DELIVERY REPORT\nDone.\n"

BASE_SCRIPT = {
    "product_manager": [PM_TEXT],
    "scope_validator": [SCOPE_TEXT],
    "solution_architect": [ARCHITECT_TEXT],
    "frontend_engineer": [FE_TEXT],
    "backend_engineer": [BE_TEXT],
    "devops_engineer": [OPS_TEXT],
    "release_reporter": [REPORTER_TEXT],
}


def sources(messages: list) -> list[str]:
    return [m.source for m in messages if isinstance(m, BaseChatMessage)]


class PipelineOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="looper_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    async def _run(self, script: dict[str, list[str]]) -> TaskResult:
        set_script(script)
        team, _agents = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        result = None
        async for message in team.run_stream(task="Build a thing."):
            if isinstance(message, TaskResult):
                result = message
        assert result is not None
        return result

    async def test_happy_path_no_rework(self) -> None:
        """QA passes and UAT approves first try — every role runs exactly once."""
        result = await self._run(
            {**BASE_SCRIPT, "qa_engineer": [QA_PASS_TEXT], "uat_reviewer": [UAT_APPROVED_TEXT]}
        )
        src = sources(result.messages)
        self.assertEqual(src.count("qa_engineer"), 1)
        self.assertEqual(src.count("uat_reviewer"), 1)
        self.assertEqual(src.count("release_reporter"), 1)
        self.assertEqual(src.count("frontend_engineer"), 1)
        self.assertIn("release_reporter", src)

    async def test_qa_rework_loop_then_pass(self) -> None:
        """QA_FAIL once routes back to the engineers; QA_PASS on retry proceeds to UAT."""
        result = await self._run(
            {
                **BASE_SCRIPT,
                "qa_engineer": [QA_FAIL_TEXT, QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )
        src = sources(result.messages)
        self.assertEqual(src.count("qa_engineer"), 2)
        self.assertEqual(src.count("frontend_engineer"), 2, "FE should rework after QA_FAIL")
        self.assertEqual(src.count("backend_engineer"), 2)
        self.assertEqual(src.count("devops_engineer"), 2)
        self.assertEqual(src.count("uat_reviewer"), 1)
        self.assertEqual(src.count("release_reporter"), 1)
        # PM/Architect only ran once — the rework loop must not restart grooming/design.
        self.assertEqual(src.count("product_manager"), 1)
        self.assertEqual(src.count("solution_architect"), 1)

    async def test_qa_hard_fail_terminates_pipeline(self) -> None:
        """3 consecutive QA_FAILs hard-stop the run with PIPELINE_FAILED, never reaching UAT."""
        result = await self._run({**BASE_SCRIPT, "qa_engineer": [QA_FAIL_TEXT]})
        self.assertIn("PIPELINE_FAILED", result.stop_reason or "")
        src = sources(result.messages)
        self.assertEqual(src.count("qa_engineer"), 3)
        self.assertNotIn("uat_reviewer", src)
        self.assertNotIn("release_reporter", src)

    async def test_uat_rejection_loop_then_approve(self) -> None:
        """UAT_REJECTED once loops back to PM grooming; UAT_APPROVED on retry proceeds to done."""
        result = await self._run(
            {
                **BASE_SCRIPT,
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_REJECTED_TEXT, UAT_APPROVED_TEXT],
            }
        )
        src = sources(result.messages)
        self.assertEqual(src.count("product_manager"), 2, "PM should re-groom after UAT_REJECTED")
        self.assertEqual(src.count("uat_reviewer"), 2)
        self.assertEqual(src.count("release_reporter"), 1)
        # Confirm ordering: the final release_reporter comes after the second uat_reviewer call.
        self.assertLess(src.index("release_reporter"), len(src))

    async def test_checkpoint_resume_recovers_from_a_crashed_turn(self) -> None:
        """A turn that raises (simulating a real Claude Code session-limit
        crash) stops the run right after the PM's completed turn. Resuming
        from the last checkpoint on a fresh team/process should retry the
        crashed solution_architect turn — not re-run product_manager — and
        continue to completion. Mirrors what was verified live against the
        real pipeline (see README "Checkpoint & resume"): we killed a real
        run right after product_manager finished and confirmed resume
        picked up at solution_architect without re-running PM.

        Note: deliberately does NOT use `break` to simulate the interruption.
        GraphFlow's runtime is a producer/consumer queue; with near-instant
        scripted responses it races ahead of a paused consumer rather than
        pausing with it, so a `break`-based version of this test raced the
        whole pipeline to completion in the background before save_state()
        was ever called (confirmed via a debug run). A real exception is
        what actually halts the runtime, matching the crash we're modeling.
        """
        set_script(
            {
                **BASE_SCRIPT,
                "solution_architect": [CRASH],
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )

        team1, _agents1 = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        seen: list[str] = []
        checkpoint_state = None
        with self.assertRaises(Exception):
            async for message in team1.run_stream(task="Build a thing."):
                if isinstance(message, BaseChatMessage):
                    seen.append(message.source)
                    # Mirrors main.py: checkpoint after every completed turn.
                    checkpoint_state = await team1.save_state()
        self.assertEqual(seen, ["user", "product_manager", "scope_validator"])
        assert checkpoint_state is not None

        # "Fix" the crash for the retry — like a human re-running after a
        # transient failure — then resume on a fresh team/agent instances.
        set_script(
            {**BASE_SCRIPT, "qa_engineer": [QA_PASS_TEXT], "uat_reviewer": [UAT_APPROVED_TEXT]}
        )
        team2, _agents2 = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        await team2.load_state(checkpoint_state)

        resumed: list[str] = []
        async for message in team2.run_stream(task=None):
            if isinstance(message, BaseChatMessage):
                resumed.append(message.source)

        self.assertNotIn(
            "product_manager", resumed, "resume must not re-run the already-completed PM turn"
        )
        self.assertEqual(resumed[0], "solution_architect", "resume should retry the crashed turn")
        self.assertIn("release_reporter", resumed)

    async def test_fail_fast_stops_the_run_when_one_of_several_parallel_engineers_crashes(self) -> None:
        """frontend_engineer, backend_engineer, and devops_engineer all run
        concurrently off the same architect fan-out. Live testing against
        the real pipeline found that when exactly one of them crashes (hit
        the turn cap) while the others are still mid-turn, the installed
        AutoGen version does NOT cleanly raise that failure out of
        `team.run_stream()` — it degrades into an unhandled internal
        message-routing error in a background asyncio task, and the run
        sits reporting "running" forever (see runner.py's module docstring
        and CLAUDE.md). This is the regression test for the fix: `run_team`
        must detect the crash via the on_event hook (independent of
        GraphFlow's own propagation) and raise AgentFailure promptly rather
        than hang — `asyncio.wait_for` below turns a reintroduced hang into
        a fast, clear test failure instead of a stuck test run.
        """
        set_script(
            {
                **BASE_SCRIPT,
                "frontend_engineer": [CRASH],
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )
        monitor = FailFastMonitor()
        team, _agents = build_team(
            self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent, on_event=monitor.on_event
        )

        async def on_message(_message: BaseChatMessage) -> None:
            pass

        with self.assertRaises(AgentFailure) as ctx:
            await asyncio.wait_for(run_team(team, "Build a thing.", on_message, monitor), timeout=10)
        self.assertEqual(ctx.exception.source, "frontend_engineer")


    async def test_checkpoint_resume_recovers_from_a_crash_mid_fan_out(self) -> None:
        """Regression test for a real, reproduced bug: when one of several
        PARALLEL engineers (devops_engineer here) crashes while its siblings
        (frontend_engineer/backend_engineer) complete successfully,
        GraphFlow's OWN checkpoint has no record that devops_engineer was
        dispatched but never finished — `GraphFlowManagerState
        .select_speaker()` clears that node's "needs to run" bookkeeping the
        instant it's selected, before it actually executes. Confirmed live:
        resuming such a checkpoint unmodified produces a run that reports
        "group chat is stopped" with zero new turns, even though
        devops_engineer clearly still has pending work.

        `recover_stuck_agents()` is the fix: it patches the raw checkpoint
        dict (using `ClaudeCodeAgent._turn_in_progress`, which DOES survive
        the crash) so devops_engineer is re-enqueued before `load_state()`.
        This test resumes WITHOUT calling it first to prove the bug is real,
        then resumes again WITH it to prove the fix works end-to-end.
        """
        set_script(
            {
                **BASE_SCRIPT,
                "devops_engineer": [CRASH],
                "qa_engineer": [QA_PASS_TEXT],
                "uat_reviewer": [UAT_APPROVED_TEXT],
            }
        )
        team1, _agents1 = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        seen: list[str] = []
        checkpoint_state = None
        with self.assertRaises(Exception):
            async for message in team1.run_stream(task="Build a thing."):
                if isinstance(message, BaseChatMessage):
                    seen.append(message.source)
                    checkpoint_state = await team1.save_state()
        assert checkpoint_state is not None
        # frontend_engineer/backend_engineer completed; devops_engineer crashed mid-turn.
        self.assertIn("frontend_engineer", seen)
        self.assertIn("backend_engineer", seen)
        self.assertNotIn("devops_engineer", seen)

        # "Fix" the crash for the retry, as a human would.
        fixed_script = {
            **BASE_SCRIPT,
            "devops_engineer": [OPS_TEXT],
            "qa_engineer": [QA_PASS_TEXT],
            "uat_reviewer": [UAT_APPROVED_TEXT],
        }

        # Prove the bug: resuming unmodified makes zero progress.
        unpatched_state = copy.deepcopy(checkpoint_state)
        set_script(fixed_script)
        team_unpatched, _agents_unpatched = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        await team_unpatched.load_state(unpatched_state)
        stalled: list[str] = []
        async for message in team_unpatched.run_stream(task=None):
            if isinstance(message, BaseChatMessage):
                stalled.append(message.source)
        self.assertEqual(stalled, [], "unpatched resume should make no progress (this is the bug)")

        # Now prove the fix: recover_stuck_agents() + resume completes the run.
        patched_state = copy.deepcopy(checkpoint_state)
        recovered = recover_stuck_agents(patched_state)
        self.assertEqual(recovered, ["devops_engineer"])
        set_script(fixed_script)
        team2, _agents2 = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        await team2.load_state(patched_state)

        resumed: list[str] = []
        async for message in team2.run_stream(task=None):
            if isinstance(message, BaseChatMessage):
                resumed.append(message.source)

        self.assertNotIn("frontend_engineer", resumed, "FE already completed — must not rerun")
        self.assertNotIn("backend_engineer", resumed, "BE already completed — must not rerun")
        self.assertIn("devops_engineer", resumed, "the crashed OPS turn must retry")
        self.assertIn("qa_engineer", resumed)
        self.assertIn("release_reporter", resumed)


    async def test_checkpoint_resume_then_qa_fail_can_still_rework(self) -> None:
        """Regression test for a real, reproduced bug, one layer deeper than
        the crash-mid-fan-out test above: when ALL THREE parallel engineers
        crash before any of them completes (not just one), the only
        checkpoint that ends up on disk pre-dates GraphFlow's own
        dispatch-time bookkeeping update for their shared "any" activation
        groups (see reset_pending_activation_flags()'s docstring for the
        full mechanism, traced against AutoGen's _digraph_group_chat.py).
        recover_stuck_agents() finds nothing to recover here (matches the
        live observation: turn_in_progress was False for all three, since
        their crash happened before any checkpoint captured them mid-turn)
        — GraphFlow correctly re-runs all three on resume regardless. The
        bug is downstream: a LATER QA_FAIL, which shares the same
        activation group as the architect's original fan-out edge, is
        silently swallowed unless reset_pending_activation_flags() runs
        first. This resumes the SAME crashed checkpoint twice — once
        unpatched to prove the bug, once patched to prove the fix — exactly
        mirroring test_checkpoint_resume_recovers_from_a_crash_mid_fan_out's
        structure for the sibling bug it covers.
        """
        set_script(
            {
                **BASE_SCRIPT,
                "frontend_engineer": [CRASH],
                "backend_engineer": [CRASH],
                "devops_engineer": [CRASH],
            }
        )
        team1, _agents1 = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        seen: list[str] = []
        checkpoint_state = None
        with self.assertRaises(Exception):
            # Mirrors main.py exactly: checkpoint only on each successfully
            # -streamed message, never freshly after catching the crash —
            # this is WHY the real checkpoint pre-dates the engineers'
            # dispatch-time bookkeeping (none of them ever streams a
            # message when all three crash), which is the whole bug.
            async for message in team1.run_stream(task="Build a thing."):
                if isinstance(message, BaseChatMessage):
                    seen.append(message.source)
                    checkpoint_state = await team1.save_state()
        assert checkpoint_state is not None
        self.assertEqual(seen, ["user", "product_manager", "scope_validator", "solution_architect"])

        # Real observation this reproduces: nothing "stuck" by the
        # turn_in_progress-based recovery — all three crashed before any
        # checkpoint captured them mid-turn.
        self.assertEqual(recover_stuck_agents(copy.deepcopy(checkpoint_state)), [])

        fixed_script = {
            **BASE_SCRIPT,
            "frontend_engineer": [FE_TEXT, FE_TEXT],
            "backend_engineer": [BE_TEXT, BE_TEXT],
            "devops_engineer": [OPS_TEXT, OPS_TEXT],
            "qa_engineer": [QA_FAIL_TEXT, QA_PASS_TEXT],
            "uat_reviewer": [UAT_APPROVED_TEXT],
        }

        # Prove the bug: without reset_pending_activation_flags(), QA_FAIL
        # never reaches a second engineer turn — the run falls straight out
        # via GraphFlow's own "Digraph execution is complete" path.
        unpatched_state = copy.deepcopy(checkpoint_state)
        set_script(fixed_script)
        team_unpatched, _a = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        await team_unpatched.load_state(unpatched_state)
        buggy: list[str] = []
        async for message in team_unpatched.run_stream(task=None):
            if isinstance(message, BaseChatMessage):
                buggy.append(message.source)
        self.assertEqual(buggy.count("qa_engineer"), 1, "QA_FAIL happened once...")
        self.assertEqual(buggy.count("frontend_engineer"), 1, "...but never triggered a rework turn (the bug)")
        self.assertNotIn("uat_reviewer", buggy)

        # Now prove the fix.
        patched_state = copy.deepcopy(checkpoint_state)
        reset = reset_pending_activation_flags(patched_state)
        self.assertEqual(len(reset), 3)
        set_script(fixed_script)
        team_patched, _a = build_team(self.workspace_dir, agent_cls=ScriptedClaudeCodeAgent)
        await team_patched.load_state(patched_state)
        fixed: list[str] = []
        async for message in team_patched.run_stream(task=None):
            if isinstance(message, BaseChatMessage):
                fixed.append(message.source)
        self.assertEqual(fixed.count("frontend_engineer"), 2, "rework turn now fires correctly")
        self.assertEqual(fixed.count("qa_engineer"), 2)
        self.assertIn("uat_reviewer", fixed)
        self.assertIn("release_reporter", fixed)


class RecoverStuckAgentsTests(unittest.TestCase):
    """Pure dict-manipulation unit tests for recover_stuck_agents(), no
    GraphFlow/asyncio involved — see the docstring on the function itself
    and PipelineOrchestrationTests.test_checkpoint_resume_recovers_from_a_
    crash_mid_fan_out for the full end-to-end regression coverage."""

    def test_recovers_agent_with_turn_in_progress(self) -> None:
        state = {
            "agent_states": {
                "GraphManager": {"ready": []},
                "frontend_engineer": {"agent_state": {"turn_in_progress": False}},
                "devops_engineer": {"agent_state": {"turn_in_progress": True}},
            }
        }
        recovered = recover_stuck_agents(state)
        self.assertEqual(recovered, ["devops_engineer"])
        self.assertEqual(state["agent_states"]["GraphManager"]["ready"], ["devops_engineer"])

    def test_no_op_when_nothing_stuck(self) -> None:
        state = {
            "agent_states": {
                "GraphManager": {"ready": []},
                "frontend_engineer": {"agent_state": {"turn_in_progress": False}},
            }
        }
        self.assertEqual(recover_stuck_agents(state), [])
        self.assertEqual(state["agent_states"]["GraphManager"]["ready"], [])

    def test_does_not_duplicate_an_already_ready_agent(self) -> None:
        state = {
            "agent_states": {
                "GraphManager": {"ready": ["devops_engineer"]},
                "devops_engineer": {"agent_state": {"turn_in_progress": True}},
            }
        }
        self.assertEqual(recover_stuck_agents(state), [])
        self.assertEqual(state["agent_states"]["GraphManager"]["ready"], ["devops_engineer"])

    def test_missing_graph_manager_is_a_no_op(self) -> None:
        state = {"agent_states": {"devops_engineer": {"agent_state": {"turn_in_progress": True}}}}
        self.assertEqual(recover_stuck_agents(state), [])


class ResetPendingActivationFlagsTests(unittest.TestCase):
    """Pure dict-manipulation unit tests for reset_pending_activation_flags()
    — see its docstring and PipelineOrchestrationTests
    .test_checkpoint_resume_then_qa_fail_can_still_rework for the full
    end-to-end regression coverage of the real bug this fixes: a QA_FAIL
    rework edge silently never firing after a crash-mid-fan-out resume,
    because the "any"-activation group's enqueued flag was still True from
    the original (never-popped) dispatch."""

    def test_resets_true_flags_for_nodes_in_ready(self) -> None:
        state = {
            "agent_states": {
                "GraphManager": {
                    "ready": ["frontend_engineer", "backend_engineer"],
                    "enqueued_any": {
                        "frontend_engineer": {"frontend_engineer_activation": True},
                        "backend_engineer": {"backend_engineer_activation": True},
                        "devops_engineer": {"devops_engineer_activation": True},
                    },
                }
            }
        }
        reset = reset_pending_activation_flags(state)
        self.assertEqual(sorted(reset), ["backend_engineer/backend_engineer_activation", "frontend_engineer/frontend_engineer_activation"])
        gm = state["agent_states"]["GraphManager"]
        self.assertFalse(gm["enqueued_any"]["frontend_engineer"]["frontend_engineer_activation"])
        self.assertFalse(gm["enqueued_any"]["backend_engineer"]["backend_engineer_activation"])
        # devops_engineer isn't in ready — must be left untouched.
        self.assertTrue(gm["enqueued_any"]["devops_engineer"]["devops_engineer_activation"])

    def test_already_false_flags_are_a_no_op(self) -> None:
        state = {
            "agent_states": {
                "GraphManager": {
                    "ready": ["frontend_engineer"],
                    "enqueued_any": {"frontend_engineer": {"frontend_engineer_activation": False}},
                }
            }
        }
        self.assertEqual(reset_pending_activation_flags(state), [])

    def test_missing_graph_manager_is_a_no_op(self) -> None:
        self.assertEqual(reset_pending_activation_flags({"agent_states": {}}), [])

    def test_reproduces_the_real_crash_mid_fanout_checkpoint(self) -> None:
        """The exact shape observed live: the architect's fan-out dispatched
        all three engineers (added to ready, enqueued_any=True), then all
        three crashed before any completed — so recover_stuck_agents()
        (turn_in_progress-based) finds nothing, but the QA_FAIL edge that
        fires much later would still be silently swallowed without this
        fix."""
        state = {
            "agent_states": {
                "GraphManager": {
                    "ready": ["frontend_engineer", "backend_engineer", "devops_engineer"],
                    "enqueued_any": {
                        "frontend_engineer": {"frontend_engineer_activation": True},
                        "backend_engineer": {"backend_engineer_activation": True},
                        "devops_engineer": {"devops_engineer_activation": True},
                        "qa_engineer": {"qa_engineer": False},
                    },
                },
                "frontend_engineer": {"agent_state": {"turn_in_progress": False}},
                "backend_engineer": {"agent_state": {"turn_in_progress": False}},
                "devops_engineer": {"agent_state": {"turn_in_progress": False}},
            }
        }
        self.assertEqual(recover_stuck_agents(state), [], "matches the live observation: nothing 'stuck'")
        reset = reset_pending_activation_flags(state)
        self.assertEqual(len(reset), 3)
        gm = state["agent_states"]["GraphManager"]
        for node in ("frontend_engineer", "backend_engineer", "devops_engineer"):
            group = f"{node}_activation"
            self.assertFalse(gm["enqueued_any"][node][group], f"{node}'s group must be re-armed for QA_FAIL")


if __name__ == "__main__":
    unittest.main()
