"""Cross-run long-term memory: what past runs cost, where they struggled,
and what the next run should learn from it.

Within a run, agents already have short-term memory (`_history` replay,
disk artifacts, checkpoints). Across runs there was nothing: every run
started blind, so a systematic pattern — like the architect's turn-budget
estimates running low on every single engineer in a run — could only be
noticed by a human reading logs. This module closes that loop with the
smallest thing that works:

- `summarize_run()` distills one finished run into a compact record by
  aggregating the run's own SDK interaction log
  (`.pipeline-docs/sdk-interactions.jsonl` — see pipeline.SDK_LOG_FILE_NAME),
  which already captures per-turn cost, duration, and max-turns hits.
  Nothing new is instrumented; the log was already the ground truth.
- `record_run()` appends/replaces that record in `<output>/memory/runs.jsonl`
  (keyed by stamp, so a resumed run's final record supersedes any earlier
  one for the same run instead of double-counting).
- `calibration_hint()` turns the accumulated records into a short prompt
  addendum for the architect: if past engineer sessions have been running
  out of their estimated budgets, say so, with the real numbers. build_team()
  appends it to ARCHITECT_PROMPT via its `architect_addendum` parameter —
  the same pattern as the TURN BUDGET line injected in on_messages():
  runtime-composed prompt context, deliberately NOT an edit to
  ARCHITECT_PROMPT/SPEC.md themselves, so the spec-sync rule is untouched.

Records are only written when a run actually completes (main.py's and
server/pipeline_runner.py's success paths). A run that crashes and is later
`--resume`d gets recorded once, at its eventual completion, with its full
log — recording on the crash path too would double-count the shared
workspace log. A run that permanently fails is simply not recorded; the
memory is a calibration aid, not an audit trail (checkpoints and the SDK
log itself remain the audit trail).
"""

import json
from pathlib import Path

from .pipeline import ARTIFACT_DIR_NAME, SDK_LOG_FILE_NAME
from .termination import last_line

MEMORY_DIR_NAME = "memory"
MEMORY_FILE_NAME = "runs.jsonl"

_ENGINEER_AGENTS = ("frontend_engineer", "backend_engineer", "devops_engineer")
_MAX_TURNS_ERROR = "hit max_turns before finishing"


def memory_path(output_dir: Path) -> Path:
    return output_dir / MEMORY_DIR_NAME / MEMORY_FILE_NAME


def summarize_run(workspace: Path, stamp: str, goal: str, stop_reason: str | None) -> dict:
    """One compact record for a finished run, aggregated from its SDK
    interaction log. Tolerates a missing/partial log (e.g. a run scripted
    by tests that never wrote one): the totals just come out zero."""
    per_agent: dict[str, dict] = {}
    total_cost = 0.0
    total_duration_ms = 0
    qa_fail_rounds = 0

    log_file = workspace / ARTIFACT_DIR_NAME / SDK_LOG_FILE_NAME
    if log_file.exists():
        for line in log_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            agent = entry.get("agent", "unknown")
            stats = per_agent.setdefault(
                agent,
                {"sessions": 0, "cost_usd": 0.0, "max_turns_hits": 0, "last_max_turns": None},
            )
            stats["sessions"] += 1
            stats["cost_usd"] += entry.get("cost_usd") or 0.0
            stats["last_max_turns"] = entry.get("max_turns")
            if entry.get("error") == _MAX_TURNS_ERROR:
                stats["max_turns_hits"] += 1
            total_cost += entry.get("cost_usd") or 0.0
            total_duration_ms += entry.get("duration_ms") or 0
            if agent == "qa_engineer" and last_line(entry.get("response") or "") == "QA_FAIL":
                qa_fail_rounds += 1

    return {
        "stamp": stamp,
        "goal": goal,
        "stop_reason": stop_reason,
        # Where this run's full SDK log lives — looper.report reads it for
        # per-session detail (turns used vs budget, defect parsing) so the
        # memory record itself can stay compact.
        "workspace": str(workspace),
        "total_cost_usd": round(total_cost, 4),
        "total_duration_ms": total_duration_ms,
        "qa_fail_rounds": qa_fail_rounds,
        "per_agent": per_agent,
    }


def record_run(output_dir: Path, record: dict) -> Path:
    """Append `record` to the cross-run memory file, replacing any earlier
    record with the same stamp (a resumed run finishing supersedes whatever
    partial state an earlier completion of the same stamp wrote).

    The read-filter-rewrite is guarded by an advisory flock on a sidecar
    lock file: two concurrent web runs completing at the same moment would
    otherwise race the rewrite and silently drop one record. POSIX-only
    (fcntl) — on platforms without it the write proceeds unguarded, which
    just reverts to the pre-lock behavior."""
    path = memory_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    try:
        import fcntl

        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                records = [r for r in load_runs(output_dir) if r.get("stamp") != record.get("stamp")]
                records.append(record)
                path.write_text("".join(json.dumps(r) + "\n" for r in records))
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except ImportError:
        records = [r for r in load_runs(output_dir) if r.get("stamp") != record.get("stamp")]
        records.append(record)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def load_runs(output_dir: Path) -> list[dict]:
    path = memory_path(output_dir)
    if not path.exists():
        return []
    runs = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return runs


def calibration_hint(runs: list[dict], window: int = 10) -> str | None:
    """A prompt addendum for the architect, derived from the last `window`
    recorded runs: how often engineer sessions ran out of their estimated
    turn budget. None when there's nothing to learn (no history, or no
    engineer ever hit its budget — silence beats noise in a prompt)."""
    recent = runs[-window:]
    sessions = 0
    hits = 0
    worst: tuple[int, str] | None = None
    for run in recent:
        for agent, stats in run.get("per_agent", {}).items():
            if agent not in _ENGINEER_AGENTS:
                continue
            sessions += stats.get("sessions", 0)
            agent_hits = stats.get("max_turns_hits", 0)
            hits += agent_hits
            if agent_hits and (worst is None or agent_hits > worst[0]):
                worst = (agent_hits, agent)
    if sessions == 0 or hits == 0:
        return None
    return (
        f"CALIBRATION FROM PAST RUNS: across your last {len(recent)} recorded run(s), "
        f"{hits} of {sessions} engineer sessions ran out of their estimated turn budget "
        f"before finishing (most affected: {worst[1]}). Your Turn Budget Estimates have "
        f"historically run low. Size toward the upper end of the ranges in your guide — "
        f"an unused turn costs nothing, while an exhausted budget produces incomplete, "
        f"unverified work and a full QA rework loop."
    )
