"""AutoGen chat agent backed by a real claude_agent_sdk session.

Unlike AssistantAgent (which only produces text), this agent executes its
role against a real filesystem workspace via the Claude Code agent loop —
it reads and writes files and runs shell commands in `cwd`, rather than
inlining code as chat text. See SPEC.md section 3 for the role prompts and
the workspace-directory convention this pairs with in pipeline.py.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from autogen_agentchat.agents import BaseChatAgent
from autogen_agentchat.base import Response
from autogen_agentchat.messages import BaseChatMessage, TextMessage
from autogen_core import CancellationToken
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .termination import last_line

DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

# Ceiling for the auto-bump applied when an engineer hit max_turns on its
# previous real session (see _last_hit_max_turns below). Mirrors
# pipeline.MAX_ENGINEER_TURNS — duplicated rather than imported to avoid a
# circular import (pipeline.py imports this module, not the other way
# around). Keep the two values in sync if either changes.
_MAX_TURN_BUDGET = 50

# QA/UAT MUST end their message in one of these tokens for GraphFlow's
# conditional edges (verdict_is() in pipeline.py) to route anywhere — no
# match means no outgoing edge fires and that branch of the graph stalls.
# A turn that hits error_max_turns (or, in principle, any other reason the
# model doesn't produce a clean final line) is force-completed with the
# conservative fallback: never silently PASS/APPROVE work that was cut off
# before it was actually verified.
_VALID_VERDICTS = {
    "qa_engineer": {"QA_PASS", "QA_FAIL"},
    "uat_reviewer": {"UAT_APPROVED", "UAT_REJECTED"},
}
_FALLBACK_VERDICT = {"qa_engineer": "QA_FAIL", "uat_reviewer": "UAT_REJECTED"}

# Sonnet, not Claude Code's own default (typically Opus-tier), for every
# agent unless LOOPER_CODE_MODEL overrides it. This pipeline runs many real
# agentic sessions per goal (8 base turns, more with QA rework loops), all
# drawing the same Claude Code session/usage quota as interactive use —
# Sonnet is materially cheaper/faster per session with no code change
# required elsewhere. "sonnet" (not a dated model string) is a CLI alias
# that always resolves to the latest Sonnet model — confirmed via
# `claude --help`: "Provide an alias for the latest model (e.g. 'sonnet' or
# 'opus') or a model's full name."
DEFAULT_MODEL = "sonnet"

# (source_name, event_type, detail, extra) -> None. Fired at turn start,
# per tool call, and turn end. Optional and best-effort: main.py's CLI path
# never sets this (nothing to consume the events), a web/UI runner does.
OnEvent = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]


def _summarize_tool_call(name: str, tool_input: dict[str, Any]) -> str:
    """One-line human-readable summary of a tool call for a live activity
    feed — e.g. "docker build ./backend", not the raw JSON input (which for
    Write/Edit would be the entire file being written)."""
    if name == "Bash":
        return str(tool_input.get("command", ""))[:200]
    if name in ("Write", "Edit", "Read"):
        return str(tool_input.get("file_path", ""))
    if name in ("Glob", "Grep"):
        return str(tool_input.get("pattern", ""))
    return str(tool_input)[:200]

# Best-effort content policy applied to every tool-using role, regardless of
# which specific tools that role has. This is NOT a sandbox — it's a
# string-match denylist on the literal Bash command, so it can be evaded by
# a sufficiently adversarial command. Real filesystem/network confinement
# is claude_agent_sdk's `SandboxSettings` (not wired in here yet); this is
# the cheap, always-on layer that blocks the categories of command no role
# in this pipeline ever legitimately needs.
_BASH_DENY_PATTERNS = [
    r"\brm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\b",  # rm -rf / rm -fr (any flag order)
    r"\bsudo\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bgit\s+push\b",
    r"\bchmod\s+-R\s+777\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
]


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _make_hooks(cwd: Path) -> dict[str, list[HookMatcher]]:
    """Build PreToolUse hooks scoped to one agent's workspace dir.

    Deliberately hooks, not `can_use_tool`: an `allowed_tools` entry that
    allows a whole tool (e.g. plain `"Bash"`) auto-approves it before
    `can_use_tool` is ever consulted — confirmed via
    `CanUseToolShadowedWarning` when this was first wired up with
    `can_use_tool` and a real `curl` call went straight through. Hooks fire
    regardless of `allowed_tools` shadowing.

    Two checks, applied before any tool actually runs:
    - `Write`/`Edit` targeting a path that resolves outside `cwd` are denied
      (precise path resolution, not a string match — catches `..` traversal
      and absolute paths alike).
    - `Bash` commands matching `_BASH_DENY_PATTERNS` are denied.
    Everything else is allowed — this is a denylist, not an allowlist, so it
    doesn't need to anticipate every legitimate command a role might run.
    """
    resolved_cwd = cwd.resolve()

    async def guard_write_edit(input_data, tool_use_id, context) -> dict[str, Any]:
        raw_path = input_data.get("tool_input", {}).get("file_path")
        if raw_path:
            target = Path(raw_path)
            if not target.is_absolute():
                target = resolved_cwd / target
            if not target.resolve().is_relative_to(resolved_cwd):
                return _deny(f"Refusing to write outside the workspace ({resolved_cwd}): {raw_path}")
        return {}

    async def guard_bash(input_data, tool_use_id, context) -> dict[str, Any]:
        command = input_data.get("tool_input", {}).get("command", "")
        for pattern in _BASH_DENY_PATTERNS:
            if re.search(pattern, command):
                return _deny(f"Command blocked by policy (matches {pattern!r}): {command}")
        return {}

    return {
        "PreToolUse": [
            HookMatcher(matcher="Write|Edit", hooks=[guard_write_edit]),
            HookMatcher(matcher="Bash", hooks=[guard_bash]),
        ]
    }


class ClaudeCodeAgent(BaseChatAgent):
    """Runs one role via a real Claude Code agent session scoped to `cwd`.

    Each `query()` call starts a fresh Claude Code session with no memory
    of prior turns — the workspace filesystem carries state across a run
    (rework loops re-read the files this agent wrote earlier), but the
    upstream chat context (PRD, TECH DESIGN, defect reports) doesn't exist
    on disk, so it's replayed as the prompt on every turn, mirroring how
    AssistantAgent's internal model_context accumulates across calls.

    `context_sources` bounds what gets replayed. GraphFlow broadcasts every
    agent's message to every other agent, so an unfiltered replay grows
    with the whole transcript — each turn re-sends artifacts the role's own
    prompt says it doesn't consume (e.g. FE re-reading BE's summaries), and
    rework loops re-send superseded PRDs/QA reports alongside their
    replacements. The filter maps source name -> "all" | "latest"; sources
    not listed are dropped, and "latest" keeps only that source's most
    recent message. `None` (the default) replays everything unfiltered.
    `_history` itself always keeps every message — filtering happens at
    prompt-build time, so checkpoints stay complete and the policy can
    change between runs without losing data.

    `pointer_files` cuts replay further for tool-using roles: a source
    listed there renders as a one-line pointer to its on-disk copy
    (written by main.py under the workspace's artifact dir) instead of
    full text — but ONLY when that source's message did NOT arrive this
    turn and the file actually exists. The effectiveness guardrail is the
    "did not arrive this turn" condition: an artifact is always inlined in
    full the first time an agent sees it (first pass, or a redesign after
    a UAT re-scope), so no agent ever has to *retrieve* a contract it was
    never shown — pointers only replace text the pipeline already
    delivered whole in an earlier turn and that is sitting unchanged on
    disk, where Read/Grep can pull back just the needed sections. Never
    set this for a tool-less role: with no Read tool a pointer is a dead
    end, which is why it's a separate opt-in rather than a context_sources
    policy.
    """

    def __init__(
        self,
        name: str,
        description: str,
        system_prompt: str,
        cwd: Path,
        max_turns: int = 20,
        allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
        context_sources: Mapping[str, str] | None = None,
        pointer_files: Mapping[str, Path] | None = None,
        on_event: OnEvent | None = None,
        sdk_log_path: Path | None = None,
        session_limiter: "asyncio.Semaphore | None" = None,
        extra_env: Mapping[str, str] | None = None,
        max_budget_usd: float | None = None,
    ) -> None:
        super().__init__(name, description=description)
        self._system_prompt = system_prompt
        self._cwd = cwd
        self._max_turns = max_turns
        self._allowed_tools = list(allowed_tools)
        self._history: list[BaseChatMessage] = []
        # Every prompt sent to and response received from a real
        # claude_agent_sdk session, appended as one JSON line per turn. None
        # (tests' ScriptedClaudeCodeAgent, which never calls the real SDK)
        # means no log is written — see _log_interaction()'s docstring.
        self._sdk_log_path = sdk_log_path
        # True from the moment on_messages() accepts a turn until it returns
        # successfully. GraphFlow's own checkpoint has no concept of
        # "dispatched but not completed" — select_speaker() clears a node's
        # "needs to run" bookkeeping the instant it's picked, before the
        # agent actually executes (see pipeline.py's recover_stuck_agents()
        # docstring for the full story). This flag is how a resume detects
        # that gap and repairs it.
        self._turn_in_progress = False
        # True when this agent's most recent real session ended by hitting
        # max_turns (see on_messages()'s error_max_turns handling below).
        # Consulted — and cleared — at the start of the next on_messages()
        # call so a rework turn doesn't get handed the exact same budget
        # that already proved insufficient. Not part of save_state() (see
        # set_max_turns()'s docstring for why dynamic budget state is
        # deliberately in-memory-only) — a --resume simply won't carry this
        # forward, same accepted tradeoff as the architect-derived budget.
        self._last_hit_max_turns = False
        # Optional scheduler for the parallel engineer fan-out: a semaphore
        # SHARED by every agent in the team (created once in build_team()
        # from LOOPER_MAX_PARALLEL_SESSIONS), bounding how many real
        # claude_agent_sdk sessions run concurrently across the whole run.
        # None (the default, and the CLI/web default when the env var is
        # unset) means unbounded — FE/BE/OPS all run at once, as before.
        # Bounding matters when an account-wide Claude Code session/usage
        # limit is the scarce resource: three concurrent engineer sessions
        # can trip it mid-fan-out, which is precisely the crash the
        # checkpoint-resume machinery exists to recover from — a limiter
        # avoids needing that recovery in the first place, trading wall
        # -clock time for it. Skipped (zero-budget) roles never acquire a
        # slot; GraphFlow's own dispatch order is untouched — waiting agents
        # simply queue on the semaphore inside their turn.
        self._session_limiter = session_limiter
        # Extra env vars merged into this agent's real SDK session (on top
        # of the full host environment claude_agent_sdk already inherits by
        # default — confirmed by reading subprocess_cli.py's `connect()`:
        # `{**os.environ, ...self._options.env}`). Currently used for one
        # thing: QA's PLAYWRIGHT_BROWSERS_PATH (see build_team()), so the
        # Chromium binary Playwright needs for visual verification is
        # installed once into a location that persists across every run —
        # not just implicitly relying on the host's own OS-level cache
        # directory, which is fragile precisely where it matters most (a
        # container restart in the Dockerized deployment has no such
        # cache unless it's inside the already-mounted output/ volume).
        self._extra_env = dict(extra_env) if extra_env is not None else {}
        # Optional hard dollar ceiling for each real SDK session, passed
        # straight through to ClaudeAgentOptions.max_budget_usd (the SDK
        # stops the query with an `error_max_budget_usd` result when
        # exceeded — handled below like error_max_turns: partial output +
        # warning banner, never a run-killing crash). None = uncapped, the
        # historical behavior. Set via LOOPER_MAX_SESSION_BUDGET_USD in
        # build_team(); complements the RUN-level ceiling enforced by
        # runner.FailFastMonitor, which sums completed turns across agents.
        self._max_budget_usd = max_budget_usd
        # Model routing: LOOPER_CODE_MODEL_<AGENT_NAME> (uppercased, e.g.
        # LOOPER_CODE_MODEL_RELEASE_REPORTER=haiku) overrides the global
        # LOOPER_CODE_MODEL for that one role. Defaults stay uniform on
        # purpose: the reasoning turns are only ~7% of a real run's cost
        # (measured), and the roles cheapest to downgrade (PM/architect)
        # are the ones whose quality steers everything downstream —
        # per-role downgrades are an operator experiment, not a default.
        self._model = os.environ.get(
            f"LOOPER_CODE_MODEL_{name.upper()}",
            os.environ.get("LOOPER_CODE_MODEL", DEFAULT_MODEL),
        )
        self._hooks = _make_hooks(cwd)
        self._context_sources = dict(context_sources) if context_sources is not None else None
        self._pointer_files = dict(pointer_files) if pointer_files is not None else None
        self._on_event = on_event

    def set_max_turns(self, max_turns: int) -> None:
        """Override the turn budget set at construction time — the seam
        `pipeline.py`'s dynamic turn-budget mechanism uses to raise or
        lower an engineer's ceiling based on the architect's own per-role
        estimate in the TECH DESIGN, once that estimate exists (it can't be
        known at `build_team()` time, before the architect has run). Public
        on purpose, unlike `_max_turns` itself — reaching into a "private"
        attribute from outside the class would work today but isn't an
        interface anyone should build on.
        """
        self._max_turns = max_turns

    @property
    def max_turns(self) -> int:
        """Current turn budget — the read counterpart to `set_max_turns()`,
        for callers that need to compute a NEW budget relative to whatever
        this agent's is right now (e.g. `pipeline.py`'s
        `apply_visual_qa_budget()`, which adds an extra amount on top of
        QA's current baseline) without reaching into `_max_turns` directly.
        """
        return self._max_turns

    def _log_interaction(
        self,
        *,
        prompt: str,
        system_prompt: str,
        response: str | None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        skipped: bool = False,
        num_turns: int | None = None,
    ) -> None:
        """Append one JSON line recording this turn's real SDK exchange —
        exactly what was sent as the prompt/system prompt and exactly what
        came back (or the error, if the session never produced a response).
        Best-effort: a logging failure (disk full, permissions) must never
        take down an actual pipeline turn, so write errors are swallowed
        the same way `_emit()` swallows on_event failures. No-op if no
        `sdk_log_path` was configured (e.g. tests' ScriptedClaudeCodeAgent,
        which never reaches this — it overrides on_messages() wholesale and
        never calls the real SDK, so there's nothing to log).
        """
        if self._sdk_log_path is None:
            return
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": self.name,
            "model": self._model,
            "cwd": str(self._cwd),
            "max_turns": self._max_turns,
            "system_prompt": system_prompt,
            "prompt": prompt,
            "response": response,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            # Actual tool-call turns the session used (ResultMessage.num_turns),
            # vs. "max_turns" above (the budget) — the pair is what
            # looper.report's budget-calibration table is computed from.
            "num_turns": num_turns,
            "error": error,
            "skipped": skipped,
        }
        try:
            self._sdk_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._sdk_log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    async def _emit(self, event_type: str, detail: str = "", **extra: Any) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(self.name, event_type, detail, extra)
        except Exception:
            pass  # a live-status sink must never break an actual pipeline turn

    def _build_prompt(self, new_sources: set[str] | None = None) -> str:
        """Render the (filtered) history as the session prompt.

        `new_sources` is the set of sources whose messages arrived THIS
        turn. When None (e.g. direct calls in tests), pointer substitution
        is disabled entirely and everything renders inline — the safe
        default, since a pointer without delta information could hide an
        artifact the agent has never seen.
        """
        if self._context_sources is None:
            visible = list(self._history)
        else:
            last_index = {msg.source: i for i, msg in enumerate(self._history)}
            visible = [
                msg
                for i, msg in enumerate(self._history)
                if self._context_sources.get(msg.source) == "all"
                or (self._context_sources.get(msg.source) == "latest" and last_index[msg.source] == i)
            ]

        parts = []
        for msg in visible:
            pointer = (
                self._pointer_files.get(msg.source)
                if self._pointer_files is not None and new_sources is not None
                else None
            )
            if pointer is not None and msg.source not in new_sources and pointer.is_file():
                parts.append(
                    f"### {msg.source}\n"
                    f"[Unchanged since an earlier turn — omitted here to save context. "
                    f"Full text is on disk at: {pointer}\n"
                    f"Read or grep just the sections you need "
                    f"(e.g. the contracts/tasks your current defects reference).]"
                )
            else:
                parts.append(f"### {msg.source}\n{msg.to_text()}")
        return "\n\n".join(parts)

    @property
    def produced_message_types(self) -> Sequence[type[BaseChatMessage]]:
        return (TextMessage,)

    async def on_reset(self, cancellation_token: CancellationToken) -> None:
        self._history.clear()

    async def save_state(self) -> Mapping[str, Any]:
        # BaseChatAgent's default save_state() is a no-op ("stateless
        # agent") — override it so `self._history` (the accumulated
        # upstream context this agent replays into every fresh query()
        # call) survives a GraphFlow checkpoint/resume round-trip.
        return {
            "history": [msg.dump() for msg in self._history],
            "turn_in_progress": self._turn_in_progress,
        }

    async def load_state(self, state: Mapping[str, Any]) -> None:
        self._history = [TextMessage.load(data) for data in state.get("history", [])]
        self._turn_in_progress = state.get("turn_in_progress", False)

    async def on_messages(
        self, messages: Sequence[BaseChatMessage], cancellation_token: CancellationToken
    ) -> Response:
        self._history.extend(messages)
        self._turn_in_progress = True
        await self._emit("turn_started")

        if self._allowed_tools and self._max_turns == 0:
            # ARCHITECT_PROMPT instructs the architect to set a role's turn
            # budget to exactly 0 when its own Task Breakdown assigns it
            # zero tasks (see set_max_turns()/pipeline.py's
            # apply_turn_budget_from_architect() and parse_turn_budget()'s
            # 0-is-not-clamped exemption). Treat that as "nothing to do"
            # and skip the real Claude Code session entirely instead of
            # spending a full paid agentic turn just to confirm there's no
            # work — the fix for a real, observed failure mode where the
            # architect always invented busywork (backend validation
            # services, Terraform/CI) for every engineer on every goal,
            # even a single static page, because nothing let it leave a
            # role genuinely idle.
            final_text = (
                f"# {self.name.upper()} IMPLEMENTATION\n\n"
                "No tasks were assigned to this role in the TECH DESIGN "
                "(turn budget: 0) — skipped without starting a Claude Code "
                "session.\n\n"
                "## Files Written\n(none)\n\n"
                "## Tasks Completed\n(none)\n\n"
                "## Assumptions\n"
                "The architect's Task Breakdown assigned zero tasks to this "
                "role for this goal.\n"
            )
            self._turn_in_progress = False
            await self._emit("turn_completed", final_text, cost_usd=0.0, duration_ms=0)
            self._log_interaction(
                prompt="(skipped — no tasks assigned, no session started)",
                system_prompt=self._system_prompt,
                response=final_text,
                cost_usd=0.0,
                duration_ms=0,
                skipped=True,
            )
            return Response(chat_message=TextMessage(content=final_text, source=self.name))

        if self._last_hit_max_turns:
            # This agent's previous real session ran out of turns before
            # finishing (see the error_max_turns handling below) — most
            # often the session right before a QA-rework turn, which is
            # guaranteed to happen next for whichever engineer's defects
            # caused the QA_FAIL. Handing it the identical budget that
            # already proved too small (now with MORE work to do: fix the
            # defects AND finish whatever the truncated first pass never
            # got to) just reproduces the same failure. Bump by +50% (floor
            # +8, so a small budget isn't bumped by a token amount), clamped
            # to _MAX_TURN_BUDGET. One-shot: cleared immediately so a turn
            # that finishes cleanly doesn't keep inflating the budget.
            self._max_turns = min(_MAX_TURN_BUDGET, self._max_turns + max(8, self._max_turns // 2))
            self._last_hit_max_turns = False
            await self._emit(
                "warning", f"{self.name} previously hit max_turns; raising budget to {self._max_turns}"
            )

        prompt = self._build_prompt(new_sources={msg.source for msg in messages})

        self._cwd.mkdir(parents=True, exist_ok=True)
        system_prompt = self._system_prompt
        if self._allowed_tools:
            # Tool-less reasoning roles always finish in one turn regardless
            # of max_turns (it's just a runaway guard for them — see
            # pipeline.py), so telling them a turn count would be noise, not
            # information. For tool-using roles, always state the actual
            # current budget — whether it's the static default or a
            # dynamic estimate set via set_max_turns() — so the agent can
            # pace itself instead of discovering the ceiling by hitting it.
            system_prompt = (
                f"{system_prompt}\n\n"
                f"TURN BUDGET: you have approximately {self._max_turns} tool-call turns "
                f"to complete this task. Prioritize the MUST-have functionality first; "
                f"if you're running low, finish the most critical pieces completely "
                f"rather than leaving many things partially done."
            )
        options = ClaudeAgentOptions(
            cwd=str(self._cwd),
            system_prompt=system_prompt,
            # `tools` is what actually restricts which tools exist for this
            # session (verified live: `allowed_tools` alone does NOT — it
            # only skips the permission prompt for tools already available,
            # so a session with `allowed_tools=["Read"]` and no `tools`
            # restriction could still run Bash). Set both: `tools` gates
            # availability, `allowed_tools` keeps the same set auto-approved
            # so acceptEdits doesn't prompt for them.
            tools=self._allowed_tools,
            allowed_tools=self._allowed_tools,
            permission_mode="acceptEdits",
            max_turns=self._max_turns,
            model=self._model,
            hooks=self._hooks,
            env=self._extra_env,
            max_budget_usd=self._max_budget_usd,
        )

        transcript: list[str] = []
        result_text = ""
        cost_usd: float | None = None
        duration_ms: int | None = None
        num_turns: int | None = None
        hit_max_turns = False
        hit_budget_cap = False
        if self._session_limiter is not None:
            # Team-wide cap on concurrent real SDK sessions (see __init__).
            # Acquired only for the session itself — the skip path above and
            # all prompt/bookkeeping work stay outside the critical section.
            await self._session_limiter.acquire()
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            transcript.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            await self._emit(
                                "tool_call",
                                _summarize_tool_call(block.name, block.input),
                                tool_name=block.name,
                            )
                elif isinstance(msg, ResultMessage):
                    if msg.is_error and msg.subtype not in ("error_max_turns", "error_max_budget_usd"):
                        # A genuine crash (auth/billing/session-limit/etc.) —
                        # no usable output exists, so there's nothing to hand
                        # QA; fail the whole run so --resume can retry this
                        # turn.
                        await self._emit("error", msg.result or str(msg.errors))
                        error_text = f"({msg.subtype}): {msg.result or msg.errors}"
                        self._log_interaction(
                            prompt=prompt,
                            system_prompt=system_prompt,
                            response=None,
                            cost_usd=msg.total_cost_usd,
                            duration_ms=msg.duration_ms,
                            error=error_text,
                        )
                        raise RuntimeError(f"{self.name}: Claude Code session failed {error_text}")
                    if msg.is_error:
                        # error_max_turns: the role ran out of its turn
                        # budget before finishing, not a crash. Real
                        # production case — a moderately complex full-stack
                        # goal ran FE/BE/OPS out of a 20-turn budget and took
                        # the whole pipeline down with it, even though most
                        # of the work was already on disk. Treat this as
                        # best-effort partial output instead: `transcript`
                        # (below) becomes final_text, the run continues to
                        # QA, and QA — reading the real, genuinely incomplete
                        # files — can fail it with a concrete defect instead
                        # of the pipeline dying with nothing to resume into
                        # but the exact same turn budget.
                        #
                        # error_max_budget_usd (the per-session dollar cap,
                        # see __init__'s max_budget_usd) gets the identical
                        # treatment for the identical reason — the work
                        # already on disk is real; only the conversation was
                        # cut off. The one difference: it does NOT set
                        # _last_hit_max_turns, since bumping the TURN budget
                        # can't help a session whose constraint is dollars.
                        if msg.subtype == "error_max_turns":
                            hit_max_turns = True
                            self._last_hit_max_turns = True
                            await self._emit(
                                "warning",
                                f"{self.name} hit max_turns ({self._max_turns}) before finishing",
                            )
                        else:
                            hit_budget_cap = True
                            await self._emit(
                                "warning",
                                f"{self.name} hit its session budget cap "
                                f"(${self._max_budget_usd}) before finishing",
                            )
                    else:
                        result_text = msg.result or ""
                    cost_usd = msg.total_cost_usd
                    duration_ms = msg.duration_ms
                    num_turns = msg.num_turns
        except RuntimeError:
            raise  # our own deliberate raise above for a genuine crash — propagate as-is
        except Exception as exc:
            # The CLI subprocess exits non-zero right after emitting an
            # error_max_turns ResultMessage ("for shell-script consumers",
            # per the SDK's own internal comment) — confirmed live: the loop
            # above already received and handled that ResultMessage on a
            # prior iteration, then this exception fires on the *next*
            # iteration of the SAME async-for, from the generator's own
            # protocol, bypassing the `elif` branches entirely. If we already
            # saw error_max_turns, this second exception carries no new
            # information (same "reached maximum number of turns" text) —
            # swallow it and fall through to the partial-output path below.
            # error_max_budget_usd is gated the same way on the assumption
            # the CLI exits non-zero symmetrically for it (same "for
            # shell-script consumers" mechanism); NOT yet live-verified for
            # the budget case — if a budget-capped session ever crashes the
            # run here anyway, that assumption is what to re-check.
            # Anything else really is an unhandled crash.
            if not (hit_max_turns or hit_budget_cap):
                await self._emit("error", str(exc))
                self._log_interaction(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    response=None,
                    cost_usd=cost_usd,
                    duration_ms=duration_ms,
                    error=str(exc),
                )
                raise RuntimeError(f"{self.name}: Claude Code session failed: {exc}") from exc
        finally:
            if self._session_limiter is not None:
                self._session_limiter.release()

        final_text = result_text or "\n".join(transcript) or "(no output produced)"
        if hit_max_turns or hit_budget_cap:
            # A truncated turn's transcript is progress *narration*, not a
            # completion report — observed live: a BE turn cut off at its
            # limit left behind "Now tests: ... covering happy path,
            # validation, idempotency" describing test files it never got
            # to write. This text becomes the role's persisted artifact
            # (.pipeline-docs/<role>.md) and is replayed to QA/downstream
            # agents, so without this banner it actively claims completion
            # of missing work. QA caught that case by checking disk, but
            # only because it happened to; make the unreliability explicit
            # instead of relying on downstream skepticism.
            cutoff = (
                f"its turn limit ({self._max_turns} turns)"
                if hit_max_turns
                else f"its session budget cap (${self._max_budget_usd})"
            )
            final_text = (
                f"WARNING: this turn was cut off at {cutoff} "
                f"before the role finished. The text below "
                f"is the incomplete session's running narration, NOT a completion "
                f"report — it may describe work that was planned but never done. "
                f"Verify claims against the actual files on disk.\n\n{final_text}"
            )
        final_text = self._apply_verdict_safety_net(final_text)

        self._turn_in_progress = False
        await self._emit("turn_completed", final_text, cost_usd=cost_usd, duration_ms=duration_ms)
        self._log_interaction(
            prompt=prompt,
            system_prompt=system_prompt,
            response=final_text,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            num_turns=num_turns,
            error=(
                "hit max_turns before finishing"
                if hit_max_turns
                else "hit max_budget_usd before finishing"
                if hit_budget_cap
                else None
            ),
        )
        return Response(chat_message=TextMessage(content=final_text, source=self.name))

    def _apply_verdict_safety_net(self, final_text: str) -> str:
        """QA/UAT specifically must end in a verdict token — see the
        module-level comment on `_VALID_VERDICTS`. Pulled out as its own
        method (pure text logic, no SDK dependency) so it's directly unit
        -testable; covers max_turns truncation and, as a general safety
        net, any other case where the model didn't land on a clean final
        line. No-op for every other role.
        """
        fallback_verdict = _FALLBACK_VERDICT.get(self.name)
        if fallback_verdict is not None and last_line(final_text) not in _VALID_VERDICTS[self.name]:
            return (
                f"{final_text}\n\n"
                f"[INCOMPLETE: this turn ended without reaching a verdict — "
                f"forcing the conservative default rather than leaving the "
                f"pipeline with no route to follow.]\n{fallback_verdict}"
            )
        return final_text
