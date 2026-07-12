"""GraphFlow pipeline: PM → Architect → (FE ∥ BE ∥ OPS) → QA → UAT, with rework loops."""

import asyncio
import os
import re
from pathlib import Path
from typing import Mapping, Sequence

from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import BaseChatMessage, TextMessage
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow

from .claude_code_agent import ClaudeCodeAgent, OnEvent
from .prompts import (
    ARCHITECT_PROMPT,
    BE_PROMPT,
    FE_PROMPT,
    GLOBAL_RULES,
    OPS_PROMPT,
    PM_PROMPT,
    QA_PROMPT,
    REPORTER_PROMPT,
    SCOPE_VALIDATOR_PROMPT,
    UAT_PROMPT,
)
from .termination import TokenCountTermination, last_line

MAX_QA_LOOPS = 3
# Base pipeline is 7 messages; each QA loop re-runs 3 engineers + QA (4).
# Cap generously above 7 + 3*4 + UAT-reject re-scope headroom.
MAX_MESSAGES = 40

# Where main.py persists each agent's latest artifact inside the run's
# workspace (<workspace>/.pipeline-docs/<source>.md), and where the
# pointer_files wiring below tells tool-using agents to find them. The two
# uses must agree — hence one shared constant. Dot-prefixed so OPS's
# Dockerfile/.dockerignore conventions treat it as metadata, not app code.
ARTIFACT_DIR_NAME = ".pipeline-docs"

# Every real claude_agent_sdk call any agent makes, across the whole run —
# one JSON line per turn (prompt sent, response received, cost/duration,
# or the error if the session failed) — lives alongside the other
# per-run artifacts under the same dot-prefixed dir. See
# ClaudeCodeAgent._log_interaction() for the entry format.
SDK_LOG_FILE_NAME = "sdk-interactions.jsonl"

# Bounds for the architect's per-engineer turn-budget estimate (see
# ARCHITECT_PROMPT's "Turn Budget Estimate" section and
# apply_turn_budget_from_architect() below). Clamping protects both ends:
# a too-low estimate would reproduce the exact bug this mechanism exists to
# fix (an engineer starved of turns on a real task), and a too-high one
# would let one miscalibrated estimate blow the run's cost far past what
# the fixed ENGINEER_MAX_TURNS default used to risk.
MIN_ENGINEER_TURNS = 8
MAX_ENGINEER_TURNS = 50

# Ceilings for the EXTRA turns the architect's QA gates (see
# ARCHITECT_PROMPT's "Visual QA" and "Deploy Verification" sections /
# apply_visual_qa_budget() and apply_deploy_verify_budget() below) can add
# to QA's baseline QA_MAX_TURNS. Both workflows are bounded even at their
# heaviest (Visual QA: build + preview server + Playwright captures;
# deploy verify: image builds + compose up + health/request checks +
# teardown) — the caps limit a miscalibrated architect estimate the same
# way MAX_ENGINEER_TURNS caps the per-engineer estimate above. A run where
# the architect grants both gates gets both extras, cumulatively.
MAX_VISUAL_QA_EXTRA_TURNS = 30
MAX_DEPLOY_VERIFY_EXTRA_TURNS = 25

_TURN_BUDGET_LINE = re.compile(r"^\s*(FE|BE|OPS)\s*:\s*(\d+)", re.MULTILINE)
_TURN_BUDGET_ROLE_TO_AGENT = {"FE": "frontend_engineer", "BE": "backend_engineer", "OPS": "devops_engineer"}

_VISUAL_QA_LINE = re.compile(r"VISUAL_QA\s*:\s*(YES|NO)\s*(?::\s*(\d+))?", re.IGNORECASE)
_DEPLOY_VERIFY_LINE = re.compile(r"DEPLOY_VERIFY\s*:\s*(YES|NO)\s*(?::\s*(\d+))?", re.IGNORECASE)


def parse_turn_budget(text: str) -> dict[str, int]:
    """Extract the architect's per-role turn estimates from its TECH
    DESIGN's "## Turn Budget Estimate" section (format one line per role:
    "FE: <N> — <reason>"). Each parsed value is clamped into
    [MIN_ENGINEER_TURNS, MAX_ENGINEER_TURNS] — EXCEPT 0, which is left
    untouched: per ARCHITECT_PROMPT, a budget of exactly 0 is the
    architect's deliberate signal that this role has zero tagged tasks,
    and `ClaudeCodeAgent.on_messages()` treats `_max_turns == 0` as "skip
    this engineer's session entirely" (see its docstring). Clamping 0 up
    to MIN_ENGINEER_TURNS would silently turn every "no work for this
    role" signal back into a real (wasted) session. A role that's missing
    or didn't parse is simply absent from the result — the caller keeps
    whatever budget that engineer already has (the static ENGINEER_MAX_TURNS
    default from build_team(), unless a prior turn already set one).
    """
    budget: dict[str, int] = {}
    for role, value in _TURN_BUDGET_LINE.findall(text):
        n = int(value)
        budget[role] = n if n == 0 else max(MIN_ENGINEER_TURNS, min(MAX_ENGINEER_TURNS, n))
    return budget


