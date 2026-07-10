"""Custom termination: hard-stop the pipeline after N QA failures."""

from typing import Sequence

from autogen_agentchat.base import TerminatedException, TerminationCondition
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage, StopMessage


def last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


class TokenCountTermination(TerminationCondition):
    """Terminate once `token` has been the final line of `source`'s messages `max_count` times.

    Used to cap QA rework loops: on the 3rd QA_FAIL the run stops with a
    PIPELINE_FAILED stop message instead of looping (or silently hitting a
    message cap).
    """

    def __init__(self, token: str, source: str, max_count: int) -> None:
        self._token = token
        self._source = source
        self._max_count = max_count
        self._count = 0
        self._terminated = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    async def __call__(
        self, messages: Sequence[BaseAgentEvent | BaseChatMessage]
    ) -> StopMessage | None:
        if self._terminated:
            raise TerminatedException("Termination condition has already been reached")
        for message in messages:
            if not isinstance(message, BaseChatMessage):
                continue
            if message.source != self._source:
                continue
            if last_line(message.to_text()) == self._token:
                self._count += 1
                if self._count >= self._max_count:
                    self._terminated = True
                    return StopMessage(
                        content=(
                            f"PIPELINE_FAILED: {self._source} emitted {self._token} "
                            f"{self._count} times (max {self._max_count})"
                        ),
                        source="TokenCountTermination",
                    )
        return None

    async def reset(self) -> None:
        self._count = 0
        self._terminated = False
