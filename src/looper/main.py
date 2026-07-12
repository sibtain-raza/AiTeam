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
    apply_visual_qa_budget,
    build_team,
    reapply_turn_budget_on_resume,
    recover_stuck_agents,
    reset_pending_activation_flags,
)
from .run_memory import calibration_hint, load_runs, record_run, summarize_run
from .runner import FailFastMonitor, run_team


async def _checkpoint(team: GraphFlow, checkpoint_path: Path, stamp: str, goal: str, workspace: Path) -> None:
    state = await team.save_state()
    checkpoint_path.write_text(
        json.dumps(
            {"stamp": stamp, "goal": goal, "workspace": str(workspace), "team_state": state},
            indent=2,
        )
    )


async def run(goal: str | None, output_dir: Path, resume: Path | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    monitor = FailFastMonitor()

    # Cross-run memory (run_memory.py): if past runs' engineer sessions have
    # been running out of their estimated turn budgets, tell the architect
    # so this run's estimates are sized better. None (no history / no hits)
    # leaves the prompt untouched.
    hint = calibration_hint(load_runs(output_dir))
    if hint:
        print(f"Architect calibration from past runs: {hint}\n")

    if resume is not None:
        checkpoint_data = json.loads(resume.read_text())
        stamp = checkpoint_data["stamp"]
        goal = checkpoint_data["goal"]
        workspace = Path(checkpoint_data["workspace"])
        team, agents = build_team(workspace, on_event=monitor.on_event, architect_addendum=hint, output_dir=output_dir)
        # A crash mid-fan-out (e.g. devops_engineer fails while
        # frontend_engineer/backend_engineer succeed) leaves GraphFlow's own
        # checkpoint with no record that the crashed node was dispatched but
        # never finished — see recover_stuck_agents()'s docstring for why.
        # Patch the raw checkpoint dict before load_state() so that agent
        # actually retries instead of the resumed run completing zero turns.
        recovered = recover_stuck_agents(checkpoint_data["team_state"])
        if recovered:
            print(f"Recovered stuck agent(s) from crashed turn: {', '.join(recovered)}")
        # A second, deeper gap than the one above: a node sitting in the
        # checkpointed "ready" queue (whether it was already there, or just
        # added back by recover_stuck_agents()) needs its activation
        # group's enqueued flag reset, or a LATER loop-back edge into that
        # same group (e.g. QA_FAIL) silently never fires. See
        # reset_pending_activation_flags()'s docstring — real, reproduced
        # bug, not a defensive guess.
        reset_flags = reset_pending_activation_flags(checkpoint_data["team_state"])
        if reset_flags:
            print(f"Reset stale activation flags for resume: {', '.join(reset_flags)}")
        await team.load_state(checkpoint_data["team_state"])
        # set_max_turns() overrides aren't part of the checkpoint (only
        # _history is) — if the architect's turn already completed before
        # the crash, re-derive the same dynamic budget from its on-disk
        # pointer file rather than silently falling back to the static
        # default on the retry. See reapply_turn_budget_on_resume()'s
        # docstring for why this reads the file instead of any specific
        # engineer's replayed history — a real, reproduced race where the
        # naive history-based version silently failed.
        reapplied = reapply_turn_budget_on_resume(workspace, agents)
        if reapplied:
            print(f"Turn budget re-applied from disk on resume: {reapplied}")
        task = None  # continue the previous task rather than starting a new one
        print(f"Resuming run {stamp} from {resume}\nWorkspace: {workspace}\n")
    else:
        assert goal is not None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        workspace = output_dir / "workspace" / stamp
        workspace.mkdir(parents=True, exist_ok=True)
        team, agents = build_team(workspace, on_event=monitor.on_event, architect_addendum=hint, output_dir=output_dir)
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
        # Checkpoint after every completed agent turn, so a failure
        # (crash, hitting the Claude Code session limit, etc.) only
        # loses the turn in flight — not the whole run.
        await _checkpoint(team, checkpoint_path, stamp, goal, workspace)

    try:
        # run_team stops the whole run the instant any single agent
        # reports a failure (see runner.py) — it does not wait for the
        # other engineers to also finish or fail on their own.
        stop_reason = await run_team(team, task, on_message, monitor)
    except Exception as exc:
        print(f"\nRun failed: {exc}")
        print(f"Progress checkpointed to {checkpoint_path} — resume with:")
        print(f'  PYTHONPATH=src python -m looper.main --resume "{checkpoint_path}"')
        raise

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