def parse_visual_qa_extra_turns(text: str) -> int:
    """Extract the architect's "## Visual QA" verdict from its TECH DESIGN
    (format: a single `VISUAL_QA: YES: <N> — <reason>` or `VISUAL_QA: NO —
    <reason>` line). Returns the extra tool-call turns to add to QA's
    baseline budget — 0 for `NO`, a missing section, or a `YES` with no
    parseable number (never silently grants extra turns without an
    explicit count). Clamped to `[0, MAX_VISUAL_QA_EXTRA_TURNS]`.
    """
    match = _VISUAL_QA_LINE.search(text)
    if match is None or match.group(1).upper() != "YES" or match.group(2) is None:
        return 0
    return max(0, min(MAX_VISUAL_QA_EXTRA_TURNS, int(match.group(2))))


def apply_visual_qa_budget(
    message: BaseChatMessage, agents: Mapping[str, ClaudeCodeAgent], base_turns: int
) -> int:
    """If `message` is solution_architect's TECH DESIGN and it requests
    Visual QA, bump `qa_engineer`'s turn budget from `base_turns` (its
    normal baseline — QA_MAX_TURNS at `build_team()` time, unless a prior
    call already changed it) to `base_turns + <parsed extra>`, via the same
    `set_max_turns()` seam `apply_turn_budget_from_architect()` uses for
    FE/BE/OPS. Returns the extra amount applied (0 if not applicable) so a
    caller can log it. No-op if `qa_engineer` is missing from `agents`.

    Kept separate from `apply_turn_budget_from_architect()` rather than
    folded into it: that function's contract is specifically "the Turn
    Budget Estimate section, applied to FE/BE/OPS" and is unit-tested as
    such — this is a different section of the same message governing a
    different role, tested independently (see test_visual_qa.py) but
    always called alongside it at the same call sites (main.py's live
    on_message, its --resume path, and server/pipeline_runner.py) since
    both are derived from the one architect message.
    """
    if message.source != "solution_architect":
        return 0
    extra = parse_visual_qa_extra_turns(message.to_text())
    if extra == 0:
        return 0
    qa = agents.get("qa_engineer")
    if qa is None:
        return 0
    qa.set_max_turns(base_turns + extra)
    return extra


def parse_deploy_verify_extra_turns(text: str) -> int:
    """Extract the architect's "## Deploy Verification" verdict from its
    TECH DESIGN (format: a single `DEPLOY_VERIFY: YES: <N> — <reason>` or
    `DEPLOY_VERIFY: NO — <reason>` line). Same contract as
    `parse_visual_qa_extra_turns()`: 0 for `NO`, a missing section, or a
    `YES` with no parseable number; clamped to
    `[0, MAX_DEPLOY_VERIFY_EXTRA_TURNS]`.
    """
    match = _DEPLOY_VERIFY_LINE.search(text)
    if match is None or match.group(1).upper() != "YES" or match.group(2) is None:
        return 0
    return max(0, min(MAX_DEPLOY_VERIFY_EXTRA_TURNS, int(match.group(2))))


def apply_deploy_verify_budget(
    message: BaseChatMessage, agents: Mapping[str, ClaudeCodeAgent], base_turns: int
) -> int:
    """Deploy-verification counterpart to `apply_visual_qa_budget()` (see
    its docstring — identical contract, different gate line): grants QA the
    extra turns to build the shipped images, run the compose stack, hit
    the health endpoints and a real request path, and tear it down. Called
    AFTER apply_visual_qa_budget() at every call site with QA's
    then-current budget as `base_turns`, so a design that grants both
    gates stacks both extras.
    """
    if message.source != "solution_architect":
        return 0
    extra = parse_deploy_verify_extra_turns(message.to_text())
    if extra == 0:
        return 0
    qa = agents.get("qa_engineer")
    if qa is None:
        return 0
    qa.set_max_turns(base_turns + extra)
    return extra


def apply_turn_budget_from_architect(
    message: BaseChatMessage, agents: Mapping[str, ClaudeCodeAgent]
) -> dict[str, int]:
    """If `message` is solution_architect's TECH DESIGN, parse its turn
    budget estimate and apply it to the FE/BE/OPS agent objects via
    `ClaudeCodeAgent.set_max_turns()` — raising or lowering their ceiling
    from the static default set at `build_team()` time to a task-specific
    one, now that the architect has actually scoped the work. Returns the
    budget that was applied (empty if `message` wasn't from the architect,
    or nothing parsed) so a caller can log/emit it. No-ops for a role
    missing from `agents` (e.g. a test team that only wired a subset).

    Shared by `main.py` and `server/pipeline_runner.py` — both `on_message`
    callbacks need this identical behavior, so it lives here once rather
    than being reimplemented per entry point.
    """
    if message.source != "solution_architect":
        return {}
    budget = parse_turn_budget(message.to_text())
    for role, n in budget.items():
        agent = agents.get(_TURN_BUDGET_ROLE_TO_AGENT[role])
        if agent is not None:
            agent.set_max_turns(n)
    return budget


