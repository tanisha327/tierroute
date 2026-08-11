# TierRoute — Documentation

A local toolkit (Python stdlib only) that makes AI coding agents cheaper and safer:

1. **Zero-Trust guard + credential broker** — the agent uses `glab`/`git` without ever
   seeing your token; a deny-by-default policy blocks dangerous commands.
2. **Model Director** — plans a task once, runs each step on the cheapest capable
   model, enforces a circuit breaker, reviews and retries, and caches results.
3. **Web UI** — drives both from the browser, starting the broker in-process.

The Director runs on the local `claude` CLI, so no Anthropic API key is needed. Put
`claude` on your `PATH` or set `GATEWAY_CLAUDE_CMD` to your launch command.

---

## Contents
- [Architecture](#architecture)
- [1 — Zero-Trust guard](#1--zero-trust-guard)
- [2 — Credential broker](#2--credential-broker)
- [3 — Model Director](#3--model-director)
  [Planner](#planner) · [Tiering](#tiering) · [Circuit breaker](#circuit-breaker) ·
  [Review loop](#review-and-retry) · [Cache](#response-cache)
- [4 — Web UI](#4--web-ui)
- [Shims](#shims-bin) · [Flags](#flag-reference) · [Config and env vars](#config-and-env-vars)
- [Running and testing](#running-and-testing)
- [Decisions and caveats](#decisions-and-caveats)
- [File map](#file-map)

---

## Architecture

```
                  ┌───────────────────── Web UI (scripts/ui.py) ─────────────────────┐
  browser ──────► │  token→broker (in-proc)    prompt+options→Run/Stop   live output  │
                  └──────────────┬──────────────────────────────┬───────────────────┘
                                 │ starts                       │ spawns
                                 ▼                              ▼
                   Credential broker (:8765)          Director (orchestrator/)
                   holds the token in RAM             plan → tier → breaker → review → cache
                                 ▲                              │  planner, each step, and review
                     glab/git via the guard                     │  are all `claude -p` calls
                                 │                              │
                   glab-gw / git-gw ◄───────────────────────────┘ (the Director's GitLab steps)
```

Two separate things use the broker: your own `glab-gw`/`git-gw` calls, and the
Director's GitLab steps. The Director's model calls go to the local `claude` CLI.

---

## 1 — Zero-Trust guard

**Goal:** let an agent run `glab`/`git` without holding the token, and block
destructive commands.

**Files:** `gateway/toolguard.py`, `gateway/config.py`, `scripts/glab_gw.py`,
`scripts/git_gw.py`, `bin/glab-gw*`, `bin/git-gw*`.

Every command goes through four steps:

1. **Policy check** — the command is parsed; anything blocked exits `3` and never runs.
2. **Just-in-time token injection**
   - `glab` → `GITLAB_TOKEN` in the subprocess environment only
   - `git` → an `Authorization: Basic base64(oauth2:PAT)` header for that one call
3. **Scrub** — the token is replaced with `glpat-***REDACTED***` in all output.
4. **Audit** — the command and decision go to `data/tool_audit.log`. Never the token.

**Policy** — edit `config.json`, copying `config.example.json`:

- glab allowed: `mr list/view/diff`, `mr create/update`, `issue list/view`,
  `repo view/clone`, `ci *`, `release list/view`, `api` 
- glab denied: `mr merge/delete/close`, `repo delete/archive`, issue writes,
  `release create/delete`, `auth login/logout`
- git: `push`/`pull`/`fetch`/`clone` allowed; `push --force`/`-f`/`--mirror`/`--delete`
  denied, as are the flagless forms — a `+` refspec forces and `:branch` deletes

**Token sources:** `$GATEWAY_GITLAB_TOKEN` in the current process, or — if
`GATEWAY_BROKER` is set — the broker, in which case this process never holds it.

---

## 2 — Credential broker

**File:** `scripts/broker.py`.

A localhost HTTP service that keeps the token in **its own process memory** and
exposes only `POST /run` (policy-checked through `toolguard.dispatch`) and `/health`.
No endpoint returns the token. Guards reach it via `GATEWAY_BROKER`.

```bash
python scripts/broker.py                    # prompt; token in memory only
python scripts/broker.py --token-env NAME   # read from an env var
python scripts/broker.py --port 8765
```

The UI starts it in-process via `broker.start_broker(token)`, so no second terminal.

**Why it matters:** with the token only in the broker — ideally running as a separate
OS user — a prompt-injected agent can't read or exfiltrate it. It can only ask the
broker to run allowed commands.

**Not reachable from a web page.** Binding to 127.0.0.1 isn't privacy: any site the
user has open can POST to a local port, because a `fetch` with a string body is a
"simple" request that needs no CORS preflight. The attacker can't read the reply, but
with the broker the side effect *is* the attack — it would spend your PAT. So `/run`
requires three things (`gateway/webguard.py`):

| Check | Stops |
|---|---|
| `X-TierRoute-Client` header | a simple cross-origin POST; adding the header forces a preflight, and `OPTIONS` answers 405 with no `Allow-*` |
| no `Origin` header | anything browser-driven; curl and the guard shims never send one |
| loopback `Host` | DNS rebinding, where a page resolves its own name to 127.0.0.1 |

This is not user authentication. A same-user process can still call the broker — that
is the documented boundary; see README's "How strong is this?".

---

## 3 — Model Director

**Files:** `orchestrator/` — `planner.py`, `director.py`, `models.py`, `backend.py`,
`tools.py`, `reviewer.py`, `cache.py`, `run.py`.

Plans a task once, executes it step by step on the cheapest capable model, checks the
result, and reports cost against a single-premium-model baseline.

### Planner

One call returns a JSON plan: a list of steps, each with a tier
(`cheap`/`standard`/`premium`), a goal, and suggested tools.

### Tiering

`models.py` maps tiers to models — cheap=Haiku, standard=Sonnet, premium=Opus — plus a
price table. Each step runs on its tier's model and receives **pruned context**: the
task, its own goal, and short summaries of earlier steps, never the whole transcript.

Cost is reported as **baseline** (every step on `--default-model`) versus **actual**
(tiered), with net savings after planner and review overhead.

**Backends** (`backend.py`):

| Backend | Behaviour |
|---|---|
| `cli` (default) | each step is a headless `claude -p --model <tier>` call, reusing the local CLI's auth. Claude Code does the real tool work. |
| `mock` | canned responses; exercises the flow with no model calls. |
| `api` | Anthropic Messages API. Needs a key; the Director runs its own tool loop. |

### Circuit breaker

Three independent layers:

1. **Loop detection** — if the agent calls the same tool with identical arguments more
   than `--loop-threshold` times in a row, the step's process is killed and the run
   halts. In `cli` mode this watches the `stream-json` event stream live.
2. **Hard cap** — `--max-cost` and `--max-turns`, checked before each step and round.
   Crossing either halts the run.
3. **Tier downgrade** — past `--reasoning-budget`, the premium tier is locked out and
   premium steps run cheap. The run continues, just cheaper.

A *turn* is one model call — planner, step, or review — summed across rounds.

### Review and retry

A reviewer model then verifies the work, reading the actual files read-only. The task
is accepted **only if** `complete` is true, there are no `issues`, **and** confidence
is at least `--min-confidence`. Otherwise the issues become a remediation task and the
Director replans and fixes, up to `--max-rounds`.

### Response cache

`cache.py` stores the planner result and each step result in
`data/director_cache.json`, keyed by request:

- **Tier 1, exact** — SHA-256 of (kind + model + request text).
- **Tier 2, semantic** — near-identical request text via a small local embedder,
  cosine ≥ `--cache-threshold`.

A hit skips the `claude -p` spawn entirely — 0 tokens, $0 — and logs
`💾 CACHE HIT — saved $X`. The report shows hit rate, tokens saved, and dollars saved.
Most useful on re-runs, the fix loop, and resuming an interrupted run.

Cached results can be **stale** if the repo changed since they were stored. Use
`--clear-cache` or `--cache-ttl`.

---

## 4 — Web UI

**File:** `scripts/ui.py`, on stdlib `http.server`. Launch with the `ui` shim; it opens
`http://127.0.0.1:8600`.

1. **Credential broker** — paste the token, click Start broker. Green dot = running.
2. **Task** — prompt, workdir, "allow edits + run tests (-x)", Run / Stop.
3. **Options**, collapsible, mirroring every flag:
   - Models — baseline, planner, review
   - Circuit breaker — max $, max turns, reasoning $, loop threshold
   - Review and retry — on/off, max rounds, min confidence
   - Cache — on/off, clear first, semantic threshold, TTL
4. **Output** — the live Director log. Stop kills the whole process tree.

The UI runs the Director as a subprocess with `GATEWAY_BROKER` set and streams its
combined stdout and stderr to the browser.

**Locked to the tab it served.** A run can enable edits and shell in any directory the
caller names, so an unprotected `POST /run` would hand code execution to any site the
user had open (same reasoning as the broker, above). Every POST must therefore carry
`X-TierRoute-Token` — minted fresh each launch, embedded in the page, and unreadable
cross-origin — come from this server's own origin, and address a loopback `Host`.
Scripting the UI from the shell means reading that token out of the page first; for
automation prefer `python -m orchestrator.run`, which is the same code path.

---

## Shims (`bin/`)

Add `bin/` to your `PATH` once, replacing `<REPO>` with your clone path:

```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path","User") + ";<REPO>\bin", "User")
```

| Shim | Runs |
|---|---|
| `glab-gw` | guarded `glab` → `scripts/glab_gw.py` |
| `git-gw` | guarded `git` → `scripts/git_gw.py` |
| `director` | the Director |
| `ddir` | alias for `director` |
| `ui` | the web UI |

The Director shims need `claude` on `PATH`, or `GATEWAY_CLAUDE_CMD` set.

---

## Flag reference

`director` / `ddir` / `python -m orchestrator.run`:

| Flag | Default | Purpose |
|---|---|---|
| `task` (positional) | — | the task to plan and execute; quote it |
| `--workdir DIR` | `$GATEWAY_DIRECTOR_WORKDIR` | target repo |
| `--default-model` | `claude-opus-4-8` | the cost **baseline** model |
| `--planner-model` | = default | model for planning; set cheaper to save |
| `--review-model` | = default | model for review; set cheaper to save |
| `--backend {cli,mock,api}` | `cli` | execution backend |
| `--mock` | off | shorthand for `--backend mock` |
| `--allow-writes` / `--allow-shell` | off | permit file edits / running commands |
| `-x`, `--go` | off | shorthand for both allow flags |
| `--review` / `--no-review` | on | final review pass |
| `--max-rounds N` | 3 | max verify→fix rounds |
| `--min-steps N` | 2 | minimum steps the planner must produce |
| `--min-confidence F` | 0.7 | review must reach this, with no issues, to accept |
| `--loop-threshold N` | 3 | trip if the same tool and args repeat more than N times |
| `--max-cost $N` | 10 | hard cap: halt at $N (0 = off) |
| `--max-turns N` | 50 | hard cap: halt after N turns (0 = off) |
| `--reasoning-budget $N` | 0 | past $N, stop using the premium tier (0 = off) |
| `--cache` / `--no-cache` | on | response cache |
| `--clear-cache` | off | wipe the cache before running |
| `--cache-threshold F` | 0.97 | semantic cutoff; > 1 means exact matches only |
| `--cache-ttl S` | 0 | ignore cached entries older than S seconds (0 = never) |
| `--call-timeout S` | 1800 | per-call timeout, to catch a hung call |
| `--selftest` | — | prove the breaker with a synthetic looping stream |
| `--json` | — | print the full report as JSON |

---

## Config and env vars

**`config.json`** — optional, repo root, copied from `config.example.json`. Holds
tool-guard settings only: `tools.gitlab` (binary, host, policy) and `tools.git`. It is
merged over the defaults in `gateway/config.py`, is found relative to the repo root
whatever your working directory, and is gitignored. Without it you get host
`gitlab.com` and `glab` from `PATH`.

| Var | Used by | Meaning |
|---|---|---|
| `GATEWAY_GITLAB_TOKEN` | guards | the PAT, in env-var mode. Prefer the broker. |
| `GATEWAY_BROKER` | guards | broker URL, e.g. `http://127.0.0.1:8765` |
| `GATEWAY_CONFIG` | guards | load config from a path other than `config.json` |
| `GATEWAY_DIRECTOR_WORKDIR` | Director | default `--workdir` |
| `GATEWAY_CLAUDE_CMD` | Director | override how `claude` is launched |
| `GATEWAY_MODEL_CHEAP` / `_STANDARD` / `_PREMIUM` | Director | override a tier's model |
| `GATEWAY_IN_DIRECTOR` | set automatically | anti-recursion marker for step subprocesses |

---

## Running and testing

**Guard** — no token needed for policy checks:

```bash
python scripts/glab_gw.py --gw-dry-run mr list     # ALLOWED, nothing run
python scripts/glab_gw.py repo delete a/b          # DENIED, exit 3
python scripts/git_gw.py  push origin x --force    # DENIED, exit 3
python scripts/git_gw.py  push origin +x:x         # DENIED, exit 3 (flagless force)
cat data/tool_audit.log                            # audit trail, no token
```

**Broker** — live GitLab:

```bash
python scripts/broker.py              # paste the token; leave running
curl http://127.0.0.1:8765/health     # {"status":"ok","token_loaded":true}

# in another shell:
export GATEWAY_BROKER=http://127.0.0.1:8765
glab-gw mr list                       # reaches GitLab; token only in the broker
```

**Director** — mock, needing neither a key nor `claude`:

```bash
python -m orchestrator.run "fix a bug and open an MR" --mock
```

**Director** — live, on the local CLI:

```bash
ddir "prepare a changes.md summarizing local changes" --workdir /path/to/repo -x \
     --planner-model claude-sonnet-4-6 --review-model claude-sonnet-4-6 \
     --max-cost 5 --max-turns 20 --reasoning-budget 3
```

**Circuit breaker** — no repo or model needed:

```bash
director --selftest --loop-threshold 3                                     # trips on the 4th call
python -m orchestrator.run "x" --mock --no-review --max-cost 0.10          # HARD CAP halt
python -m orchestrator.run "x" --mock --no-review --reasoning-budget 0.05  # tier downgrade
```

**Cache:**

```bash
python -m orchestrator.run "same task" --mock --clear-cache   # run 1: misses, stores
python -m orchestrator.run "same task" --mock                 # run 2: all hits, $0
```

**Tests:** `python -m pytest tests` (or `python -m unittest discover -s tests -t .`).

---

## Decisions and caveats

- **No API key, so no prompt caching.** The Director uses the `claude` CLI, where
  Anthropic prompt caching across steps isn't available — and per-step tiering is
  incompatible with it anyway, since the cache is per-model. What's here is *response*
  caching: skip the repeat entirely.
- **Each `claude -p` spawn has fixed overhead.** It reloads Claude Code's system
  prompt and `CLAUDE.md` as fresh cache-creation tokens. On Opus that dominates cost —
  run planner and review on Sonnet, and keep `CLAUDE.md` short.
- **Models won't loop on command,** so a live loop can't be staged for the breaker
  demo. `--selftest` drives the real monitor with a synthetic looping stream instead.
- **Cached results can be stale** if the repo changed. `--clear-cache` for a fresh run.
- **`GATEWAY_CLAUDE_CMD` pointing at a wrapper is fragile:** banner or log lines on
  stdout corrupt claude's JSON, and the wrapper may resolve the wrong binary. Prefer
  `claude` directly on `PATH`.
- **Same-user broker isn't airtight.** A determined agent running as your user could
  scrape the token from the broker's memory. Real isolation needs the broker as a
  separate OS user — see the README.
- **Windows/Git Bash:** arguments starting with `/` get path-mangled, so use
  `MSYS_NO_PATHCONV=1`; `.cmd` launchers run via `cmd /c`; UTF-8 is forced on output to
  avoid cp1252 crashes.

---

## File map

```
tierroute/
├── config.example.json              # copy to config.json to override guard settings
├── README.md  DOCUMENTATION.md  CLAUDE.md  CACHING-STRATEGY.md
├── gateway/
│   ├── config.py        # config loader; defaults merged with config.json
│   ├── toolguard.py     # policy, injection, scrubbing, audit — glab and git
│   └── webguard.py      # CSRF / rebinding checks for the localhost broker and UI
├── orchestrator/
│   ├── run.py           # CLI entry point, all flags
│   ├── director.py      # plan → tier → breaker → review → cache
│   ├── planner.py       # one-shot planner, returns tiered plan JSON
│   ├── models.py        # tiers, model ids, prices
│   ├── backend.py       # cli / mock / api; stream monitor; selftest
│   ├── tools.py         # api-mode tool executors: read, write, shell, glab, git
│   ├── reviewer.py      # final review verdict
│   └── cache.py         # response cache, exact + semantic, persisted
├── scripts/
│   ├── glab_gw.py  git_gw.py   # guard entry points
│   ├── broker.py               # credential broker
│   ├── director.py             # Director launcher, path-independent
│   └── ui.py                   # web UI server
├── bin/                        # shims: glab-gw, git-gw, director, ddir, ui (+ .cmd)
├── tests/                      # test_models.py (tiers, pricing), test_guard.py (policy, webguard)
└── data/                       # runtime: tool_audit.log, director_cache.json (gitignored)
```
