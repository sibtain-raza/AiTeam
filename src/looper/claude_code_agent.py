"""AutoGen chat agent backed by a real claude_agent_sdk session.

Unlike AssistantAgent (which only produces text), this agent executes its
role against a real filesystem workspace via the Claude Code agent loop —
it reads and writes files and runs shell commands in `cwd`, rather than
inlining code as chat text. See SPEC.md section 3 for the role prompts and
the workspace-directory convention this pairs with in pipeline.py.
"""

import os
import re
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
    ) -> None:
        super().__init__(name, description=description)
        self._system_prompt = system_prompt
        self._cwd = cwd
        self._max_turns = max_turns
        self._allowed_tools = list(allowed_tools)
        self._history: list[BaseChatMessage] = []
        # True from the moment on_messages() accepts a turn until it returns
        # successfully. GraphFlow's own checkpoint has no concept of
        # "dispatched but not completed" — select_speaker() clears a node's
        # "needs to run" bookkeeping the instant it's picked, before the
        # agent actually executes (see pipeline.py's recover_stuck_agents()
        # docstring for the full story). This flag is how a resume detects
        # that gap and repairs it.
        self._turn_in_progress = False
        self._model = os.environ.get("LOOPER_CODE_MODEL", DEFAULT_MODEL)
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

    def find_message_from(self, source: str) -> BaseChatMessage | None:
        """Most recent message from `source` in this agent's replayed
        history, or None. `set_max_turns()` overrides aren't captured by
        `save_state()`/`load_state()` (only `_history` is), so a freshly
        -rebuilt agent on `--resume` reverts to the static default unless
        the caller re-derives the dynamic budget itself — the architect's
        own TECH DESIGN message is still in an engineer's history (every
        engineer's `context_sources` includes `solution_architect`), so
        re-parsing it from here reconstructs the same budget
        deterministically. See `pipeline.py`'s resume handling in `main.py`.
        """
        for msg in reversed(self._history):
            if msg.source == source:
                return msg
        return None

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
            return Response(chat_message=TextMessage(content=final_text, source=self.name))

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
        )

        transcript: list[str] = []
        result_text = ""
        cost_usd: float | None = None
        duration_ms: int | None = None
        hit_max_turns = False
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
                    if msg.is_error and msg.subtype != "error_max_turns":
                        # A genuine crash (auth/billing/session-limit/etc.) —
                        # no usable output exists, so there's nothing to hand
                        # QA; fail the whole run so --resume can retry this
                        # turn.
                        await self._emit("error", msg.result or str(msg.errors))
                        raise RuntimeError(
                            f"{self.name}: Claude Code session failed "
                            f"({msg.subtype}): {msg.result or msg.errors}"
                        )
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
                        hit_max_turns = True
                        await self._emit(
                            "warning", f"{self.name} hit max_turns ({self._max_turns}) before finishing"
                        )
                    else:
                        result_text = msg.result or ""
                    cost_usd = msg.total_cost_usd
                    duration_ms = msg.duration_ms
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
            # Anything else really is an unhandled crash.
            if not hit_max_turns:
                await self._emit("error", str(exc))
                raise RuntimeError(f"{self.name}: Claude Code session failed: {exc}") from exc

        final_text = result_text or "\n".join(transcript) or "(no output produced)"
        final_text = self._apply_verdict_safety_net(final_text)

        self._turn_in_progress = False
        await self._emit("turn_completed", final_text, cost_usd=cost_usd, duration_ms=duration_ms)
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