def reapply_turn_budget_on_resume(
    workspace: Path, agents: Mapping[str, ClaudeCodeAgent]
) -> dict[str, int]:
    """On `--resume`, re-derive the architect's per-engineer turn budget —
    `set_max_turns()` overrides are pure in-memory state and don't survive
    a fresh `build_team()` (only `_history` does, via `save_state()`).

    Reads the architect's TECH DESIGN directly from its on-disk pointer
    file (`<workspace>/.pipeline-docs/solution_architect.md`, written
    unconditionally by `main.py`'s `on_message()` the instant the
    architect's turn completes) rather than searching any specific
    engineer's replayed `_history` for it (an earlier version of this did
    exactly that, via a since-removed `find_message_from()` method).

    That mattered because of a real, reproduced race, not a hypothetical
    one: `on_messages()` only appends to `_history` when it is actually
    CALLED — and if the checkpoint that ends up on disk was saved right
    after the architect's turn but before GraphFlow had dispatched any
    engineer's turn yet (plausible and observed live: checkpointing is
    synchronous per completed message, while the FE/BE/OPS fan-out is
    asynchronous and can crash — e.g. an account-wide Claude Code session
    limit — before any of the three has been given its turn at all), then
    NONE of the engineers' `_history` yet contains the architect's message,
    so a lookup rooted in one specific agent's history silently found
    nothing. The resumed run still correctly re-dispatched all three
    engineers (GraphFlow's own state was self-consistent — nothing to
    patch there), but every one of them silently reverted to the static
    `ENGINEER_MAX_TURNS` default instead of the architect's real estimate.
    Confirmed live: a run with a 38/38/22 FE/BE/OPS budget resumed after an
    account session-limit crash and re-ran all three at the static default
    of 20 — two of them then hit THAT ceiling too, on turns that had
    already been proven to need more.

    The on-disk pointer file has no equivalent race: `main.py` writes it
    the moment the architect's message is produced, regardless of whether
    any downstream agent has been dispatched yet. No-op (returns `{}`) if
    the file doesn't exist (the architect hasn't completed a turn on this
    checkpoint) or has no parseable budget section.

    Also re-applies QA's Visual QA and Deploy Verification extra-turns
    budgets (`apply_visual_qa_budget()` / `apply_deploy_verify_budget()`)
    from the same on-disk design, for the identical reason — those
    overrides are pure in-memory state too. Included in the returned dict
    under `"QA_VISUAL"` / `"QA_DEPLOY"` keys when non-zero, alongside the
    FE/BE/OPS keys, so callers can log everything re-applied in one line.
    """
    design_path = workspace / ARTIFACT_DIR_NAME / "solution_architect.md"
    if not design_path.exists():
        return {}
    message = TextMessage(content=design_path.read_text(), source="solution_architect")
    budget = apply_turn_budget_from_architect(message, agents)
    qa = agents.get("qa_engineer")
    if qa is not None:
        visual_extra = apply_visual_qa_budget(message, agents, qa.max_turns)
        if visual_extra:
            budget["QA_VISUAL"] = visual_extra
        deploy_extra = apply_deploy_verify_budget(message, agents, qa.max_turns)
        if deploy_extra:
            budget["QA_DEPLOY"] = deploy_extra
    return budget


