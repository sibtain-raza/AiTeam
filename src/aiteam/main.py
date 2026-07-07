"""CLI entry point.

Usage:
    python -m aiteam.main "Build a URL shortener with custom aliases and click analytics."
    python -m aiteam.main --resume output/checkpoints/20260707-223327.json
"""

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import GraphFlow

from .pipeline import build_team


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

    if resume is not None:
        checkpoint_data = json.loads(resume.read_text())
        stamp = checkpoint_data["stamp"]
        goal = checkpoint_data["goal"]
        workspace = Path(checkpoint_data["workspace"])
        team = build_team(workspace)
        await team.load_state(checkpoint_data["team_state"])
        task = None  # continue the previous task rather than starting a new one
        print(f"Resuming run {stamp} from {resume}\nWorkspace: {workspace}\n")
    else:
        assert goal is not None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        workspace = output_dir / "workspace" / stamp
        workspace.mkdir(parents=True, exist_ok=True)
        team = build_team(workspace)
        task = goal

    checkpoint_path = output_dir / "checkpoints" / f"{stamp}.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    transcript = output_dir / f"run-{stamp}.md"
    if resume is None:
        transcript.write_text(f"# AiTeam run — {stamp}\n\nGOAL: {goal}\n\n---\n\n")

    result: TaskResult | None = None
    try:
        async for message in team.run_stream(task=task):
            if isinstance(message, TaskResult):
                result = message
                continue
            if isinstance(message, BaseChatMessage):
                print(f"\n---------- {message.source} ----------\n{message.to_text()}")
                with transcript.open("a") as f:
                    f.write(f"## {message.source}\n\n{message.to_text()}\n\n---\n\n")
                # Checkpoint after every completed agent turn, so a failure
                # (crash, hitting the Claude Code session limit, etc.) only
                # loses the turn in flight — not the whole run.
                await _checkpoint(team, checkpoint_path, stamp, goal, workspace)
    except Exception as exc:
        print(f"\nRun failed: {exc}")
        print(f"Progress checkpointed to {checkpoint_path} — resume with:")
        print(f'  PYTHONPATH=src python -m aiteam.main --resume "{checkpoint_path}"')
        raise

    print(f"\nStop reason: {result.stop_reason if result else 'unknown'}")
    print(f"Transcript saved to {transcript}")
    print(f"Workspace (real files written by FE/BE/OPS/QA): {workspace}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AiTeam delivery pipeline")
    parser.add_argument("goal", nargs="?", help="The software goal to build (omit when using --resume)")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("AITEAM_OUTPUT_DIR", "output"),
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
