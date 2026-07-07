# AiTeam

An autonomous AI software delivery team built on [AutoGen AgentChat](https://microsoft.github.io/autogen/) `GraphFlow`. Give it a goal; an eight-agent pipeline grooms it into a PRD, designs the system, implements frontend/backend/ops in parallel, QA-verifies against acceptance criteria (with up to 3 rework loops), UAT-validates against the original goal, and emits a final delivery report. Full spec in [SPEC.md](SPEC.md).

```
PM (groom) → Architect → FE ∥ BE ∥ OPS → QA ──QA_FAIL (≤3)──► engineers
                                          └─QA_PASS──► PM (UAT) ──UAT_REJECTED──► PM (groom, once)
                                                                └─UAT_APPROVED──► Release Reporter → done
```

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your API key
```

## Run

```sh
export ANTHROPIC_API_KEY=sk-ant-...          # or OPENAI_API_KEY with AITEAM_PROVIDER=openai
export PYTHONPATH=src
.venv/bin/python -m aiteam.main "Build a URL shortener with custom aliases and click analytics."
```

The run streams to the console and writes a full transcript to `output/run-<timestamp>.md`.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AITEAM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `AITEAM_MODEL` | provider default | Model ID override |
| `AITEAM_OUTPUT_DIR` | `output` | Transcript directory |

## Layout

- `src/aiteam/prompts.py` — global rules + the 8 role system prompts
- `src/aiteam/pipeline.py` — GraphFlow graph (fan-out/fan-in, conditional rework edges)
- `src/aiteam/termination.py` — QA-fail counter (hard stop with `PIPELINE_FAILED` on the 3rd fail)
- `src/aiteam/config.py` — provider-configurable model client
- `src/aiteam/main.py` — CLI entry point