def recover_stuck_agents(team_state: dict) -> list[str]:
    """Patch a raw GraphFlow checkpoint dict so a crashed-mid-fan-out agent
    actually retries on `--resume`, instead of the run silently completing
    zero turns.

    The gap this works around lives in AutoGen itself, not in this
    codebase: `GraphFlowManagerState.select_speaker()` (see
    `_digraph_group_chat.py`) drains its `_ready` queue and resets a
    dispatched node's activation bookkeeping the INSTANT it's selected to
    speak — before that node's agent has actually run, let alone completed.
    There is no "dispatched but not finished" state in GraphFlow's own
    checkpoint. So when one of several parallel siblings (e.g. `devops
    _engineer` alongside `frontend_engineer`/`backend_engineer`) crashes
    mid-turn, the checkpoint saved right after captures `_ready` already
    empty and the crashed node's own activation counters already reset to
    their pre-triggered state — nothing will ever re-enqueue it, because
    the parent (`solution_architect`) already sent its one and only
    message and isn't going to run again. Confirmed live: resuming such a
    checkpoint via `team.load_state()` unmodified produces a run that
    matches every termination check on its very first step and reports
    "group chat is stopped" with zero new turns.

    The fix is possible because `ClaudeCodeAgent` tracks its own
    `_turn_in_progress` flag (set True when `on_messages()` accepts a turn,
    False only once it returns successfully — see claude_code_agent.py),
    which DOES survive the crash in the checkpoint, since it's part of
    that agent's own `save_state()`. Any agent whose saved state still has
    `turn_in_progress: True` was dispatched but never finished — this
    function appends each such name back onto `GraphManager`'s saved
    `ready` list (a plain list of node names; `select_speaker()` just pops
    from it) so the very next `select_speaker()` call after resume
    re-dispatches it. The crashed agent's own `message_buffer` (a
    separate, container-level list — untouched by any of this) still
    holds the exact messages it needs, because `ChatAgentContainer` only
    clears that buffer on a *successful* completion, never on a crash.

    Mutates `team_state` in place (call before `team.load_state(
    team_state)`) and returns the list of agent names it recovered, purely
    so a caller can log what happened.
    """
    agent_states = team_state.get("agent_states", {})
    graph_manager = agent_states.get("GraphManager")
    if graph_manager is None:
        return []
    ready = graph_manager.setdefault("ready", [])
    recovered = []
    for name, state in agent_states.items():
        if name == "GraphManager":
            continue
        if state.get("agent_state", {}).get("turn_in_progress") and name not in ready:
            ready.append(name)
            recovered.append(name)
    return recovered


def reset_pending_activation_flags(team_state: dict) -> list[str]:
    """Patch a raw GraphFlow checkpoint dict so a node sitting in the
    checkpointed `ready` queue can be re-dispatched into the SAME "any"
    -semantics activation group again later — e.g. an engineer whose only
    checkpointed dispatch was the architect's fan-out must still be
    reachable by a later QA_FAIL rework edge sharing that group.

    A second, deeper gap in AutoGen's own GraphFlow checkpointing, sibling
    to the one `recover_stuck_agents()` fixes above but NOT the same bug —
    read that docstring first. `GraphFlowManagerState` only clears an "any"
    group's `enqueued_any` flag back to False inside `select_speaker()`, at
    the exact instant a node is POPPED off `ready` for dispatch (see
    `_reset_triggered_activation_groups()` in `_digraph_group_chat.py`) —
    and that reset itself depends on `_triggered_activation_groups`, a
    dict that ISN'T part of `save_state()`/`load_state()` at all. Looper's
    own checkpointing (`main.py`'s `on_message()`) only fires once a
    message is fully PRODUCED — so a checkpoint saved while a node is
    still sitting in `ready` (added there, not yet popped) captures
    `enqueued_any` still `True` for its group. `select_speaker()` would
    have reset it moments later in the crashed process's now-lost memory,
    but the checkpoint never saw that.

    Confirmed live, not hypothetical: `frontend_engineer`/`backend
    _engineer`/`devops_engineer` were all dispatched by the architect's
    fan-out and started real Claude Code sessions (logged in
    `sdk-interactions.jsonl`), then all three hit an account-wide session
    limit before any completed — so the checkpoint that ended up on disk
    pre-dates the dispatch-time bookkeeping update entirely. On `--resume`,
    GraphFlow correctly re-ran all three (nothing wrong with `ready`
    itself — this is why `recover_stuck_agents()` above found nothing:
    `turn_in_progress` was still False for all three, since their crash
    happened before ANY checkpoint captured them mid-turn). But with
    `enqueued_any` still `True` for their groups, the later QA_FAIL edge's
    `if not enqueued_any[...]` guard in `update_message_thread()` silently
    swallowed the re-activation: none of the three engineers got a rework
    turn, and the run printed "Digraph execution is complete" one QA_FAIL
    into what should have been a 3-loop budget — `MAX_QA_LOOPS` never even
    got a chance to matter.

    Resets `enqueued_any[node][group] = False` for every group of every
    node in `ready` (call after `recover_stuck_agents()`, so newly
    -recovered nodes are covered too, before `team.load_state(team_state)`)
    — exactly what `select_speaker()` would have done for them, performed
    here instead since that in-memory update didn't survive the crash.
    Safe even for a node whose flag is already False (a no-op) or that has
    no "any" groups at all ("all"-semantics groups use `remaining`
    instead, untouched by this). Returns `"node/group"` strings actually
    flipped, purely for logging.
    """
    graph_manager = team_state.get("agent_states", {}).get("GraphManager")
    if graph_manager is None:
        return []
    ready = graph_manager.get("ready", [])
    enqueued_any = graph_manager.get("enqueued_any", {})
    reset: list[str] = []
    for node in ready:
        for group, is_enqueued in enqueued_any.get(node, {}).items():
            if is_enqueued:
                enqueued_any[node][group] = False
                reset.append(f"{node}/{group}")
    return reset


