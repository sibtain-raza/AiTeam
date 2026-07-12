import asyncio
import json
from datetime import datetime
from typing import Coroutine

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, get_current_user_sse
from ..events import broker
from ..models import Run, RunEvent, RunStatus, User
from .. import pipeline_runner
from ..pipeline_runner import run_pipeline
from ..schemas import CreateRunRequest, RunEventOut, RunSummary

router = APIRouter(prefix="/runs", tags=["runs"])

# Fire-and-forget pipeline runs, kept alive by a strong reference: asyncio
# only holds a weak reference to a task via create_task(), so without this
# a task can be garbage-collected mid-run — a real, documented asyncio
# gotcha, not a hypothetical one.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


@router.post("", response_model=RunSummary, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RunSummary:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    # Read via the module attribute (not an import-time copy) so tests can
    # patch server.pipeline_runner.OUTPUT_DIR to a temp dir and have it
    # take effect here too.
    workspace = pipeline_runner.OUTPUT_DIR / "workspace" / stamp
    checkpoint_path = pipeline_runner.OUTPUT_DIR / "checkpoints" / f"{stamp}.json"

    run = Run(
        owner_id=user.id,
        goal=body.goal,
        status=RunStatus.running,
        workspace_path=str(workspace),
        checkpoint_path=str(checkpoint_path),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    # Fire-and-forget on the running event loop — Phase 1 has no persistent
    # task queue, so a server restart mid-run orphans it (the checkpoint
    # file is still written incrementally; recovery is the CLI's existing
    # `--resume` flow, not yet wired into this API — see README).
    _spawn(run_pipeline(run.id, body.goal))
    return RunSummary.model_validate(run)


@router.get("", response_model=list[RunSummary])
def list_runs(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[RunSummary]:
    runs = db.query(Run).filter(Run.owner_id == user.id).order_by(Run.created_at.desc()).all()
    return [RunSummary.model_validate(r) for r in runs]


@router.get("/{run_id}", response_model=RunSummary)
def get_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> RunSummary:
    run = db.get(Run, run_id)
    if run is None or run.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return RunSummary.model_validate(run)


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_sse),
) -> StreamingResponse:
    run = db.get(Run, run_id)
    if run is None or run.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    # Subscribe *before* reading history: if we snapshotted history first,
    # any event published between that snapshot and subscribing would be
    # lost forever (missing from the snapshot, missed by the live queue).
    # Subscribing first can instead double-deliver an event into both the
    # snapshot and the queue, which the `seq` dedupe below resolves.
    already_finished = run.status != RunStatus.running
    queue = broker.subscribe(run_id) if not already_finished else None
    history = [RunEventOut.model_validate(e).model_dump(mode="json") for e in run.events]
    last_seq = history[-1]["seq"] if history else -1

    async def event_source():
        for event in history:
            yield f"data: {json.dumps(event)}\n\n"
        if already_finished:
            yield "event: end\ndata: {}\n\n"
            return

        assert queue is not None
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event["seq"] <= last_seq:
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event["event_type"] in ("run_completed", "run_failed"):
                    yield "event: end\ndata: {}\n\n"
                    break
        finally:
            broker.unsubscribe(run_id, queue)

    return StreamingResponse(event_source(), media_type="text/event-stream")
