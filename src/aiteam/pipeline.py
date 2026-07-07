"""GraphFlow pipeline: PM → Architect → (FE ∥ BE ∥ OPS) → QA → UAT, with rework loops."""

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow
from autogen_core.models import ChatCompletionClient

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


def build_team(model_client: ChatCompletionClient) -> GraphFlow:
    def agent(name: str, prompt: str) -> AssistantAgent:
        return AssistantAgent(
            name=name,
            model_client=model_client,
            system_message=GLOBAL_RULES + "\n\n" + prompt,
        )

    pm = agent("product_manager", PM_PROMPT)
    architect = agent("solution_architect", ARCHITECT_PROMPT)
    fe = agent("frontend_engineer", FE_PROMPT)
    be = agent("backend_engineer", BE_PROMPT)
    ops = agent("devops_engineer", OPS_PROMPT)
    qa = agent("qa_engineer", QA_PROMPT)
    uat = agent("uat_reviewer", UAT_PROMPT)
    reporter = agent("release_reporter", REPORTER_PROMPT)

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
