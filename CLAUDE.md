# AiTeam

Autonomous AI software delivery team on AutoGen AgentChat (0.7.x) `GraphFlow`. Eight agents (PM, Architect, FE/BE/OPS engineers, QA, UAT, Release Reporter) run a deterministic pipeline with QA rework loops (max 3) and a single UAT re-scope loop. The authoritative behavior spec is `SPEC.md` — keep prompts and routing consistent with it. The Release Reporter exists partly for a structural reason: GraphFlow validation requires at least one leaf node, and the run completes naturally when it executes.

## Commands

```sh
.venv/bin/pip install -r requirements.txt          # deps
PYTHONPATH=src .venv/bin/python -m aiteam.main "<goal>"   # run pipeline
```

Requires `ANTHROPIC_API_KEY` (default provider) or `OPENAI_API_KEY` + `AITEAM_PROVIDER=openai`.

## Architecture

- `src/aiteam/pipeline.py` — the graph. Key invariant: nodes with multiple incoming edges (engineers: architect + QA-rework; PM: entry + UAT-reject) must keep both edges in a shared `activation_group` with `activation_condition="any"`, or the first pass deadlocks (GraphFlow defaults to "all").
- Routing is by verdict tokens (`QA_PASS`/`QA_FAIL`, `UAT_APPROVED`/`UAT_REJECTED`) matched against the **last non-empty line** of the agent's message (`verdict_is` / `last_line`) — never substring-match, since prompts/templates can mention both tokens.
- `src/aiteam/termination.py` — `TokenCountTermination` hard-stops after 3 `QA_FAIL`s with a `PIPELINE_FAILED` stop reason.
- `src/aiteam/prompts.py` — verbatim from SPEC.md section 3, plus the "token must appear nowhere else" hardening on QA/UAT.

## Conventions

- Verify AutoGen API signatures against the installed version before changing orchestration code — the GraphFlow API is experimental and shifts between minor releases.
- No secrets in code; provider/model selection only via env vars (see `.env.example`).
