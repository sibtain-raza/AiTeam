# AiTeam

An experimental autonomous software delivery pipeline: eight AI agents take a raw one-line goal and carry it through grooming, design, implementation, QA, and UAT — with the engineering roles actually writing files, running builds, and executing tests in a real workspace on disk, not just describing code in chat.

This document explains what it is, why it's built the way it is, what's actually been verified versus what hasn't, and what to watch out for.

---

## At a glance

| | |
|---|---|
| **Orchestration** | [AutoGen AgentChat](https://microsoft.github.io/autogen/)'s `GraphFlow` — a deterministic directed graph, not free-form agent chat |
| **Execution** | [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) — real file/bash access for the engineering roles |
| **Auth** | `claude auth login` only — no `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` anywhere |
| **Roles** | PM → Architect → (Frontend ∥ Backend ∥ DevOps) → QA → UAT → Release Reporter |
| **Rework** | Up to 3 QA-fail loops, 1 UAT-reject loop, then a hard stop |
| **Status** | Experimental, unbenchmarked — see [Honest limitations](#honest-limitations-and-tradeoffs) before trusting it with anything real |

Want to just run it? Jump to [Getting started](#getting-started).

---

## Contents

- [The problem](#the-problem)
- [How it works](#how-it-works)
- [Evidence this works](#evidence-this-works-not-just-theory)
- [Checkpoint & resume](#checkpoint-resume)
- [Getting started](#getting-started)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Security & access control](#security-access-control)
- [Honest limitations and tradeoffs](#honest-limitations-and-tradeoffs)
- [Comparisons & alternatives](#comparisons-alternatives)
- [Possible future work](#possible-future-work)

---

## The problem

Most multi-agent "AI software team" demos share a specific weakness: every role, including QA, communicates entirely in chat text. An "implementation" is a fenced code block in a message; a "QA report" is the QA agent reading that text and reasoning about whether it looks correct. Nothing is ever actually built, run, or tested. Concretely, that means:

- QA can be fooled by code that reads correctly but doesn't compile or run.
- An "OPS deliverable" can claim a Docker setup works without `docker build` ever having been run against it.
- A defect can be marked "fixed" because the diff looks plausible, not because a previously-failing check now passes.

AiTeam exists to close that gap for the roles where it matters — the engineers and QA — while keeping the roles that are genuinely pure reasoning tasks (grooming, architecture, UAT judgment) as plain text generation, since tool access wouldn't help them there.

---

## How it works

Two different techniques, combined in one pipeline:

1. **AutoGen's `GraphFlow` drives the *process*.** The pipeline is a literal directed graph, not "agents chat until they agree." Routing is by exact verdict tokens (`QA_PASS`/`QA_FAIL`, `UAT_APPROVED`/`UAT_REJECTED`) matched against the last non-empty line of a message — never a substring match, since a prompt or template might mention both tokens. Rework loops are hard-capped, so a run can never loop forever.

2. **The Claude Agent SDK drives *execution*** for the engineering roles. Instead of an LLM completion that inlines code as chat text, `frontend_engineer`, `backend_engineer`, `devops_engineer`, and `qa_engineer` each run as a real, multi-tool-call agent session scoped to a shared workspace directory on disk — FE/BE/OPS can `Read`, `Write`, `Edit`, `Glob`, `Grep`, and run `Bash`; QA gets `Read`/`Glob`/`Grep`/`Bash` but deliberately *not* `Write`/`Edit` (full breakdown in [Security & access control](#security-access-control)). Code is written to real files. QA reads and *executes* those files instead of parsing pasted-in text — it verifies, it doesn't modify.

Every one of the eight agents — including the four pure-reasoning roles (`product_manager`, `solution_architect`, `uat_reviewer`, `release_reporter`) — runs through the same `ClaudeCodeAgent` wrapper class (`src/aiteam/claude_code_agent.py`). The only difference is whether tools are enabled: the reasoning roles get `allowed_tools=[]` (no filesystem access at all), the engineering roles get real file/bash tools. This also means the entire pipeline authenticates through one mechanism — the `claude` CLI's own OAuth login — with no API key required anywhere.

**Context routing keeps prompts bounded.** Every Claude Code session starts with amnesia, so each agent replays its accumulated message history as the prompt on every turn — but GraphFlow broadcasts every message to every agent, and an unfiltered replay grows with the whole transcript: an engineer's rework prompt would re-send the other engineers' summaries and every superseded QA report alongside the current one (wasted tokens *and* a stale-context hazard). Each role's replayed prompt is therefore filtered (`context_sources` in `pipeline.py`) to exactly the artifacts its role spec declares as inputs, keeping only the latest version per source — an engineer sees the goal, the latest TECH DESIGN, and the latest QA report, nothing else.

**Pointer routing turns push into pull on rework turns.** The pipeline's artifacts already cross-reference each other by stable IDs (`AC-n`, `T-n`, `D-n`) — a knowledge graph serialized as markdown. Rather than maintaining a separate graph store, `main.py` persists each artifact to `<workspace>/.pipeline-docs/<source>.md` as it's produced, and on turns where a large artifact is *unchanged since the agent last received it* (QA rework loops), the replay collapses it to a one-line pointer at that file — the agent `Read`s/`Grep`s back only the sections its defects reference, following the ID cross-references itself. The effectiveness guardrail: an artifact is **always inlined in full the first time it reaches an agent** (first pass, or a redesign after a UAT re-scope), so retrieval can never hide a contract the agent was never shown; a missing file falls back to full inline; tool-less reasoning roles are never given pointers. Measured on a realistic second-QA-round engineer turn: unfiltered replay 26.3K chars → context routing 11.1K (−58%) → routing + pointer 3.3K (−87%), with the saving compounding every additional loop (the agent reads back what it needs, so real spend scales with what the defects touch, not with design size).

### The pipeline

```
USER GOAL
   │
   ▼
[PM: Groom] ──► PRD (user stories, Given/When/Then acceptance criteria, NFRs)
   │
   ▼
[Solution Architect] ──► TECH DESIGN (stack, interface contracts, task breakdown tagged FE/BE/OPS)
   │
   ├──► [Frontend Engineer]  writes real files to  workspace/frontend/
   ├──► [Backend Engineer]   writes real files to  workspace/backend/
   └──► [DevOps Engineer]    writes real Docker/CI/RUNBOOK to  workspace/
   │
   ▼
[QA Engineer] — reads the real files in the workspace, runs real builds/tests/lint against them
   │
   ├── QA_FAIL (up to 3 loops) ──► only the owning engineer(s) rework — QA's defect tags route it
   └── QA_PASS
          │
          ▼
[PM: UAT] — validates against the ORIGINAL goal, not the PRD (catches "built the wrong thing correctly")
   │
   ├── UAT_REJECTED (once) ──► back to PM grooming, re-scope
   └── UAT_APPROVED ──► [Release Reporter] ──► done
```

Full role-by-role prompts and the formal orchestration spec are in [SPEC.md](SPEC.md); the implementation graph is `src/aiteam/pipeline.py`.

### Workspace layout

Each run gets one workspace directory (`output/workspace/<timestamp>/`), shared by the four tool-using agents so they can see each other's work and QA can verify the whole thing:

| Path | Who writes there | Why |
|---|---|---|
| `workspace/frontend/` | `frontend_engineer` | Isolated from backend to avoid concurrent-write collisions (FE and BE run in parallel) |
| `workspace/backend/` | `backend_engineer` | Same reasoning |
| `workspace/` (root) | `devops_engineer` | Needs to reference `./frontend` and `./backend` in Dockerfiles/compose/CI |
| `workspace/` (root) | `qa_engineer` (read/execute) | Needs to see and run everything FE/BE/OPS produced |

This convention is encoded directly into the FE/BE/OPS/QA prompts (`src/aiteam/prompts.py` and `SPEC.md`, kept byte-identical on purpose) — the path names there and the directory assignment in `pipeline.py` have to stay in sync.

### Why this combination, specifically

> **Why AutoGen for the process, and not something more free-form?**
> The pipeline has a genuinely deterministic shape — grooming always precedes design, QA always gates UAT, a defect always routes back to the team that owns it. Encoding that as an explicit graph with conditional edges means the control flow is *data you can read and test*, not emergent behavior you have to hope an LLM negotiates correctly turn after turn. The one real gotcha this surfaces: any node with two incoming edges (an engineer reachable from both the architect and a QA-rework loop; the PM reachable from both the entry point and a UAT-reject loop) needs both edges in a shared `activation_group` with `activation_condition="any"` — GraphFlow defaults to requiring *all* incoming edges to fire, which deadlocks the first pass. Documented as a load-bearing invariant in `CLAUDE.md`.

> **Why Claude Agent SDK for execution, and not just a bigger prompt?**
> No amount of prompting makes a text-only "QA agent" catch a Dockerfile with a SHA-256 digest that's one character short — that's a fact about a build tool's behavior, not about code quality that's visible by inspection. Giving QA (and the engineers) a real, tool-using agent loop scoped to a real workspace turns "does this look right" into "does this actually run."

> **Why route everything through the `claude` CLI instead of a raw API key?**
> Simplicity of one auth path — the reasoning roles and the execution roles use the exact same underlying primitive (`claude_agent_sdk.query()`), just configured differently (tools on/off). The tradeoff is real: this is a genuine single-vendor dependency, not a neutral choice — see [Honest limitations](#honest-limitations-and-tradeoffs).

---

## Evidence this works (not just theory)

This was built and tested interactively, not designed on paper and left unverified. The most convincing proof point: on a real run building a small Go CLI tool, `qa_engineer` — with actual filesystem and Bash access — found genuine, reproducible defects that a text-reading QA agent could never have caught:

- **Truncated digests.** OPS's Dockerfile pinned base images with SHA-256 digests that were 63 hex characters instead of the required 64. `docker build` failed immediately — caught because QA actually ran `docker build`, not by reading the Dockerfile.
- **Missing `go.sum`.** The same Dockerfile did `COPY go.mod go.sum ./`, but `go.sum` didn't exist (the module has zero third-party dependencies, so Go correctly never generated one) — a second, independent build failure, also only visible by actually running the build.
- **No-op test targets.** The Makefile's `test-integration` and `bench` targets matched zero real test/benchmark functions and silently reported success while testing nothing. Caught by actually running `make test-integration`.
- **A false CI claim.** A CI workflow comment claimed pty-based subprocess test coverage existed for two acceptance criteria; `grep` proved no such test existed anywhere in the repo.

We also verified, deliberately:

- **The rework loop uses the filesystem as memory, not chat replay.** A `backend_engineer` on a QA-rework turn re-read its own file from disk (not from any replayed conversation) and correctly determined a claimed defect wasn't reproducible — the `argv` length check was already there — and correctly left the code alone instead of blindly "fixing" something that wasn't broken.
- **Checkpoint/resume works on a cold process, not just the same one.** We killed a real run mid-flight (right after `product_manager` finished), resumed from its checkpoint in a brand-new process with brand-new agent objects, and confirmed it picked up exactly where it left off — `solution_architect` ran next, correctly saw `product_manager`'s PRD, and `product_manager` itself was never re-run.
- **Tools-disabled mode works cleanly** for the four reasoning roles — a `ClaudeCodeAgent` with `allowed_tools=[]` reliably produces a single-turn text response with no wasted tool-call attempts.
- **The permission guard works in both directions, verified live.** A real session had a `curl` call blocked by policy; a separate real session successfully wrote a file and ran a safe command — and when that same session first tried to write outside its workspace, it was denied, read the reason, and correctly retried inside its own directory instead of failing. Details in [Security & access control](#security-access-control).

---

## Checkpoint & resume

Why this exists: the first time we ran the full pipeline, `frontend_engineer`'s Claude Code session hit the account's usage limit partway through a QA rework loop, and the run died with nothing recoverable — a restart meant re-running PM, Architect, and everything else from scratch. Two gaps caused that:

1. `main.py` only wrote its transcript *after* the whole run finished — a mid-run crash meant nothing was ever persisted.
2. AutoGen's `GraphFlow` supports `save_state()`/`load_state()` for exactly this, but nothing called it. Separately, `ClaudeCodeAgent` didn't override those methods either, so even a team-level checkpoint would have lost each agent's accumulated context (the upstream chat history it replays into every fresh `query()` call, since a Claude Code session has no memory of its own across turns).

Both are now fixed:

- After every completed agent turn, `main.py` calls `GraphFlow.save_state()` and overwrites `output/checkpoints/<timestamp>.json` (goal, workspace path, full team state — including every agent's replay history via `ClaudeCodeAgent.save_state()`).
- On any exception, the run stops and prints the checkpoint path.
- `python -m aiteam.main --resume output/checkpoints/<timestamp>.json` rebuilds the team against the *same* workspace, loads the saved state, and continues with `run_stream(task=None)` — AutoGen's own mechanism for "continue the previous task."

> **What resume does *not* give you:** recovery is per completed turn, not per tool call. If a turn is interrupted mid-session (like the quota hit did, inside a `frontend_engineer` session), resuming retries that *whole* turn from scratch — you don't get partial credit for tool calls already made inside a turn that never finished. What you don't lose is everything *before* that turn, and any files a previous, successfully-completed turn already wrote to the workspace.

---

## Getting started

### Install

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Every agent runs via the Claude Agent SDK, which shells out to the **`claude` CLI (Claude Code)** and uses its own login — no API key needed. Install the CLI and log in once:

```sh
npm install -g @anthropic-ai/claude-code   # or: brew install --cask claude-code
claude auth login                          # opens a browser; one-time
claude auth status                          # confirm you're logged in
```

### Run

```sh
export PYTHONPATH=src
.venv/bin/python -m aiteam.main "Build a URL shortener with custom aliases and click analytics."
```

The run streams to the console and writes:

- a full transcript to `output/run-<timestamp>.md` (appended incrementally, so it survives a crash),
- the real generated code to `output/workspace/<timestamp>/`,
- a resumable checkpoint to `output/checkpoints/<timestamp>.json` after every completed turn.

If a run fails partway (Claude session/usage limit, a crash, anything), resume instead of starting over:

```sh
.venv/bin/python -m aiteam.main --resume output/checkpoints/<timestamp>.json
```

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AITEAM_CODE_MODEL` | Claude Code's own default | Model override for all 8 agents |
| `AITEAM_OUTPUT_DIR` | `output` | Directory for transcripts, workspaces, and checkpoints |

`AITEAM_PROVIDER` / `AITEAM_MODEL` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in `.env.example` are **not used by default**. They're read only by `src/aiteam/config.py`, which is currently unwired dead code, kept intentionally in case the pipeline is ever pointed back at a raw-API-key provider (`AnthropicChatCompletionClient`/`OpenAIChatCompletionClient` via `autogen-ext`) instead of the `claude` CLI. Setting them has no effect on a normal run.

Per-role turn budgets are set in `src/aiteam/pipeline.py` (not env-configurable today): the four reasoning roles are capped at 3 turns (they never call a tool, so this is just a runaway guard); the four engineering roles are capped at 20 turns each. Lower these if you're hitting session/usage limits — see [Honest limitations](#honest-limitations-and-tradeoffs).

---

## Project layout

| File | What's there |
|---|---|
| `SPEC.md` | The authoritative behavior spec: full role prompts, orchestration rules, design rationale. Kept in lockstep with `prompts.py`. |
| `src/aiteam/prompts.py` | Global rules + the 8 role system prompts, verbatim from `SPEC.md` section 3. |
| `src/aiteam/pipeline.py` | The `GraphFlow` graph: fan-out/fan-in, conditional rework edges, per-role turn budgets, workspace directory assignment. |
| `src/aiteam/claude_code_agent.py` | `ClaudeCodeAgent`, the `BaseChatAgent` every role runs as: a real `claude_agent_sdk` session, with or without tool access, a `PreToolUse` permission guard, and `save_state()`/`load_state()` for checkpoint/resume. |
| `src/aiteam/termination.py` | `TokenCountTermination`, the QA-fail counter that hard-stops a run with `PIPELINE_FAILED` after 3 rework loops. |
| `src/aiteam/main.py` | CLI entry point: run/resume, per-turn checkpointing, transcript writing. |
| `src/aiteam/config.py` | Unused: a provider-configurable AutoGen model client, kept for a possible future raw-API-key path (see [Configuration](#configuration)). |
| `CLAUDE.md` | Instructions and invariants for AI coding agents working on this repo (activation-group deadlock gotcha, verdict-token matching rules, etc.) — read it before changing orchestration code. |
| `tests/` | Orchestration tests against a scripted mock — see [Testing](#testing) below. |

---

## Testing

Every live verification in [Evidence this works](#evidence-this-works-not-just-theory) was run against the real `claude` CLI — accurate, but slow and consuming real Claude Code session quota, which makes it a bad fit for routine regression testing. `tests/` covers the *orchestration* logic (GraphFlow routing, rework loops, hard-stop termination, checkpoint/resume) separately, against a scripted stand-in that returns canned text instead of calling `claude_agent_sdk` — deterministic, instant, and free.

```sh
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -t . -v
```

- `tests/mock_agent.py` — `ScriptedClaudeCodeAgent`, a `ClaudeCodeAgent` subclass that overrides only the SDK call; `save_state()`/`load_state()` and the rest of `BaseChatAgent` are inherited unchanged, so a passing test genuinely exercises the checkpoint/resume machinery, not a reimplementation of it. `build_team()` takes an `agent_cls` parameter for exactly this — production code (`main.py`) never passes it, so the real pipeline is unaffected.
- `tests/test_pipeline.py` — five scenarios: the happy path (every role runs once), a QA rework loop that fails once then passes, three consecutive `QA_FAIL`s hitting the hard `PIPELINE_FAILED` stop, a UAT rejection loop, and checkpoint/resume recovering from a simulated crash.

> **A real bug in the test's own design, caught before it shipped:** the checkpoint/resume test originally used `break` to simulate an interrupted run — stop consuming the message stream after `product_manager`'s turn, then save state. It failed: `resumed` sources came back as *all eight roles starting from `product_manager` again*. The cause wasn't a resume bug — `GraphFlow`'s runtime is a producer/consumer queue, and with near-instant scripted responses it kept processing in the background regardless of whether the test was still reading from the stream, racing the entire pipeline to completion before `save_state()` was ever called. The fix was a `CRASH` script sentinel that makes a turn genuinely raise an exception — the same thing that actually halts the runtime in production (the real Claude Code session-limit `RuntimeError`) — rather than a consumer merely pausing, which doesn't. Left as a comment in the test itself as a warning against the more "obvious" `break`-based version.

---

## Security & access control

### Access per role

Not every role gets the same tools, and the tools that are granted are further restricted by a permission guard, enforced at every tool call.

> **A real bug this table used to be wrong about.** For most of this project's early life, tool restriction was set only via `allowed_tools=`. Live testing (prompted by "check for AI leak" — good instinct) found that `allowed_tools` doesn't restrict anything: per `claude_agent_sdk`'s own docs, it only skips the permission prompt for tools already available. A session built with `allowed_tools=["Read"]` and no further restriction could still run `Bash` — confirmed live. That means every role, including the four "pure reasoning" ones below, had Claude Code's full default toolset available the entire time, gated only by whether the prompt happened to ask for it. The fix: `ClaudeCodeAgent` now also sets `tools=`, the field that actually removes a tool from the model's context — confirmed live that a `tools=[]` agent genuinely cannot invoke any tool, and a QA-like agent with `Write`/`Edit` excluded from `tools` genuinely refuses a `Write` call. The table below now reflects what's actually enforced, not what `allowed_tools` alone implied.

| Role | Tools | Write scope | Notes |
|---|---|---|---|
| `product_manager` | *(none)* | — | Pure text reasoning. Cannot touch the filesystem or run anything. |
| `solution_architect` | *(none)* | — | Same. |
| `uat_reviewer` | *(none)* | — | Same — deliberately judges the *original goal* against summaries, not the code. |
| `release_reporter` | *(none)* | — | Same — summarizes the record, doesn't re-inspect anything. |
| `frontend_engineer` | `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash` | `workspace/frontend/` | Can install deps and self-verify (run its own build/tests) via Bash. |
| `backend_engineer` | `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash` | `workspace/backend/` | Same. |
| `devops_engineer` | `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash` | `workspace/` (root) | Needs root access to write Dockerfiles/compose/CI referencing `./frontend` and `./backend`. |
| `qa_engineer` | `Read`, `Glob`, `Grep`, `Bash` — **no `Write`/`Edit`** | *(read-only, with a caveat)* | Genuinely cannot call `Write`/`Edit` now. But it still has `Bash`, and Bash can replicate file-writing (`echo ... > file`) — verified live: asked to write a file, it correctly refused `Write`, then *offered* to do the same thing via `Bash` instead. QA needs Bash for real test/build execution, so this isn't a bug to trivially fix — see the caveat below the table. |

### The permission guard

Every tool-using role (FE/BE/OPS/QA) runs with a `PreToolUse` hook (`_make_hooks` in `claude_code_agent.py`) that fires on *every* tool call before it executes, independent of what's in `allowed_tools`:

- **`Write`/`Edit`** calls targeting a path that resolves outside that agent's own workspace directory are denied — precise path resolution (handles both `..` traversal and absolute paths), not a string match.
- **`Bash`** commands matching a short denylist (`rm -rf`/`rm -fr`, `sudo`, `curl`, `wget`, `git push`, `chmod -R 777`, `mkfs`, `dd if=`, `shutdown`, `reboot`) are denied.

This was built the way it was for a specific, verified reason: the SDK's own `can_use_tool` callback looked like the obvious mechanism for this, but a live test showed a real `curl` call sail straight through it — `allowed_tools` entries that grant a whole tool (e.g. plain `"Bash"`) auto-approve every call before `can_use_tool` is ever consulted (`CanUseToolShadowedWarning`). `PreToolUse` hooks don't have that shadowing problem, which is why the guard is built on hooks, not `can_use_tool`. Both the deny paths and the legitimate-call paths were verified live — see [Evidence this works](#evidence-this-works-not-just-theory).

> **What this is not: a sandbox.** It's a string-match denylist on the literal Bash command and a path-resolution check on Write/Edit — both enforced in this process, not by the OS. A sufficiently adversarial command can likely evade the denylist (e.g. an alternate spelling of a blocked binary, or a destructive action the list doesn't happen to name). Real OS-level filesystem/network confinement is `claude_agent_sdk`'s `SandboxSettings` (macOS/Linux), which isn't wired in yet — see [Possible future work](#possible-future-work).

### Other notes

- **QA's read-only boundary is a tool restriction, not a Bash restriction.** Removing `Write`/`Edit` from `tools` genuinely blocks those two tools, but QA keeps `Bash` (it needs to run real builds/tests), and Bash can write files by other means (shell redirection, `sed -i`, etc.) that aren't on the `_BASH_DENY_PATTERNS` denylist — blocking generic redirection would also block QA's legitimate build/test output, so this wasn't tightened further. Treat "QA doesn't modify code" as a strong prompting-level expectation reinforced by removing the two most direct tools, not an airtight guarantee.
- Treat `output/workspace/<timestamp>/` as untrusted-generated-code territory regardless of the guard above. It's written by an AI agent with real Bash access and no human review of individual commands.
- No secrets should ever be required in code or config the agents write — `GLOBAL_RULES` in `prompts.py` mandates this, and BE/OPS prompts specifically forbid committed secrets, but this is enforced by prompting, not by a code-level guarantee.
- `claude auth login` credentials are account-wide. Anyone who can run this pipeline can spend against your Claude Code plan.

---

## Honest limitations and tradeoffs

This section exists on purpose. It's easy to write a README that only sells the idea; here's what's actually true about the current state of this project.

- **No published benchmark.** Everything in [Evidence this works](#evidence-this-works-not-just-theory) came from a handful of real runs during development — it demonstrates the *mechanism* works (QA can catch real bugs it couldn't otherwise catch, resume genuinely resumes), not that output quality is measured or competitive with anything. Treat claims here as "verified to occur at least once," not "proven at scale."
- **Cost and quota are unpredictable, and can be significant.** Every engineering turn is a full agentic Claude Code session — potentially dozens of tool calls — and rework loops multiply that (one QA_FAIL can re-run three engineers). We hit the account's Claude Code session/usage limit mid-testing during development. Context routing (see [How it works](#how-it-works)) already bounds each turn's replayed prompt to the role's declared inputs. Further mitigations, in order of effort: lower the per-role `max_turns` in `pipeline.py`; set `AITEAM_CODE_MODEL` to a cheaper model (trades away some engineering/QA judgment quality); or, more structurally, note that `product_manager`/`solution_architect`/`uat_reviewer`/`release_reporter` don't use any tools but still each spin up a full `claude` CLI session just to produce one text completion — moving only those four roles to a raw metered API call (e.g. reviving `config.py`) would cut Claude Code session-quota consumption roughly in half without touching the real-execution value the engineering roles provide. See [Possible future work](#possible-future-work).
- **Single-vendor lock-in.** The entire pipeline authenticates via the `claude` CLI specifically, with no fallback wired in. If Claude Code's plan limits, pricing, or CLI behavior change, there's no alternate path today (`config.py` exists as a starting point for one, but it's disconnected).
- **Crash recovery is per-turn, not per-tool-call.** See [Checkpoint & resume](#checkpoint-resume) — a turn interrupted mid-session is retried wholesale on resume, not resumed from its last tool call.
- **The permission guard is a denylist, not a sandbox.** `permission_mode="acceptEdits"` auto-approves file edits and shell commands with no human review — intentional for a "no human in the loop" pipeline (confirmed in testing that this doesn't hang waiting for approval). The `PreToolUse` hook (see [Security & access control](#security-access-control)) covers the specific things we thought to name; it doesn't cover everything — a command not on the denylist, or a sufficiently adversarial rephrasing of one that is, still runs. Real OS-level confinement (`SandboxSettings`) isn't wired in. Treat the workspace as meaningfully less trusted than the rest of this repo regardless.
- **Built on an explicitly experimental orchestration layer.** AutoGen's own docs label `GraphFlow` experimental ("API, behavior, and capabilities are subject to change"), and Microsoft has since put AutoGen into maintenance mode in favor of a newer framework (Microsoft Agent Framework). That's a real platform-risk signal for anyone extending this long-term.
- **The plumbing is new.** Checkpoint/resume, the workspace conventions, the tool-permission model — all written and lightly tested in the course of building this, not hardened by years of bug reports the way an established framework's equivalent machinery would be.

---

## Comparisons & alternatives

### How this compares to similar projects

The closest prior art is **[MetaGPT](https://github.com/FoundationAgents/MetaGPT)** — same role decomposition in spirit (Product Manager → Architect → Project Manager → Engineer → QA Engineer), same philosophy of structured document handoffs over free-form dialogue, and it has published benchmark numbers (85.9%/87.7% Pass@1 on standard coding benchmarks) and years of real usage that AiTeam does not have. It differs in two important ways: it's a bespoke orchestration engine rather than AutoGen, and its execution/testing capability is its own built-in interpreter rather than a full agentic coding tool like Claude Code — meaningfully less capable at the kind of real, autonomous, many-tool-call verification this project's QA role depends on.

ChatDev is a similar-genre "AI software company" simulation with dialogue-driven agent communication — also its own framework, predating the Claude Agent SDK.

We didn't find any existing project combining AutoGen's `GraphFlow` with the Claude Agent SDK in this way — the pairing of deterministic graph-based process control with a full agentic execution backend for the engineering roles appears to be novel, for whatever that's worth given the immaturity noted above.

### Why not just prompt Claude Code directly?

A fair question, worth answering honestly rather than defensively: **we don't have proof this beats a single well-prompted Claude Code session doing the whole thing end to end.** That's a genuine gap, not something to paper over.

The theoretical case for the multi-agent structure:

1. **Forced independent verification.** `qa_engineer` runs in a *separate* session from the engineers, given only the PRD/TECH DESIGN/code — not the engineers' reasoning or confidence about their own work. It has no "I already convinced myself this is right" momentum. A single continuous session reviewing its own output doesn't have that separation — it's the same context reviewing itself, which is exactly the blind spot human teams built dedicated QA roles to avoid in the first place.
2. **A structural checkpoint specifically against goal-drift.** `uat_reviewer` re-checks against the *original one-line goal*, not the PRD — designed to catch "the PRD subtly misread the goal, and everything downstream is a correct implementation of the wrong thing." A single session has no equivalent forced re-grounding step unless someone remembers to ask for one.
3. **Deterministic, bounded rework** — `QA_FAIL` routes only to the owning engineer(s), capped at 3 loops — versus an unstructured session where "keep fixing it" has no natural stopping rule.
4. **A reproducible paper trail** (numbered ACs, a QA traceability table, a UAT walkthrough, a final delivery report) that exists because the structure requires it, not because someone remembered to ask for it.

Where the skeptic is right:

- This costs meaningfully more — we hit a real Claude Code session/usage limit running it (see [Honest limitations](#honest-limitations-and-tradeoffs)) — versus one well-scoped Claude Code session, which already runs its own internal plan → write → test → fix loop without any of this scaffolding.
- For simple or small tasks, this is overhead with no demonstrated payoff. The multi-agent split only earns its cost on complexity where independent review genuinely catches something a single self-reviewing pass would miss — where that threshold actually is has not been measured.
- The whole pipeline still shares one blind spot with "just prompt Claude Code directly": there's no human and no external ground truth anywhere in the loop except the original one-line goal. If every agent shares the same wrong assumption about ambiguous phrasing, nothing here catches that either.
- This is genuinely unproven right now, not just under-marketed. The honest way to settle it is to run the same goal both ways — this pipeline vs. one direct Claude Code session — and compare defect rates and cost, which is exactly the evaluation harness listed under [Possible future work](#possible-future-work).

---

## Possible future work

Cost/quota is the most pressing known limitation, so it leads this list:

- **Split billing by role need.** Move the four tool-less reasoning roles (PM, Architect, UAT, Release Reporter) off the `claude` CLI and onto a raw, metered Anthropic API call — reviving and rewiring `config.py` is most of the work. They never call a tool, so a full Claude Code session is overkill for them; a plain API completion is cheap and removes 4 of the 8 CLI sessions a run currently consumes, without giving up any real-execution verification (that value is entirely in FE/BE/OPS/QA, which would stay on the Claude Agent SDK).
- **Replace the Claude Code CLI dependency entirely for the engineering roles**, with a hand-rolled agentic loop against the raw Anthropic Messages API (bash/file tools via native tool-use) instead of shelling out to `claude`. Removes the hard, clock-based session/usage quota wall in favor of linear pay-per-token billing — a bigger rewrite of `claude_code_agent.py`, and it gives up Claude Code's own tuning as a coding agent, but removes single-vendor CLI lock-in at the same time.
- A cost/turn budget knob exposed via env var rather than hardcoded constants in `pipeline.py`, so tuning cost vs. thoroughness doesn't require a code change.
- A real evaluation harness: run the same N tasks through this pipeline and through an established baseline (e.g. MetaGPT), and compare defect rates, cost, and wall-clock time — the honest next step before making any comparative quality claim.
- Per-tool-call checkpointing (or at least periodic mid-turn snapshots) to reduce how much work a crash mid-turn discards.