def verdict_is(token: str):
    """Edge condition: the source agent's final non-empty line is exactly `token`.

    Matching the last line (not a substring) prevents a message that merely
    *mentions* both tokens (e.g. quoting the output template) from firing
    both conditional edge sets at once.
    """

    def check(message: BaseChatMessage) -> bool:
        return last_line(message.to_text()) == token

    return check


class QaVerdictRouter:
    """Stateful edge conditions for QA's outgoing edges: graceful
    degradation instead of a hard kill when the rework budget runs out.

    Before this, the 3rd QA_FAIL tripped TokenCountTermination and the run
    died with PIPELINE_FAILED — all the work on disk, no verdict, no
    report. A real team at that point doesn't burn the building down; it
    judges shippability. Now: QA_FAIL #1 and #2 route to the engineers for
    rework exactly as before, and QA_FAIL #`max_fails` routes to UAT
    instead, whose prompt (see UAT_PROMPT's rework-exhausted rule) makes
    the final call — approve with the open defects documented prominently
    as Known Issues, or reject into the one re-scope loop. A BLOCKER is
    never approvable.

    Mechanics: both edge conditions may be called for the same QA message
    (GraphFlow evaluates every outgoing edge's condition against it), so
    the fail counter registers each distinct message object exactly once —
    `id()` is stable here because messages are retained in GraphFlow's own
    message thread for the run's lifetime.

    Two documented limits, both bounded by the safety terminations in
    build_team():
    - The count is cumulative across the whole run, including after a UAT
      re-scope: a re-scoped attempt that fails QA again goes straight to
      final judgment rather than earning a fresh rework budget — the run
      has already consumed its patience.
    - The count is in-memory only (edge conditions aren't part of
      GraphFlow's checkpoint), so a `--resume` resets it and can allow up
      to `max_fails - 1` additional rework loops. Accepted: the loops are
      productive work, and the raised TokenCountTermination safety net
      plus MaxMessageTermination still bound the run.
    """

    def __init__(self, max_fails: int) -> None:
        self._max_fails = max_fails
        self.fail_count = 0
        self._counted: set[int] = set()

    def _register_fail(self, message: BaseChatMessage) -> int:
        key = id(message)
        if key not in self._counted:
            self._counted.add(key)
            self.fail_count += 1
        return self.fail_count

    def rework(self, message: BaseChatMessage) -> bool:
        """QA → engineers: fires for a QA_FAIL while rework budget remains."""
        if last_line(message.to_text()) != "QA_FAIL":
            return False
        return self._register_fail(message) < self._max_fails

    def to_uat(self, message: BaseChatMessage) -> bool:
        """QA → UAT: fires for QA_PASS (the normal path), or for the final
        QA_FAIL once the rework budget is exhausted (the shippability
        judgment path)."""
        verdict = last_line(message.to_text())
        if verdict == "QA_PASS":
            return True
        if verdict == "QA_FAIL":
            return self._register_fail(message) >= self._max_fails
        return False


