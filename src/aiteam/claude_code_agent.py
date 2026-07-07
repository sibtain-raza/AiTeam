"""AutoGen chat agent backed by a real claude_agent_sdk session.

Unlike AssistantAgent (which only produces text), this agent executes its
role against a real filesystem workspace via the Claude Code agent loop —
it reads and writes files and runs shell commands in `cwd`, rather than
inlining code as chat text. See SPEC.md section 3 for the role prompts and
the workspace-directory convention this pairs with in pipeline.py.
"""

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from autogen_agentchat.agents import BaseChatAgent
from autogen_agentchat.base import Response
from autogen_agentchat.messages import BaseChatMessage, TextMessage
from autogen_core import CancellationToken
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]


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
            allowed_tools=self._allowed_tools,
            permission_mode="acceptEdits",
            max_turns=self._max_turns,
            model=self._model,
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
