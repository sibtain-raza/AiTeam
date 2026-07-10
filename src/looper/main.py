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

from .pipeline import ARTIFACT_DIR_NAME, apply_turn_budget_from_architect, build_team, recover_stuck_agents
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

    if resume is not None:
        checkpoint_data = json.loads(resume.read_text())
        stamp = checkpoint_data["stamp"]
        goal = checkpoint_data["goal"]
        workspace = Path(checkpoint_data["workspace"])
        team, agents = build_team(workspace, on_event=monitor.on_event)
        # A crash mid-fan-out (e.g. devops_engineer fails while
        # frontend_engineer/backend_engineer succeed) leaves GraphFlow's own
        # checkpoint with no record that the crashed node was dispatched but
        # never finished — see recover_stuck_agents()'s docstring for why.
        # Patch the raw checkpoint dict before load_state() so that agent
        # actually retries instead of the resumed run completing zero turns.
        recovered = recover_stuck_agents(checkpoint_data["team_state"])
        if recovered:
            print(f"Recovered stuck agent(s) from crashed turn: {', '.join(recovered)}")
        await team.load_state(checkpoint_data["team_state"])
        # set_max_turns() overrides aren't part of the checkpoint (only
        # _history is) — if the architect's turn already completed before
        # the crash, its TECH DESIGN is still in every engineer's replayed
        # history; re-derive the same dynamic budget from it rather than
        # silently falling back to the static default on the retry.
        architect_message = agents["frontend_engineer"].find_message_from("solution_architect")
        if architect_message is not None:
            apply_turn_budget_from_architect(architect_message, agents)
        task = None  # continue the previous task rather than starting a new one
        print(f"Resuming run {stamp} from {resume}\nWorkspace: {workspace}\n")
    else:
        assert goal is not None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        workspace = output_dir / "workspace" / stamp
        workspace.mkdir(parents=True, exist_ok=True)
        team, agents = build_team(workspace, on_event=monitor.on_event)
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
        # Once the architect's TECH DESIGN lands, size each engineer's turn
        # budget to the task instead of leaving them all on the static
        # default — see pipeline.py's ARCHITECT_PROMPT "Turn Budget
        # Estimate" section and apply_turn_budget_from_architect().
        applied_budget = apply_turn_budget_from_architect(message, agents)
        if applied_budget:
            print(f"Turn budget applied from architect: {applied_budget}")
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

    print(f"\nStop reason: {stop_reason}")
    print(f"Transcript saved to {transcript}")
    print(f"Workspace (real files written by FE/BE/OPS/QA): {workspace}")


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
