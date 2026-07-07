"""GraphFlow pipeline: PM → Architect → (FE ∥ BE ∥ OPS) → QA → UAT, with rework loops."""

from pathlib import Path
from typing import Sequence

from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow

from .claude_code_agent import ClaudeCodeAgent
from .prompts import (
    ARCHITECT_PROMPT,
    BE_PROMPT,
    FE_PROMPT,
    GLOBAL_RULES,
    OPS_PROMPT,
    PM_PROMPT,
    QA_PROMPT,
    REPORTER_PROMPT,
    UAT_PROMPT,
)
from .termination import TokenCountTermination, last_line

MAX_QA_LOOPS = 3
# Base pipeline is 7 messages; each QA loop re-runs 3 engineers + QA (4).
# Cap generously above 7 + 3*4 + UAT-reject re-scope headroom.
MAX_MESSAGES = 40


def verdict_is(token: str):
    """Edge condition: the source agent's final non-empty line is exactly `token`.

    Matching the last line (not a substring) prevents a message that merely
    *mentions* both tokens (e.g. quoting the output template) from firing
    both conditional edge sets at once.
    """

    def check(message: BaseChatMessage) -> bool:
        return last_line(message.to_text()) == token

    return check


def build_team(workspace: Path) -> GraphFlow:
    """Build the pipeline. Every agent runs via a real claude_agent_sdk
    session authenticated through the `claude` CLI's own OAuth login — no
    ANTHROPIC_API_KEY/OPENAI_API_KEY needed. See claude_code_agent.py.

    PM/Architect/UAT/Reporter are pure reasoning roles: same execution
    backend, but with `allowed_tools=[]` so they can't touch the filesystem.
    FE/BE/OPS/QA get real file/bash tools scoped to `workspace` (a per-run
    directory) so they actually write and verify code.

    Workspace layout (also encoded in prompts.py — keep the two in sync):
      workspace/frontend/   — frontend_engineer's cwd
      workspace/backend/    — backend_engineer's cwd
      workspace/            — devops_engineer's and qa_engineer's cwd (root,
                               so OPS can reference ./frontend and ./backend,
                               and QA can read/test everything); also the
                               cwd for the tool-less reasoning agents, who
                               never use it
    """

    def code_agent(
        name: str,
        description: str,
        prompt: str,
        cwd: Path,
        max_turns: int,
        allowed_tools: Sequence[str] | None = None,
    ) -> ClaudeCodeAgent:
        kwargs = {} if allowed_tools is None else {"allowed_tools": allowed_tools}
        return ClaudeCodeAgent(
            name=name,
            description=description,
            system_prompt=GLOBAL_RULES + "\n\n" + prompt,
            cwd=cwd,
            max_turns=max_turns,
            **kwargs,
        )

    # Tool-less reasoning roles never make a tool call, so they always finish
    # in a single turn — a small cap here is just a runaway guard, not a
    # working budget. Engineer/QA budgets are a real cost lever: each is a
    # full agentic session (writing files, running builds/tests), and QA
    # rework loops repeat FE/BE/OPS, so these numbers multiply fast across a
    # run. Lower them further (or set AITEAM_CODE_MODEL to a cheaper model)
    # if you're hitting the Claude Code session/usage limit.
    REASONING_MAX_TURNS = 3
    ENGINEER_MAX_TURNS = 20
    QA_MAX_TURNS = 20

    pm = code_agent(
        "product_manager",
        "Grooms the raw goal into a build-ready PRD.",
        PM_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        allowed_tools=[],
    )
    architect = code_agent(
        "solution_architect",
        "Turns the PRD into a production-grade technical design.",
        ARCHITECT_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        allowed_tools=[],
    )
    fe = code_agent(
        "frontend_engineer",
        "Implements [FE] tasks from the TECH DESIGN by writing real code to the shared workspace.",
        FE_PROMPT,
        cwd=workspace / "frontend",
        max_turns=ENGINEER_MAX_TURNS,
    )
    be = code_agent(
        "backend_engineer",
        "Implements [BE] tasks from the TECH DESIGN by writing real code to the shared workspace.",
        BE_PROMPT,
        cwd=workspace / "backend",
        max_turns=ENGINEER_MAX_TURNS,
    )
    ops = code_agent(
        "devops_engineer",
        "Implements [OPS] tasks from the TECH DESIGN by writing real Docker/CI config to the shared workspace.",
        OPS_PROMPT,
        cwd=workspace,
        max_turns=ENGINEER_MAX_TURNS,
    )
    qa = code_agent(
        "qa_engineer",
        "Verifies the real implementation in the shared workspace against the PRD and TECH DESIGN.",
        QA_PROMPT,
        cwd=workspace,
        max_turns=QA_MAX_TURNS,
    )
    uat = code_agent(
        "uat_reviewer",
        "The same PM performing UAT against the original goal.",
        UAT_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        allowed_tools=[],
    )
    reporter = code_agent(
        "release_reporter",
        "Summarizes the approved run into a final delivery report.",
        REPORTER_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        allowed_tools=[],
    )

    builder = DiGraphBuilder()
    for a in (pm, architect, fe, be, ops, qa, uat, reporter):
        builder.add_node(a)

    builder.add_edge(pm, architect)

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
            condition=verdict_is("QA_FAIL"),
            activation_group=group,
            activation_condition="any",
        )

    # Fan-in to QA: default "all" activation — QA waits for all three engineers.
    builder.add_edge(fe, qa)
    builder.add_edge(be, qa)
    builder.add_edge(ops, qa)

    builder.add_edge(qa, uat, condition=verdict_is("QA_PASS"))

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
        # 3rd QA_FAIL ⇒ PIPELINE_FAILED; 2nd UAT_REJECTED ⇒ re-scope only once.
        TokenCountTermination("QA_FAIL", source=qa.name, max_count=MAX_QA_LOOPS)
        | TokenCountTermination("UAT_REJECTED", source=uat.name, max_count=2)
        | MaxMessageTermination(MAX_MESSAGES)
    )

    return GraphFlow(
        participants=builder.get_participants(),
        graph=graph,
        termination_condition=termination,
    )
