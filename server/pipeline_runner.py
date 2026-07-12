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
import os
from pathlib import Path

from autogen_agentchat.messages import BaseChatMessage

from looper.artifacts import find_blockers, validate_artifact
from looper.claude_code_agent import ClaudeCodeAgent
from looper.pipeline import (
    ARTIFACT_DIR_NAME,
    apply_turn_budget_from_architect,
    apply_deploy_verify_budget,
    apply_visual_qa_budget,
    build_team,
)
from looper.run_memory import (
    calibration_hint,
    defect_history_hints,
    load_runs,
    record_run,
    summarize_run,
)
from looper.runner import FailFastMonitor, run_team

from .db import SessionLocal
from .events import broker
from .models import Run, RunEvent, RunStatus

# Same env override as main.py's --output-dir default. Tests point this at
# a temp dir (see tests/test_server_e2e.py) — scripted runs used to write
# workspaces into the real ./output, and once cross-run memory landed that
# meant test data could leak into the architect calibration hint for real
# runs. Read at import time; tests that can't control import order patch
# `server.pipeline_runner.OUTPUT_DIR` directly (routes/runs.py reads it via
# the module attribute, not an import-time copy, for exactly this reason).
OUTPUT_DIR = Path(os.environ.get("LOOPER_OUTPUT_DIR", "output"))


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
    # looper/runner.py for why this doesn't rely on GraphFlow's own error
    # propagation (confirmed live: it doesn't work reliably).
    _run_budget_raw = os.environ.get("LOOPER_MAX_RUN_BUDGET_USD", "")
    monitor = FailFastMonitor(
        on_event, max_run_budget_usd=float(_run_budget_raw) if _run_budget_raw else None
    )

    workspace.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    # Cross-run memory: same calibration-hint flow as main.py — shared
    # output/memory/runs.jsonl, so CLI and web runs learn from each other.
    past_runs = load_runs(OUTPUT_DIR)
    hint = calibration_hint(past_runs)
    team, agents = build_team(
        workspace,
        agent_cls=agent_cls,
        on_event=monitor.on_event,
        architect_addendum=hint,
        output_dir=OUTPUT_DIR,
        # Per-engineer recurring-defect hints from past runs' QA reports —
        # same cross-run memory flow as main.py.
        role_addenda=defect_history_hints(past_runs),
    )

    await _record_event(run_id, seq_counter, "system", "run_started", goal, {})

    async def on_message(message: BaseChatMessage) -> None:
        artifact_dir = workspace / ARTIFACT_DIR_NAME
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / f"{message.source}.md").write_text(message.to_text())

        # Advisory protocol checks (see looper/artifacts.py) — recorded as
        # events so the UI can show them, mirroring main.py's printed
        # warnings. Neither stops the run.
        for problem in validate_artifact(message.source, message.to_text()):
            await _record_event(run_id, seq_counter, message.source, "artifact_warning", problem, {})
        for blocker in find_blockers(message.to_text()):
            await _record_event(run_id, seq_counter, message.source, "blocker_raised", blocker, {})

        # Once the architect's TECH DESIGN lands, size each engineer's turn
        # budget to the task instead of leaving them all on the static
        # default — see pipeline.py's ARCHITECT_PROMPT "Turn Budget
        # Estimate" section and apply_turn_budget_from_architect().
        applied_budget = apply_turn_budget_from_architect(message, agents)
        if applied_budget:
            await _record_event(
                run_id, seq_counter, "system", "turn_budget_applied", str(applied_budget), {}
            )
        # Same idea, for QA's Visual QA pass (ARCHITECT_PROMPT's "Visual QA"
        # section) — only bumps QA's budget when the architect requested it.
        visual_qa_extra = apply_visual_qa_budget(message, agents, agents["qa_engineer"].max_turns)
        if visual_qa_extra:
            await _record_event(
                run_id, seq_counter, "system", "visual_qa_budget_applied", f"+{visual_qa_extra} turns", {}
            )
        # ...and QA's deploy-verification pass; stacks on top of the visual
        # bump (called after it, with QA's then-current budget as base).
        deploy_extra = apply_deploy_verify_budget(message, agents, agents["qa_engineer"].max_turns)
        if deploy_extra:
            await _record_event(
                run_id, seq_counter, "system", "deploy_verify_budget_applied", f"+{deploy_extra} turns", {}
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
    record_run(OUTPUT_DIR, summarize_run(workspace, run_id, goal, stop_reason))
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
