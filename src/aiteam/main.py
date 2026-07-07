"""CLI entry point.

Usage:
    python -m aiteam.main "Build a URL shortener with custom aliases and click analytics."
"""

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.ui import Console

from .config import make_model_client
from .pipeline import build_team


async def run(goal: str, output_dir: Path) -> None:
    model_client = make_model_client()
    team = build_team(model_client)

    result = await Console(team.run_stream(task=goal))

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    transcript = output_dir / f"run-{stamp}.md"
    with transcript.open("w") as f:
        f.write(f"# AiTeam run — {stamp}\n\nGOAL: {goal}\n\n")
        f.write(f"STOP REASON: {result.stop_reason}\n\n---\n\n")
        for msg in result.messages:
            if isinstance(msg, BaseChatMessage):
                f.write(f"## {msg.source}\n\n{msg.to_text()}\n\n---\n\n")
    print(f"\nStop reason: {result.stop_reason}")
    print(f"Transcript saved to {transcript}")

    await model_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AiTeam delivery pipeline")
    parser.add_argument("goal", help="The software goal to build")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("AITEAM_OUTPUT_DIR", "output"),
        help="Directory for run transcripts (default: ./output)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.goal, Path(args.output_dir)))


if __name__ == "__main__":
    main()