def build_team(
    workspace: Path,
    agent_cls: type[ClaudeCodeAgent] = ClaudeCodeAgent,
    on_event: OnEvent | None = None,
    architect_addendum: str | None = None,
    output_dir: Path | None = None,
    role_addenda: Mapping[str, str] | None = None,
) -> tuple[GraphFlow, dict[str, ClaudeCodeAgent]]:
    """Build the pipeline. Every agent runs via a real claude_agent_sdk
    session authenticated through the `claude` CLI's own OAuth login — no
    ANTHROPIC_API_KEY/OPENAI_API_KEY needed. See claude_code_agent.py.

    Returns `(team, agents)` — `agents` maps each role name to its actual
    `ClaudeCodeAgent` object, since `GraphFlow` itself exposes no public way
    to look up a participant after construction. This is what
    `apply_turn_budget_from_architect()` (above) uses to call
    `set_max_turns()` on FE/BE/OPS once the architect's estimate exists;
    callers that don't need it can just ignore the second element.

    PM/Architect/UAT/Reporter are pure reasoning roles: same execution
    backend, but with `allowed_tools=[]` so they can't touch the filesystem.
    FE/BE/OPS/QA get real file/bash tools scoped to `workspace` (a per-run
    directory) so they actually write and verify code.

    `agent_cls` defaults to the real `ClaudeCodeAgent` and is the seam
    `tests/` uses to swap in `ScriptedClaudeCodeAgent` — a stand-in that
    returns canned text instead of running a real Claude Code session, so
    the graph/routing/checkpoint logic can be verified deterministically
    and without spending Claude Code session quota. The CLI (`main.py`)
    never passes this argument.

    `on_event`, if given, is wired into every tool-using and reasoning
    agent's `ClaudeCodeAgent._emit()` (see claude_code_agent.py) — fired on
    turn start, each tool call, and turn completion. This is what the web
    server's live "run floor" view subscribes to; the CLI leaves it unset
    and simply doesn't get per-tool-call granularity (it already prints
    each completed turn's full text once GraphFlow yields it).

    Workspace layout (also encoded in prompts.py — keep the two in sync):
      workspace/frontend/   — frontend_engineer's cwd
      workspace/backend/    — backend_engineer's cwd
      workspace/            — devops_engineer's and qa_engineer's cwd (root,
                               so OPS can reference ./frontend and ./backend,
                               and QA can read/test everything); also the
                               cwd for the tool-less reasoning agents, who
                               never use it

    `output_dir`, if given, is the CROSS-RUN parent directory (`workspace`
    is one specific run's subdirectory under it — same relationship
    `run_memory.py`'s `output_dir` has to a run's workspace). Used to point
    `qa_engineer`'s `PLAYWRIGHT_BROWSERS_PATH` at `<output_dir>/.playwright
    -browsers/` — a location that survives across every run, not just this
    one — so the Chromium binary QA's Visual QA pass needs (see
    QA_PROMPT step 4) is downloaded once and reused, instead of every run
    (or every container restart, in the Dockerized deployment) re-fetching
    it. None (the default) leaves QA's session to fall back on whatever
    the host's own Playwright cache location resolves to — the SDK's
    subprocess inherits the full host environment by default, so this
    already often works by accident on a long-lived host, but isn't
    guaranteed (an ephemeral container has no such implicit cache).
    """

    sdk_log_path = workspace / ARTIFACT_DIR_NAME / SDK_LOG_FILE_NAME
    qa_extra_env = (
        {"PLAYWRIGHT_BROWSERS_PATH": str(output_dir / ".playwright-browsers")}
        if output_dir is not None
        else {}
    )

    # Parallel-session scheduler: LOOPER_MAX_PARALLEL_SESSIONS=N bounds how
    # many real claude_agent_sdk sessions run concurrently across the whole
    # team (the FE/BE/OPS fan-out is the only phase where more than one runs
    # at once). Unset/0 = unbounded, the historical behavior. One semaphore
    # shared by every agent — see ClaudeCodeAgent.__init__ for why this
    # exists (account-wide session limits tripping mid-fan-out).
    max_parallel = int(os.environ.get("LOOPER_MAX_PARALLEL_SESSIONS", "0") or 0)
    session_limiter = asyncio.Semaphore(max_parallel) if max_parallel > 0 else None

    # Per-SESSION dollar cap (SDK-enforced; an exceeded session degrades to
    # partial output like max_turns, it doesn't crash the run — see
    # ClaudeCodeAgent). Complements the run-level LOOPER_MAX_RUN_BUDGET_USD
    # cap enforced by runner.FailFastMonitor across all sessions. Unset =
    # uncapped, the historical behavior.
    _session_budget_raw = os.environ.get("LOOPER_MAX_SESSION_BUDGET_USD", "")
    session_budget_usd = float(_session_budget_raw) if _session_budget_raw else None

    # Per-role prompt addenda (runtime-composed context, same pattern as
    # architect_addendum): today this carries run_memory's cross-run defect
    # hints into engineer prompts ("your role produced these BLOCKER/MAJOR
    # defects in earlier runs..."). Appended after the role's static prompt,
    # never edited into prompts.py/SPEC.md themselves.
    addenda = dict(role_addenda) if role_addenda is not None else {}

    def code_agent(
        name: str,
        description: str,
        prompt: str,
        cwd: Path,
        max_turns: int,
        context_sources: dict[str, str],
        allowed_tools: Sequence[str] | None = None,
        pointer_files: dict[str, Path] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> ClaudeCodeAgent:
        kwargs = {} if allowed_tools is None else {"allowed_tools": allowed_tools}
        addendum = addenda.get(name)
        if addendum:
            prompt = f"{prompt}\n\n{addendum}"
        return agent_cls(
            name=name,
            description=description,
            system_prompt=GLOBAL_RULES + "\n\n" + prompt,
            cwd=cwd,
            max_turns=max_turns,
            context_sources=context_sources,
            pointer_files=pointer_files,
            on_event=on_event,
            sdk_log_path=sdk_log_path,
            session_limiter=session_limiter,
            extra_env=extra_env,
            max_budget_usd=session_budget_usd,
            **kwargs,
        )

    # Tool-less reasoning roles never make a tool call, so they always finish
    # in a single turn — a small cap here is just a runaway guard, not a
    # working budget. Engineer/QA budgets are a real cost lever: each is a
    # full agentic session (writing files, running builds/tests), and QA
    # rework loops repeat FE/BE/OPS, so these numbers multiply fast across a
    # run. Lower them further (or set LOOPER_CODE_MODEL to a cheaper model)
    # if you're hitting the Claude Code session/usage limit.
    REASONING_MAX_TURNS = 3
    ENGINEER_MAX_TURNS = 20
    QA_MAX_TURNS = 20

    # Context routing: GraphFlow broadcasts every message to every agent, but
    # each role's replayed prompt is filtered down to the artifacts SPEC.md
    # section 3 declares as that role's inputs, keeping only the latest
    # version per source ("latest") — a superseded PRD or old QA report is
    # wasted tokens and a stale-context hazard. The original user goal
    # ("user") is kept everywhere: it's one line, and UAT/PM route on it.
    # Keep these dicts in sync with the "Input:" line of each role's prompt.
    ENGINEER_CONTEXT = {
        "user": "all",
        "solution_architect": "latest",
        "qa_engineer": "latest",
    }

    # Pointer routing (tool-using roles only): on turns where a large
    # artifact is unchanged since the agent last received it (QA rework
    # loops), replay a one-line pointer to its on-disk copy instead of the
    # full text — the agent Reads/Greps just the sections its defects
    # reference. main.py writes these files as each artifact is produced.
    # First-sight turns always get the full text inline (see
    # ClaudeCodeAgent docstring), so this never hides an unseen contract.
    docs = workspace / ARTIFACT_DIR_NAME
    DESIGN_POINTER = {"solution_architect": docs / "solution_architect.md"}
    QA_POINTERS = {
        "product_manager": docs / "product_manager.md",
        "solution_architect": docs / "solution_architect.md",
    }

    pm = code_agent(
        "product_manager",
        "Grooms the raw goal into a build-ready PRD.",
        PM_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        context_sources={"user": "all", "uat_reviewer": "latest"},
        allowed_tools=[],
    )
    # `architect_addendum` is runtime-composed prompt context (e.g. the
    # cross-run calibration hint from run_memory.calibration_hint()) — same
    # pattern as the TURN BUDGET line ClaudeCodeAgent injects per turn. It
    # deliberately does NOT edit ARCHITECT_PROMPT itself, so the
    # prompts.py/SPEC.md sync rule is untouched.
    architect_prompt = ARCHITECT_PROMPT
    if architect_addendum:
        architect_prompt = f"{ARCHITECT_PROMPT}\n\n{architect_addendum}"
    # Annotate-only scope gate between PM and architect (see
    # SCOPE_VALIDATOR_PROMPT / SPEC.md 3.1b): always forwards — enforcement
    # is downstream prompts honoring its Must-Cut list (architect designs
    # nothing for those items, QA marks them TRIMMED, UAT treats a wrong
    # trim as re-scope grounds), NOT a reject loop back to PM. Chosen over
    # a loop deliberately: no new activation groups, no loop-guard
    # termination, no deadlock surface. Exists because scope inflation at
    # the PM stage was a real, measured incident (a "beautiful car showroom
    # website" goal ballooning into a $13 full-stack platform whose padded
    # scope is also what ran the engineers out of their turn budgets).
    scope_validator = code_agent(
        "scope_validator",
        "Independent proportionality check of the PRD against the original goal.",
        SCOPE_VALIDATOR_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        context_sources={"user": "all", "product_manager": "latest"},
        allowed_tools=[],
    )
    architect = code_agent(
        "solution_architect",
        "Turns the PRD into a production-grade technical design.",
        architect_prompt,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        context_sources={"user": "all", "product_manager": "latest", "scope_validator": "latest"},
        allowed_tools=[],
    )
    fe = code_agent(
        "frontend_engineer",
        "Implements [FE] tasks from the TECH DESIGN by writing real code to the shared workspace.",
        FE_PROMPT,
        cwd=workspace / "frontend",
        max_turns=ENGINEER_MAX_TURNS,
        context_sources=ENGINEER_CONTEXT,
        pointer_files=DESIGN_POINTER,
    )
    be = code_agent(
        "backend_engineer",
        "Implements [BE] tasks from the TECH DESIGN by writing real code to the shared workspace.",
        BE_PROMPT,
        cwd=workspace / "backend",
        max_turns=ENGINEER_MAX_TURNS,
        context_sources=ENGINEER_CONTEXT,
        pointer_files=DESIGN_POINTER,
    )
    ops = code_agent(
        "devops_engineer",
        "Implements [OPS] tasks from the TECH DESIGN by writing real Docker/CI config to the shared workspace.",
        OPS_PROMPT,
        cwd=workspace,
        max_turns=ENGINEER_MAX_TURNS,
        context_sources=ENGINEER_CONTEXT,
        pointer_files=DESIGN_POINTER,
    )
    qa = code_agent(
        "qa_engineer",
        "Verifies the real implementation in the shared workspace against the PRD and TECH DESIGN.",
        QA_PROMPT,
        cwd=workspace,
        max_turns=QA_MAX_TURNS,
        context_sources={
            "user": "all",
            "product_manager": "latest",
            "scope_validator": "latest",
            "solution_architect": "latest",
            "frontend_engineer": "latest",
            "backend_engineer": "latest",
            "devops_engineer": "latest",
        },
        pointer_files=QA_POINTERS,
        # No Write/Edit: QA verifies and reports defects, it never modifies
        # the code it's independently checking — see README "Access per role".
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        # Persistent cross-run Playwright browser cache (see build_team()'s
        # docstring) — {} (no-op) when output_dir wasn't given.
        extra_env=qa_extra_env,
    )
    uat = code_agent(
        "uat_reviewer",
        "The same PM performing UAT against the original goal.",
        UAT_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        context_sources={
            "user": "all",
            "product_manager": "latest",
            "scope_validator": "latest",
            "frontend_engineer": "latest",
            "backend_engineer": "latest",
            "devops_engineer": "latest",
            "qa_engineer": "latest",
        },
        allowed_tools=[],
    )
    reporter = code_agent(
        "release_reporter",
        "Summarizes the approved run into a final delivery report.",
        REPORTER_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        context_sources={
            "user": "all",
            "product_manager": "latest",
            "solution_architect": "latest",
            "frontend_engineer": "latest",
            "backend_engineer": "latest",
            "devops_engineer": "latest",
            "qa_engineer": "latest",
            "uat_reviewer": "latest",
        },
        allowed_tools=[],
    )

    builder = DiGraphBuilder()
    for a in (pm, scope_validator, architect, fe, be, ops, qa, uat, reporter):
        builder.add_node(a)

    # PM → scope gate → architect, both unconditional: the validator always
    # forwards (annotate-only — see its construction above). On a UAT
    # re-scope loop the re-groomed PRD passes back through the same gate.
    builder.add_edge(pm, scope_validator)
    builder.add_edge(scope_validator, architect)

    # QA's outgoing edges route through a stateful QaVerdictRouter (see its
    # docstring): the first MAX_QA_LOOPS-1 QA_FAILs go to the engineers for
    # rework; the final one goes to UAT for a shippability judgment instead
    # of hard-killing the run.
    qa_router = QaVerdictRouter(MAX_QA_LOOPS)

    # Fan-out to engineers. Each engineer has two ways to activate — the
    # architect's design (first pass) or a QA_FAIL rework loop — so both
    # edges share one activation group with "any" semantics; the default
    # ("all") would deadlock the first pass waiting on QA.
    for eng in (fe, be, ops):
        group = f"{eng.name}_activation"
        builder.add_edge(
            architect, eng, activation_group=group, activation_condition="any"
        )
        builder.add_edge(
            qa,
            eng,
            condition=qa_router.rework,
            activation_group=group,
            activation_condition="any",
        )

    # Fan-in to QA: default "all" activation — QA waits for all three engineers.
    builder.add_edge(fe, qa)
    builder.add_edge(be, qa)
    builder.add_edge(ops, qa)

    # QA_PASS, or the final QA_FAIL after the rework budget is exhausted.
    builder.add_edge(qa, uat, condition=qa_router.to_uat)

    # UAT rejection loops back to grooming. PM is also the entry point, so
    # this edge needs "any" activation for the same reason as the engineers.
    builder.add_edge(
        uat,
        pm,
        condition=verdict_is("UAT_REJECTED"),
        activation_group="pm_activation",
        activation_condition="any",
    )

    # Approval flows to the terminal reporter — the graph's required leaf
    # node. The run completes naturally there (GraphFlow stops at leaves),
    # so no UAT_APPROVED termination condition is needed.
    builder.add_edge(uat, reporter, condition=verdict_is("UAT_APPROVED"))

    builder.set_entry_point(pm)
    graph = builder.build()

    termination = (
        # QA_FAIL no longer hard-kills the run at MAX_QA_LOOPS — the
        # QaVerdictRouter routes the final fail to UAT for a shippability
        # judgment instead. This raised count is purely a runaway guard:
        # it can only fire if that routing breaks (or a resumed run's
        # reset router allows extra loops) and QA keeps failing anyway.
        # 2nd UAT_REJECTED ⇒ re-scope happened once already, stop.
        TokenCountTermination("QA_FAIL", source=qa.name, max_count=MAX_QA_LOOPS + 2)
        | TokenCountTermination("UAT_REJECTED", source=uat.name, max_count=2)
        | MaxMessageTermination(MAX_MESSAGES)
    )

    team = GraphFlow(
        participants=builder.get_participants(),
        graph=graph,
        termination_condition=termination,
    )
    agents = {
        "product_manager": pm,
        "scope_validator": scope_validator,
        "solution_architect": architect,
        "frontend_engineer": fe,
        "backend_engineer": be,
        "devops_engineer": ops,
        "qa_engineer": qa,
        "uat_reviewer": uat,
        "release_reporter": reporter,
    }
    return team, agents
