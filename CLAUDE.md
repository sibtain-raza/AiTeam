# AiTeam

Autonomous AI software delivery team on AutoGen AgentChat (0.7.x) `GraphFlow`. Eight agents (PM, Architect, FE/BE/OPS engineers, QA, UAT, Release Reporter) run a deterministic pipeline with QA rework loops (max 3) and a single UAT re-scope loop. The authoritative behavior spec is `SPEC.md` — keep prompts and routing consistent with it. The Release Reporter exists partly for a structural reason: GraphFlow validation requires at least one leaf node, and the run completes naturally when it executes.

## Commands

```sh
.venv/bin/pip install -r requirements.txt          # deps
PYTHONPATH=src .venv/bin/python -m aiteam.main "<goal>"   # run pipeline
PYTHONPATH=src .venv/bin/python -m aiteam.main --resume output/checkpoints/<stamp>.json   # resume a failed/interrupted run
```

All eight agents run via `claude-agent-sdk`, which shells out to the `claude` CLI (Claude Code) and uses **its own OAuth login** — no `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` needed. Requires the `claude` CLI installed and authenticated on the host (`claude auth status`). Optional `AITEAM_CODE_MODEL` overrides the model all agents use (default: Claude Code's own default).

`src/aiteam/config.py` (`make_model_client`, `AITEAM_PROVIDER`) is unused dead code, kept intentionally in case the pipeline is ever wired back to a raw-API-key provider (`AnthropicChatCompletionClient`/`OpenAIChatCompletionClient` via `autogen-ext`) instead of the `claude` CLI. It is not called from `pipeline.py` or `main.py` — don't assume it's live.

## Architecture

- `src/aiteam/pipeline.py` — the graph. Key invariant: nodes with multiple incoming edges (engineers: architect + QA-rework; PM: entry + UAT-reject) must keep both edges in a shared `activation_group` with `activation_condition="any"`, or the first pass deadlocks (GraphFlow defaults to "all").
- Routing is by verdict tokens (`QA_PASS`/`QA_FAIL`, `UAT_APPROVED`/`UAT_REJECTED`) matched against the **last non-empty line** of the agent's message (`verdict_is` / `last_line`) — never substring-match, since prompts/templates can mention both tokens.
- `src/aiteam/termination.py` — `TokenCountTermination` hard-stops after 3 `QA_FAIL`s with a `PIPELINE_FAILED` stop reason.
- `src/aiteam/prompts.py` — verbatim from SPEC.md section 3 (including the "token must appear nowhere else" hardening on QA/UAT). Edit SPEC.md and prompts.py together; they must not drift.
- `src/aiteam/claude_code_agent.py` — `ClaudeCodeAgent`, a `BaseChatAgent` that runs its role as a real `claude_agent_sdk` session (`permission_mode="acceptEdits"`) instead of a plain model-client completion. Every agent is one of these. PM, Architect, UAT, and Release Reporter get `allowed_tools=[]` (pure text reasoning, no filesystem access); FE, BE, OPS, and QA get real file/bash tools (Read/Write/Edit/Glob/Grep/Bash) scoped to a workspace directory, so they actually write files and QA can run real tests/lint/build against them. Each `query()` call is a fresh Claude Code session with no memory of prior turns, so `ClaudeCodeAgent` replays the accumulated upstream chat history as the prompt every turn (mirroring `AssistantAgent`'s internal `model_context`) — but code from earlier turns persists for the tool-using agents because it's really on disk in the workspace.
- Workspace layout, built per run in `main.py` under `<output_dir>/workspace/<timestamp>/` and passed into `build_team()`: `frontend/` (FE's cwd), `backend/` (BE's cwd), and the root (OPS's and QA's cwd, so OPS can reference `./frontend` and `./backend` and QA can read/test everything; also the cwd for the tool-less reasoning agents, who never use it). This convention is encoded into the FE/BE/OPS/QA prompts in `prompts.py`/`SPEC.md` — the path names and pipeline.py's directory assignment must stay in sync.
- **Checkpoint/resume** (`main.py`): after every completed agent turn, `main.py` calls `GraphFlow.save_state()` and overwrites `<output_dir>/checkpoints/<stamp>.json` (goal, workspace path, full team state). On any exception (Claude Code session limit hit, crash, etc.) the run stops with that path printed — rerun with `--resume <path>` to continue from the last completed turn instead of restarting at PM. Verified: a freshly-constructed team (new `GraphFlow`, new agent instances) correctly resumes at the next graph node after `load_state()`, without re-running completed ones. This relies on two things staying correct together: `GraphFlow.save_state()`/`load_state()` (built into AutoGen, captures graph position + each participant's pending message buffer) and `ClaudeCodeAgent.save_state()`/`load_state()` (added here — `BaseChatAgent`'s default is a stateless no-op, so without this override the agent's replayed `_history` would not survive a resume even though the graph position would). Resuming calls `run_stream(task=None)` — per AutoGen's own contract, this continues the previous task rather than starting a new one.

## Conventions

- Verify AutoGen API signatures against the installed version before changing orchestration code — the GraphFlow API is experimental and shifts between minor releases.
- No secrets in code; provider/model selection only via env vars (see `.env.example`).
- `ClaudeCodeAgent` gives FE/BE/OPS/QA real Bash access with `acceptEdits` (auto-approved, no human in the loop) — Bash is not sandboxed to `cwd` beyond it being the process's starting directory. Treat the workspace as untrusted-generated-code territory, not as safe as the rest of the repo.
