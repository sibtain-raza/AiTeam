"""Drives one pipeline run in the background.

Builds the team with ClaudeCodeAgent's `on_event` hook wired to persist
RunEvent rows and publish them to the live SSE broker, then updates the
Run row's status when the run finishes (or fails). Reuses the same
workspace/checkpoint conventions as the CLI (`main.py`) — a run started
here writes the identical `.pipeline-docs/` artifacts and checkpoint JSON,
so the existing `--resume` CLI flow works on a web-started run too if the
server process itself goes down mid-run.
"""

import json
from pathlib import Path

from autogen_agentchat.messages import BaseChatMessage

from aiteam.claude_code_agent import ClaudeCodeAgent
from aiteam.pipeline import ARTIFACT_DIR_NAME, apply_turn_budget_from_architect, build_team
from aiteam.runner import FailFastMonitor, run_team

from .db import SessionLocal
from .events import broker
from .models import Run, RunEvent, RunStatus

OUTPUT_DIR = Path("output")


class _SeqCounter:
    """Monotonic per-run event ordering. A plain "insert order" isn't
    reliable here — frontend/backend/devops engineers run concurrently, so
    their on_event calls can interleave — this single counter, shared by
    every emit for one run, is what the UI sorts on."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


async def _record_event(
    run_id: str, seq_counter: _SeqCounter, source: str, event_type: str, detail: str, extra: dict
) -> None:
    db = SessionLocal()
    try:
        row = RunEvent(
            run_id=run_id,
            seq=seq_counter.next(),
            source=source,
            event_type=event_type,
            detail=detail,
            extra=json.dumps(extra) if extra else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        await broker.publish(
            run_id,
            {
                "id": row.id,
                "seq": row.seq,
                "at": row.at.isoformat(),
                "source": row.source,
                "event_type": row.event_type,
                "detail": row.detail,
                "extra": row.extra,
            },
        )
    finally:
        db.close()


async def run_pipeline(run_id: str, goal: str, agent_cls: type[ClaudeCodeAgent] = ClaudeCodeAgent) -> None:
    """`agent_cls` defaults to the real ClaudeCodeAgent; tests pass
    ScriptedClaudeCodeAgent to exercise this whole flow for free."""
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        workspace = Path(run.workspace_path)
        checkpoint_path = Path(run.checkpoint_path)
    finally:
        db.close()

    seq_counter = _SeqCounter()

    async def on_event(source: str, event_type: str, detail: str, extra: dict) -> None:
        await _record_event(run_id, seq_counter, source, event_type, detail, extra)

    # FailFastMonitor wraps on_event so a crash from any single agent
    # (e.g. hitting the turn cap) stops the whole run immediately instead
    # of leaving the other engineers to keep working — and spending real
    # Claude Code quota — against a pipeline that's already doomed. See
    # aiteam/runner.py for why this doesn't rely on GraphFlow's own error
    # propagation (confirmed live: it doesn't work reliably).
    monitor = FailFastMonitor(on_event)

    workspace.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    team, agents = build_team(workspace, agent_cls=agent_cls, on_event=monitor.on_event)

    await _record_event(run_id, seq_counter, "system", "run_started", goal, {})

    async def on_message(message: BaseChatMessage) -> None:
        artifact_dir = workspace / ARTIFACT_DIR_NAME
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / f"{message.source}.md").write_text(message.to_text())

        # Once the architect's TECH DESIGN lands, size each engineer's turn
        # budget to the task instead of leaving them all on the static
        # default — see pipeline.py's ARCHITECT_PROMPT "Turn Budget
        # Estimate" section and apply_turn_budget_from_architect().
        applied_budget = apply_turn_budget_from_architect(message, agents)
        if applied_budget:
            await _record_event(
                run_id, seq_counter, "system", "turn_budget_applied", str(applied_budget), {}
            )

        state = await team.save_state()
        checkpoint_path.write_text(
            json.dumps({"stamp": run_id, "goal": goal, "workspace": str(workspace), "team_state": state})
        )

    try:
        stop_reason = await run_team(team, goal, on_message, monitor)
    except Exception as exc:
        await _record_event(run_id, seq_counter, "system", "run_failed", str(exc), {})
        db = SessionLocal()
        try:
            run = db.get(Run, run_id)
            if run is not None:
                run.status = RunStatus.failed
                run.stop_reason = str(exc)
                db.add(run)
                db.commit()
        finally:
            db.close()
        return

    await _record_event(run_id, seq_counter, "system", "run_completed", stop_reason, {})
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is not None:
            run.status = RunStatus.completed
            run.stop_reason = stop_reason
            db.add(run)
            db.commit()
    finally:
        db.close()
