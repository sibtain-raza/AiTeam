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

DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

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
        self._model = os.environ.get("AITEAM_CODE_MODEL")
        self._hooks = _make_hooks(cwd)
        self._context_sources = dict(context_sources) if context_sources is not None else None
        self._pointer_files = dict(pointer_files) if pointer_files is not None else None
        self._on_event = on_event

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
        return {"history": [msg.dump() for msg in self._history]}

    async def load_state(self, state: Mapping[str, Any]) -> None:
        self._history = [TextMessage.load(data) for data in state.get("history", [])]

    async def on_messages(
        self, messages: Sequence[BaseChatMessage], cancellation_token: CancellationToken
    ) -> Response:
        self._history.extend(messages)
        prompt = self._build_prompt(new_sources={msg.source for msg in messages})
        await self._emit("turn_started")

        self._cwd.mkdir(parents=True, exist_ok=True)
        options = ClaudeAgentOptions(
            cwd=str(self._cwd),
            system_prompt=self._system_prompt,
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
                if msg.is_error:
                    await self._emit("error", msg.result or str(msg.errors))
                    raise RuntimeError(
                        f"{self.name}: Claude Code session failed "
                        f"({msg.subtype}): {msg.result or msg.errors}"
                    )
                result_text = msg.result or ""
                cost_usd = msg.total_cost_usd
                duration_ms = msg.duration_ms

        final_text = result_text or "\n".join(transcript) or "(no output produced)"
        await self._emit("turn_completed", final_text, cost_usd=cost_usd, duration_ms=duration_ms)
        return Response(chat_message=TextMessage(content=final_text, source=self.name))
