"""CLI entry point.

Usage:
    python -m looper.main "Build a URL shortener with custom aliases and click analytics."
    python -m looper.main --resume output/checkpoints/20260707-223327.json
"""

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import GraphFlow

from .artifacts import find_blockers, validate_artifact
from .pipeline import (
    ARTIFACT_DIR_NAME,
    SDK_LOG_FILE_NAME,
    apply_turn_budget_from_architect,
    apply_deploy_verify_budget,
    apply_visual_qa_budget,
    build_team,
    reapply_turn_budget_on_resume,
    recover_stuck_agents,
    reset_pending_activation_flags,
)
from .recovery import acquire_run_lock, compute_retry_delay, release_run_lock
from .run_memory import (
    calibration_hint,
    defect_history_hints,
    load_runs,
    record_run,
    summarize_run,
)
from .runner import FailFastMonitor, run_team


async def _checkpoint(team: GraphFlow, checkpoint_path: Path, stamp: str, goal: str, workspace: Path) -> None:
    state = await team.save_state()
    checkpoint_path.write_text(
        json.dumps(
            {"stamp": stamp, "goal": goal, "workspace": str(workspace), "team_state": state},
            indent=2,
        )
    )


async def _restore_from_checkpoint(
    checkpoint_data: dict,
    monitor: FailFastMonitor,
    hint: str | None,
    defect_hints: dict,
    output_dir: Path,
):
    """Rebuild a team from a checkpoint dict and repair everything a raw
    `load_state()` alone gets wrong — one shared implementation for the
    manual `--resume` path and the auto-resume recovery loop, so the two
    can never drift apart. Each repair traces to a real reproduced bug;
    see the respective functions' docstrings."""
    stamp = checkpoint_data["stamp"]
    goal = checkpoint_data["goal"]
    workspace = Path(checkpoint_data["workspace"])
    team, agents = build_team(
        workspace,
        on_event=monitor.on_event,
        architect_addendum=hint,
        output_dir=output_dir,
        role_addenda=defect_hints,
    )
    # Crash mid-fan-out: a dispatched-but-never-finished agent leaves no
    # trace in GraphFlow's own checkpoint — re-enqueue via the agent-level
    # turn_in_progress flag (see recover_stuck_agents()).
    recovered = recover_stuck_agents(checkpoint_data["team_state"])
    if recovered:
        print(f"Recovered stuck agent(s) from crashed turn: {', '.join(recovered)}")
    # A node still sitting in the checkpointed "ready" queue needs its
    # "any"-activation flag re-armed or a later loop-back edge (QA_FAIL)
    # is silently swallowed (see reset_pending_activation_flags()).
    reset_flags = reset_pending_activation_flags(checkpoint_data["team_state"])
    if reset_flags:
        print(f"Reset stale activation flags for resume: {', '.join(reset_flags)}")
    await team.load_state(checkpoint_data["team_state"])
    # Dynamic budgets are in-memory only — re-derive from the on-disk
    # design (see reapply_turn_budget_on_resume() for why NOT from any
    # agent's replayed history).
    reapplied = reapply_turn_budget_on_resume(workspace, agents)
    if reapplied:
        print(f"Turn budget re-applied from disk on resume: {reapplied}")
    # Run-level dollar cap covers the WHOLE run across resumes: seed the
    # counter with everything already spent, per the authoritative SDK log.
    already_spent = summarize_run(workspace, stamp, goal, None)["total_cost_usd"]
    if already_spent:
        monitor.seed_spent(already_spent)
        print(f"Run-budget counter seeded with ${already_spent:.2f} already spent before this resume")
    return team, agents, stamp, goal, workspace


