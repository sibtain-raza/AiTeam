"""Typed artifact schemas + validation for the inter-agent message protocol.

Every role's OUTPUT ARTIFACT format is specified prose-style in its prompt
(prompts.py / SPEC.md section 3), but until this module nothing ever
*checked* that an emitted message actually follows it — the only
machine-parsed parts of the protocol were the QA/UAT verdict tokens and the
architect's Turn Budget Estimate lines. This module makes the rest of the
contract explicit and checkable: each role has an ArtifactSpec (its kind,
expected title, required/optional sections), and validate_artifact()
returns a list of human-readable problems for a message that drifts from
its spec.

Validation is deliberately advisory, not fatal: a malformed artifact is
surfaced as a warning (printed by main.py, recorded as an
`artifact_warning` event by server/pipeline_runner.py) rather than crashing
the run — downstream agents are LLMs that usually cope with a missing
heading, and killing a half-finished $10 run over one is a worse outcome
than flagging it. The two protocol elements that ARE load-bearing for
routing (verdict tokens, turn budgets) keep their existing hard
enforcement in termination.py / pipeline.py; this validator re-checks the
verdict as one more advisory signal, it does not replace that.

Section matching is prefix-based and case-insensitive ("## Gaps" satisfies
a "Gaps" requirement even though the prompt writes "Gaps (if any)"), and
engineer titles are pattern-matched ("# FE IMPLEMENTATION" and the skip
path's "# FRONTEND_ENGINEER IMPLEMENTATION" both count) — the goal is to
catch real drift (an agent that stopped emitting its Defects table, a
truncated turn that never reached its artifact), not to punish cosmetic
variation.

Also here: find_blockers(). GLOBAL_RULES rule 1 tells every agent to state
`BLOCKER: <reason>` when an upstream decision blocks it — that's the
protocol's escalation message kind, but nothing ever scanned for it, so a
blocker was only as visible as a human reading the transcript happened to
make it. find_blockers() extracts those lines so both entry points can
surface them explicitly.
"""

import re
from dataclasses import dataclass, field

from .termination import last_line

_HEADING = re.compile(r"^(#{1,2})\s+(.+?)\s*$", re.MULTILINE)

# One line of a QA report that *defines* a defect: a D-n id with a severity
# on the same line (owner tags optional — re-verification lines like
# "- **D-1** BLOCKER [BE] — FIXED" also match, which is why parse_defects()
# dedupes by id keeping the first occurrence: the original definition).
# Real QA reports vary the framing (table rows, bold headers, bullets), so
# this matches the invariant parts only.
_DEFECT_LINE = re.compile(
    r"\bD-(\d+)\b[^\n]*?\b(BLOCKER|MAJOR|MINOR)\b([^\n]*)", re.MULTILINE
)
_OWNER_TAG = re.compile(r"\[(FE|BE|OPS)\]")

