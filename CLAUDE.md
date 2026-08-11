# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Overview

**TierRoute** is a local, dependency-free toolkit that cuts the cost and risk of
running AI coding agents. Three parts:

- **Zero-Trust tool guard** (`gateway/`, `scripts/glab_gw.py`, `scripts/git_gw.py`,
  `scripts/broker.py`) — lets an agent use `glab`/`git` without ever seeing the access
  token. The token is injected into the subprocess just in time, dangerous commands are
  denied by policy, output is scrubbed, and every call is audited.
- **Model Director** (`orchestrator/`) — plans a multi-step task once, runs each step
  on the cheapest capable model tier, applies a circuit breaker (loop, cost and turn
  caps), reviews the result, and reports the cost saved. Uses the local `claude` CLI,
  so no API key is needed.
- **Web UI** (`scripts/ui.py`) — a browser front end for the broker and the Director.

Everything is **pure Python 3.10+ standard library**.

## Layout

```
gateway/        # zero-trust guard
  config.py     #   defaults, merged with config.json
  toolguard.py  #   policy, injection, scrubbing, audit
  webguard.py   #   keeps the localhost broker/UI unreachable from a web page
orchestrator/   # the Model Director
  run.py        #   CLI entry point (see --help)
  director.py   #   plan -> execute steps -> review/retry
  planner.py    #   one-shot planner
  backend.py    #   cli / mock / api backends
  models.py     #   tiers and prices (env-overridable)
  cache.py      #   response cache (exact + semantic)
  reviewer.py   #   final review verdict
scripts/        # launchers: director.py, glab_gw.py, git_gw.py, broker.py, ui.py
bin/            # PATH shims: glab-gw, git-gw, director, ddir, ui
config.example.json  # copy to config.json to override host/binary/policy
```

## Common commands

```bash
# Investigate only — reads allowed, no edits or shell:
python scripts/director.py "<task>" --workdir /path/to/repo

# Implement — allow edits and running tests:
python scripts/director.py "<task>" --workdir /path/to/repo --allow-writes --allow-shell

# All flags:
python -m orchestrator.run --help

# Prove the circuit breaker trips (no models needed):
python -m orchestrator.run --selftest --loop-threshold 3

# Web UI on http://127.0.0.1:8600:
python scripts/ui.py

# Tests:
python -m pytest tests
```

The Director launches the local `claude` CLI. Keep `claude` on `PATH`, or set
`GATEWAY_CLAUDE_CMD` to your launch command.

## GitLab and git access

Run `glab`/`git` through the guard so the token is never exposed:

```bash
glab-gw mr list                  # or: python scripts/glab_gw.py mr list
git-gw  push origin my-branch
```

- The token comes from `$GATEWAY_GITLAB_TOKEN` or a running broker, and is injected
  server-side only — never printed, logged, or placed in the agent's environment.
- Destructive commands are denied by policy (`mr merge/delete`, `repo delete`,
  `git push --force/--mirror`, plus the flagless `push origin +main:main` and
  `push origin :branch`) and exit `3`.
- No `config.json` ships with the repo; the defaults are `gitlab.com` and `glab` from
  `PATH`. Copy `config.example.json` to set your own host, binary, or policy.

See `README.md` for setup and broker mode, and `DOCUMENTATION.md` /
`orchestrator/README.md` for internals.
