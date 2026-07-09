"""GraphFlow pipeline: PM → Architect → (FE ∥ BE ∥ OPS) → QA → UAT, with rework loops."""

import re
from pathlib import Path
from typing import Mapping, Sequence

from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import BaseChatMessage
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

# Bounds for the architect's per-engineer turn-budget estimate (see
# ARCHITECT_PROMPT's "Turn Budget Estimate" section and
# apply_turn_budget_from_architect() below). Clamping protects both ends:
# a too-low estimate would reproduce the exact bug this mechanism exists to
# fix (an engineer starved of turns on a real task), and a too-high one
# would let one miscalibrated estimate blow the run's cost far past what
# the fixed ENGINEER_MAX_TURNS default used to risk.
MIN_ENGINEER_TURNS = 8
MAX_ENGINEER_TURNS = 50

_TURN_BUDGET_LINE = re.compile(r"^\s*(FE|BE|OPS)\s*:\s*(\d+)", re.MULTILINE)
_TURN_BUDGET_ROLE_TO_AGENT = {"FE": "frontend_engineer", "BE": "backend_engineer", "OPS": "devops_engineer"}


def parse_turn_budget(text: str) -> dict[str, int]:
    """Extract the architect's per-role turn estimates from its TECH
    DESIGN's "## Turn Budget Estimate" section (format one line per role:
    "FE: <N> — <reason>"). Each parsed value is clamped into
    [MIN_ENGINEER_TURNS, MAX_ENGINEER_TURNS]. A role that's missing or
    didn't parse is simply absent from the result — the caller keeps
    whatever budget that engineer already has (the static ENGINEER_MAX_TURNS
    default from build_team(), unless a prior turn already set one).
    """
    budget: dict[str, int] = {}
    for role, value in _TURN_BUDGET_LINE.findall(text):
        budget[role] = max(MIN_ENGINEER_TURNS, min(MAX_ENGINEER_TURNS, int(value)))
    return budget


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


def verdict_is(token: str):
    """Edge condition: the source agent's final non-empty line is exactly `token`.

    Matching the last line (not a substring) prevents a message that merely
    *mentions* both tokens (e.g. quoting the output template) from firing
    both conditional edge sets at once.
    """

    def check(message: BaseChatMessage) -> bool:
        return last_line(message.to_text()) == token

    return check


def build_team(
    workspace: Path,
    agent_cls: type[ClaudeCodeAgent] = ClaudeCodeAgent,
    on_event: OnEvent | None = None,
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
    """

    def code_agent(
        name: str,
        description: str,
        prompt: str,
        cwd: Path,
        max_turns: int,
        context_sources: dict[str, str],
        allowed_tools: Sequence[str] | None = None,
        pointer_files: dict[str, Path] | None = None,
    ) -> ClaudeCodeAgent:
        kwargs = {} if allowed_tools is None else {"allowed_tools": allowed_tools}
        return agent_cls(
            name=name,
            description=description,
            system_prompt=GLOBAL_RULES + "\n\n" + prompt,
            cwd=cwd,
            max_turns=max_turns,
            context_sources=context_sources,
            pointer_files=pointer_files,
            on_event=on_event,
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
    architect = code_agent(
        "solution_architect",
        "Turns the PRD into a production-grade technical design.",
        ARCHITECT_PROMPT,
        cwd=workspace,
        max_turns=REASONING_MAX_TURNS,
        context_sources={"user": "all", "product_manager": "latest"},
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
            "solution_architect": "latest",
            "frontend_engineer": "latest",
            "backend_engineer": "latest",
            "devops_engineer": "latest",
        },
        pointer_files=QA_POINTERS,
        # No Write/Edit: QA verifies and reports defects, it never modifies
        # the code it's independently checking — see README "Access per role".
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
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

    team = GraphFlow(
        participants=builder.get_participants(),
        graph=graph,
        termination_condition=termination,
    )
    agents = {
        "product_manager": pm,
        "solution_architect": architect,
        "frontend_engineer": fe,
        "backend_engineer": be,
        "devops_engineer": ops,
        "qa_engineer": qa,
        "uat_reviewer": uat,
        "release_reporter": reporter,
    }
    return team, agents