async def run(goal: str | None, output_dir: Path, resume: Path | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run-level dollar ceiling (across every agent's sessions, cumulative;
    # see runner.FailFastMonitor). Unset = uncapped. A resumed run seeds the
    # counter from what the interrupted run already spent (below).
    _run_budget_raw = os.environ.get("LOOPER_MAX_RUN_BUDGET_USD", "")
    monitor = FailFastMonitor(
        max_run_budget_usd=float(_run_budget_raw) if _run_budget_raw else None
    )

    # Cross-run memory (run_memory.py): if past runs' engineer sessions have
    # been running out of their estimated turn budgets, tell the architect
    # so this run's estimates are sized better. None (no history / no hits)
    # leaves the prompt untouched.
    past_runs = load_runs(output_dir)
    hint = calibration_hint(past_runs)
    if hint:
        print(f"Architect calibration from past runs: {hint}\n")
    # Per-engineer recurring-defect hints (run_memory.defect_history_hints):
    # BLOCKER/MAJOR defects each role produced in past runs, injected into
    # that role's prompt so the same class of mistake isn't repeated.
    defect_hints = defect_history_hints(past_runs)
    if defect_hints:
        print(f"Defect-history hints injected for: {', '.join(sorted(defect_hints))}\n")

    if resume is not None:
        checkpoint_data = json.loads(resume.read_text())
        team, agents, stamp, goal, workspace = await _restore_from_checkpoint(
            checkpoint_data, monitor, hint, defect_hints, output_dir
        )
        task = None  # continue the previous task rather than starting a new one
        print(f"Resuming run {stamp} from {resume}\nWorkspace: {workspace}\n")
    else:
        assert goal is not None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        workspace = output_dir / "workspace" / stamp
        workspace.mkdir(parents=True, exist_ok=True)
        team, agents = build_team(workspace, on_event=monitor.on_event, architect_addendum=hint, output_dir=output_dir, role_addenda=defect_hints)
        task = goal

    checkpoint_path = output_dir / "checkpoints" / f"{stamp}.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    transcript = output_dir / f"run-{stamp}.md"
    if resume is None:
        transcript.write_text(f"# Looper run — {stamp}\n\nGOAL: {goal}\n\n---\n\n")

    async def on_message(message: BaseChatMessage) -> None:
        print(f"\n---------- {message.source} ----------\n{message.to_text()}")
        with transcript.open("a") as f:
            f.write(f"## {message.source}\n\n{message.to_text()}\n\n---\n\n")
        # Persist the latest artifact per source to disk. This is what
        # the pointer_files mechanism (see pipeline.py / ClaudeCodeAgent)
        # points agents at on rework turns instead of replaying the
        # full text — overwriting keeps exactly one file per source,
        # always the current version.
        artifact_dir = workspace / ARTIFACT_DIR_NAME
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / f"{message.source}.md").write_text(message.to_text())
        # Advisory protocol checks (see artifacts.py): warn on an artifact
        # that drifts from its role's schema, and surface any BLOCKER
        # escalation lines instead of leaving them buried in the transcript.
        # Neither stops the run.
        for problem in validate_artifact(message.source, message.to_text()):
            print(f"ARTIFACT WARNING [{message.source}]: {problem}")
        for blocker in find_blockers(message.to_text()):
            print(f"BLOCKER raised by {message.source}: {blocker}")
        # Once the architect's TECH DESIGN lands, size each engineer's turn
        # budget to the task instead of leaving them all on the static
        # default — see pipeline.py's ARCHITECT_PROMPT "Turn Budget
        # Estimate" section and apply_turn_budget_from_architect().
        applied_budget = apply_turn_budget_from_architect(message, agents)
        if applied_budget:
            print(f"Turn budget applied from architect: {applied_budget}")
        # Same idea, for QA's Visual QA pass (see ARCHITECT_PROMPT's
        # "Visual QA" section and apply_visual_qa_budget()) — only bumps
        # QA's budget when the architect actually requested it.
        visual_qa_extra = apply_visual_qa_budget(message, agents, agents["qa_engineer"].max_turns)
        if visual_qa_extra:
            print(f"QA visual-verification budget applied from architect: +{visual_qa_extra} turns")
        # ...and QA's deploy-verification pass (compose build/up + health
        # checks). Called after the visual bump so both extras stack.
        deploy_extra = apply_deploy_verify_budget(message, agents, agents["qa_engineer"].max_turns)
        if deploy_extra:
            print(f"QA deploy-verification budget applied from architect: +{deploy_extra} turns")
        # Checkpoint after every completed agent turn, so a failure
        # (crash, hitting the Claude Code session limit, etc.) only
        # loses the turn in flight — not the whole run.
        await _checkpoint(team, checkpoint_path, stamp, goal, workspace)

    # Per-run PID lock: closes the documented double-resume hazard (two
    # --resume invocations against the same checkpoint once ran
    # concurrently against the same workspace — see README). Stale locks
    # from a crashed holder are taken over automatically.
    lock_path = output_dir / "locks" / f"{stamp}.lock"
    if not acquire_run_lock(lock_path):
        raise RuntimeError(
            f"Another process (PID {lock_path.read_text().strip()}) is already running "
            f"run {stamp} — refusing to start a second one against the same workspace. "
            f"Verify it has exited (ps) before retrying; a stale lock from a dead "
            f"process is taken over automatically."
        )

    # Auto-resume loop: a session-limit failure waits out the printed
    # reset time and resumes itself from the latest checkpoint; any other
    # failure gets a short bounded backoff and one retry cycle — the
    # decisions live in recovery.compute_retry_delay(), this loop just
    # executes them. LOOPER_AUTO_RESUME=0 disables it entirely
    # (pre-autonomous behavior: print --resume instructions and exit).
    auto_resume = os.environ.get("LOOPER_AUTO_RESUME", "1") != "0"
    max_auto_resumes = int(os.environ.get("LOOPER_MAX_AUTO_RESUMES", "3") or 3)
    attempt = 0
    try:
        while True:
            try:
                # run_team stops the whole run the instant any single agent
                # reports a failure (see runner.py) — it does not wait for
                # the other engineers to also finish or fail on their own.
                stop_reason = await run_team(team, task, on_message, monitor)
                break
            except Exception as exc:
                delay = (
                    compute_retry_delay(str(exc), attempt)
                    if auto_resume and checkpoint_path.exists()
                    else None
                )
                if delay is None or attempt >= max_auto_resumes:
                    print(f"\nRun failed: {exc}")
                    print(f"Progress checkpointed to {checkpoint_path} — resume with:")
                    print(f'  PYTHONPATH=src python -m looper.main --resume "{checkpoint_path}"')
                    raise
                attempt += 1
                print(f"\nRun interrupted: {exc}")
                print(
                    f"Auto-resume {attempt}/{max_auto_resumes}: waiting "
                    f"{delay / 60:.1f} min, then resuming from {checkpoint_path}"
                )
                await asyncio.sleep(delay)
                monitor.reset()
                checkpoint_data = json.loads(checkpoint_path.read_text())
                team, agents, stamp, goal, workspace = await _restore_from_checkpoint(
                    checkpoint_data, monitor, hint, defect_hints, output_dir
                )
                task = None
                print(f"Auto-resumed run {stamp}\n")
    finally:
        release_run_lock(lock_path)

    # Record this run into cross-run memory (aggregated from the SDK
    # interaction log) — completion path only, so a crashed-then-resumed run
    # is recorded exactly once, at its eventual completion.
    memory_file = record_run(output_dir, summarize_run(workspace, stamp, goal, stop_reason))

    print(f"\nStop reason: {stop_reason}")
    print(f"Run recorded in cross-run memory: {memory_file}")
    print(f"Transcript saved to {transcript}")
    print(f"Workspace (real files written by FE/BE/OPS/QA): {workspace}")
    print(f"Claude SDK interaction log (every prompt/response, per agent): {workspace / ARTIFACT_DIR_NAME / SDK_LOG_FILE_NAME}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Looper delivery pipeline")
    parser.add_argument("goal", nargs="?", help="The software goal to build (omit when using --resume)")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("LOOPER_OUTPUT_DIR", "output"),
        help="Directory for run transcripts (default: ./output)",
    )
    parser.add_argument(
        "--resume",
        metavar="CHECKPOINT_JSON",
        help="Resume a failed/interrupted run from its checkpoint file (path is printed on failure)",
    )
    args = parser.parse_args()
    if args.resume is None and args.goal is None:
        parser.error("goal is required unless --resume is given")
    asyncio.run(run(args.goal, Path(args.output_dir), Path(args.resume) if args.resume else None))


if __name__ == "__main__":
    main()