# GLOBAL_RULES rule 1: "state BLOCKER: <reason>". Anchored to line starts
# (possibly after list markers/emphasis) so prose that merely mentions the
# word "blocker" doesn't false-positive.
_BLOCKER_LINE = re.compile(r"^[\s\-*>]*\**BLOCKER\**\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class ArtifactSpec:
    """The schema one role's output artifact must follow."""

    kind: str  # protocol-level message kind, e.g. "PRD", "QA_REPORT"
    title_pattern: str  # regex the H1 heading must match
    required_sections: tuple[str, ...]  # H2s that must be present (prefix match)
    optional_sections: tuple[str, ...] = ()  # documented but legitimately absent (e.g. rework-only)
    verdict_tokens: frozenset[str] = field(default_factory=frozenset)  # non-empty ⇒ last line must be one


_ENGINEER_SECTIONS = ("Files Written", "Tasks Completed", "Assumptions")

# Keyed by agent name (message.source). Kept in sync with each role's
# OUTPUT ARTIFACT block in prompts.py/SPEC.md — same convention as
# context_sources: if a prompt's artifact format changes, change this too.
ARTIFACT_SPECS: dict[str, ArtifactSpec] = {
    "product_manager": ArtifactSpec(
        kind="PRD",
        title_pattern=r"PRD",
        required_sections=(
            "North Star Goal",
            "Personas & Problem",
            "User Stories",
            "Acceptance Criteria",
            "Non-Functional Requirements",
            "Out of Scope",
            "Definition of Done",
            "Assumptions",
        ),
    ),
    "scope_validator": ArtifactSpec(
        kind="SCOPE_REVIEW",
        title_pattern=r"SCOPE REVIEW",
        required_sections=(
            "Goal Restated",
            "Verdict",
            "Must-Cut Items",
            "Watch Items",
            "Missing Essentials",
            "Assumptions",
        ),
    ),
    "solution_architect": ArtifactSpec(
        kind="TECH_DESIGN",
        title_pattern=r"TECH DESIGN",
        required_sections=(
            "Stack",
            "Architecture",
            "Interface Contracts",
            "Cross-Cutting Design",
            "Task Breakdown",
            "Turn Budget Estimate",
            "Risks",
            "Assumptions",
        ),
    ),
    "frontend_engineer": ArtifactSpec(
        kind="FE_IMPLEMENTATION",
        # "FE IMPLEMENTATION" normally; "FRONTEND_ENGINEER IMPLEMENTATION"
        # from the zero-budget skip path in ClaudeCodeAgent.on_messages().
        title_pattern=r"(FE|FRONTEND_ENGINEER) IMPLEMENTATION",
        required_sections=_ENGINEER_SECTIONS,
        optional_sections=("Defects Fixed",),
    ),
    "backend_engineer": ArtifactSpec(
        kind="BE_IMPLEMENTATION",
        title_pattern=r"(BE|BACKEND_ENGINEER) IMPLEMENTATION",
        required_sections=_ENGINEER_SECTIONS,
        optional_sections=("Defects Fixed",),
    ),
    "devops_engineer": ArtifactSpec(
        kind="OPS_IMPLEMENTATION",
        title_pattern=r"(OPS|DEVOPS_ENGINEER) IMPLEMENTATION",
        # The skip path emits the shared engineer skeleton without a
        # Runbook section, so Runbook is checked but only when the role
        # actually did work — see validate_artifact()'s skip handling.
        required_sections=_ENGINEER_SECTIONS + ("Runbook",),
        optional_sections=("Defects Fixed",),
    ),
    "qa_engineer": ArtifactSpec(
        kind="QA_REPORT",
        title_pattern=r"QA REPORT",
        required_sections=("Traceability", "Defects", "Verdict"),
        verdict_tokens=frozenset({"QA_PASS", "QA_FAIL"}),
    ),
    "uat_reviewer": ArtifactSpec(
        kind="UAT_REPORT",
        title_pattern=r"UAT REPORT",
        required_sections=("Story Walkthrough", "Gaps", "Final Summary"),
        verdict_tokens=frozenset({"UAT_APPROVED", "UAT_REJECTED"}),
    ),
    "release_reporter": ArtifactSpec(
        kind="FINAL_DELIVERY_REPORT",
        title_pattern=r"FINAL DELIVERY REPORT",
        required_sections=(
            "Goal",
            "What Was Delivered",
            "How To Run It",
            "Known Limitations",
            "Recommended Next Steps",
            "Artifacts Index",
        ),
    ),
}

# The skip path's canned message (zero-budget engineer) is a legitimate
# artifact that intentionally lacks role-specific extras like OPS's
# Runbook. Detected by its fixed phrasing from ClaudeCodeAgent.on_messages().
_SKIP_MARKER = "No tasks were assigned to this role in the TECH DESIGN"


def classify(source: str) -> str | None:
    """The protocol-level message kind for an agent's artifact, or None
    for a source with no schema (e.g. the user's goal message)."""
    spec = ARTIFACT_SPECS.get(source)
    return spec.kind if spec else None


def _headings(text: str) -> tuple[list[str], list[str]]:
    """(h1 texts, h2 texts) in document order."""
    h1s, h2s = [], []
    for hashes, title in _HEADING.findall(text):
        (h1s if hashes == "#" else h2s).append(title)
    return h1s, h2s


def validate_artifact(source: str, text: str) -> list[str]:
    """Check `text` against `source`'s ArtifactSpec. Returns a list of
    human-readable problems — empty means the artifact conforms. Unknown
    sources (no spec) validate clean by definition."""
    spec = ARTIFACT_SPECS.get(source)
    if spec is None:
        return []

    problems: list[str] = []
    h1s, h2s = _headings(text)

    if not any(re.fullmatch(spec.title_pattern, h1) for h1 in h1s):
        problems.append(
            f"missing '# ...' title matching {spec.title_pattern!r} (kind {spec.kind})"
        )

    skipped = _SKIP_MARKER in text
    required = spec.required_sections
    if skipped:
        # The canned skip message carries only the shared engineer skeleton.
        required = tuple(s for s in required if s in _ENGINEER_SECTIONS)
    h2s_lower = [h.lower() for h in h2s]
    for section in required:
        if not any(h.startswith(section.lower()) for h in h2s_lower):
            problems.append(f"missing required section '## {section}'")

    if spec.verdict_tokens and last_line(text) not in spec.verdict_tokens:
        problems.append(
            f"last line is not one of {sorted(spec.verdict_tokens)} — "
            f"routing relies on it (safety net in ClaudeCodeAgent should have caught this)"
        )

    return problems


@dataclass(frozen=True)
class Defect:
    """One defect extracted from a QA report. This is the pragmatic version
    of 'structured JSON artifacts': the artifacts stay Markdown (LLMs
    consume them; prose evidence matters), and the parts that programmatic
    consumers need — defect ids, severities, owners — are extracted into
    data instead of forcing every agent to emit strict JSON."""

    id: str  # "D-1"
    severity: str  # "BLOCKER" | "MAJOR" | "MINOR"
    owners: tuple[str, ...]  # subset of ("FE", "BE", "OPS"); may be empty
    # Cleaned remainder of the defining line — a short human-readable gist,
    # not the full evidence. Feeds run_memory's cross-run defect hints
    # ("[BLOCKER] backend build fails under noUnusedParameters..."); ""
    # when the line had nothing after the severity/owner tags.
    summary: str = ""


def _clean_defect_summary(rest: str) -> str:
    """Strip markdown/table decoration from a defect line's tail so it
    reads as prose in a prompt hint. Truncated — hints are reminders, the
    full report lives in the run's own artifacts."""
    cleaned = _OWNER_TAG.sub("", rest)
    cleaned = re.sub(r"[*_`|]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" —–-:;,. ")
    return cleaned[:160]


def parse_defects(text: str) -> list[Defect]:
    """Extract defects from a QA report, tolerant of the formatting
    variation real QA sessions produce (table rows, bold headers, bullet
    re-verifications). Deduped by id, first occurrence wins — a defect's
    defining line precedes its re-verification mentions in the same
    report. Returns [] for text with no defect lines (e.g. a clean pass)."""
    seen: dict[str, Defect] = {}
    for number, severity, rest in _DEFECT_LINE.findall(text):
        defect_id = f"D-{number}"
        if defect_id in seen:
            continue
        owners = tuple(dict.fromkeys(_OWNER_TAG.findall(rest)))
        seen[defect_id] = Defect(
            id=defect_id,
            severity=severity,
            owners=owners,
            summary=_clean_defect_summary(rest),
        )
    return list(seen.values())


def find_blockers(text: str) -> list[str]:
    """All `BLOCKER: <reason>` escalation lines in an artifact (GLOBAL_RULES
    rule 1) — the protocol's way for a downstream agent to challenge an
    upstream decision. Callers surface these; nothing routes on them."""
    return [m.strip() for m in _BLOCKER_LINE.findall(text)]
