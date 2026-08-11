# Model Director

Call a model **once** to turn a task into a tiered, step-by-step plan. Then step
through it, running each step on the **cheapest model that can do it**, giving each
step only the context it needs, and tracking **actual cost against the baseline**.

Stdlib only. Runs on the local `claude` CLI (no API key), a mock backend, or the
Anthropic API.

## How it works

```
task ──► planner (1 call) ──► plan JSON, each step tagged cheap/standard/premium
                                │
              ┌─────────────────┘
              ▼   for each step:
   pick the model for its tier → build pruned context (task + goal + prior
   summaries, not full history) → run the tool loop → record cost
              │
              ▼
   baseline (every step on --default-model)  vs  actual (tiered + overhead)
```

- **Baseline** — `--default-model` applied to every step. Fixed at the start, so
  savings are measured against a number that can't drift.
- **Per-step swap** — each step runs on the tier the planner assigned it.
- **Context pruning** — a step sees the task, its own goal, and short summaries of
  earlier steps. Never the whole growing transcript.
- **Net savings** — baseline minus (tiered steps + planner + review). The one-time
  planner and review calls are subtracted, not hidden.

## Tiers — `models.py`

| Tier | Model | Used for |
|---|---|---|
| cheap | claude-haiku-4-5 | reading, extraction, docs and summaries |
| standard | claude-sonnet-4-6 | ordinary implementation, running tests |
| premium | claude-opus-4-8 | root-cause analysis, algorithmic design |

Override any tier with `GATEWAY_MODEL_CHEAP` / `_STANDARD` / `_PREMIUM` if your
deployment doesn't have that model.

## Tools — `tools.py`

| Tool | Safety |
|---|---|
| `read_file` | always allowed |
| `write_file` | dry-run unless `--allow-writes` |
| `run_powershell` | dry-run unless `--allow-shell` |
| `glab` / `git` | through the zero-trust guard: policy-limited, token never exposed |

Dry-run is the default because this may run against a live repo. Nothing is written
or executed until you opt in.

## Run

```bash
# Test the whole flow with no models at all:
python -m orchestrator.run "fix the auth timeout and open an MR" --mock

# Real run on the local claude CLI — reads allowed, writes/shell still dry-run:
python -m orchestrator.run "fix the auth timeout and open an MR"

# Let it edit files and run commands:
python -m orchestrator.run "..." --allow-writes --allow-shell

# Machine-readable report:
python -m orchestrator.run "..." --mock --json
```

The default backend is `cli`, which uses your local `claude` login. Pass
`--backend api` to use the Anthropic API instead; that needs `ANTHROPIC_API_KEY`.

Every flag: `python -m orchestrator.run --help`.

## Notes

- In `api` and `mock` modes the planner is forced through a `submit_plan` tool call,
  so the plan is always valid JSON.
- `glab`/`git` steps go through the same guard and broker as the rest of the project,
  so the agent never sees the token and can't run denied commands.
- Costs come from the price table in `models.py` — update it when prices change.
