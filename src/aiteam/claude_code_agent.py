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
from typing import Any, Mapping, Sequence

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
    query,
)

DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

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
    """

    def __init__(
        self,
        name: str,
        description: str,
        system_prompt: str,
        cwd: Path,
        max_turns: int = 20,
        allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    ) -> None:
        super().__init__(name, description=description)
        self._system_prompt = system_prompt
        self._cwd = cwd
        self._max_turns = max_turns
        self._allowed_tools = list(allowed_tools)
        self._history: list[BaseChatMessage] = []
        self._model = os.environ.get("AITEAM_CODE_MODEL")
        self._hooks = _make_hooks(cwd)

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
        prompt = "\n\n".join(f"### {msg.source}\n{msg.to_text()}" for msg in self._history)

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
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    raise RuntimeError(
                        f"{self.name}: Claude Code session failed "
                        f"({msg.subtype}): {msg.result or msg.errors}"
                    )
                result_text = msg.result or ""

        final_text = result_text or "\n".join(transcript) or "(no output produced)"
        return Response(chat_message=TextMessage(content=final_text, source=self.name))
