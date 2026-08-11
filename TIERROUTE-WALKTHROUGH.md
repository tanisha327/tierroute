# TierRoute — walkthrough

What it is, how to use it, and how to read its output.

## The idea

You give TierRoute a task. It splits the task into steps, runs each step on the
cheapest model that can handle it, reviews its own work, stops before it overspends,
reuses anything it has already done, and never lets the agent touch your access
token. At the end it reports what you saved.

It runs on your existing `claude` login. No API key.

## 1. Hand over the token, safely

Tasks that touch GitLab need a token. Handing that token to the agent is how secrets
leak, so TierRoute doesn't.

Paste it once and click **Start broker**. The token then lives only in the broker's
memory — never on disk, never in your shell, never visible to the agent. When a step
needs `git` or `glab`, the broker injects the token for that one command, blocks
anything dangerous, and logs the call. The green dot means the broker is holding it.

## 2. Describe the task

Type it in plain English — *"prepare a changes.md summarizing the current local
changes"*. Set **which folder** to work in, and whether it may **edit files and run
tests**. Leave that off for a read-only preview.

The **Director** then writes a plan: it splits the task into a few steps and judges
how hard each one is. Then it works through them.

## 3. Right-sized model per step

This is where the savings come from.

Reading a file is easy; redesigning an algorithm is not. TierRoute sorts steps into
three levels and uses the cheapest model that fits: **Haiku** for easy work,
**Sonnet** for normal coding, **Opus** only where real reasoning is needed.

The UI shows three model choices:

- **Baseline** — the model you'd otherwise use for everything. It doesn't actually
  run; it's the yardstick savings are measured against.
- **Planner** and **Reviewer** — which models build the plan and check the result.
  Set these cheaper too.

## 4. The cost firewall

- **Max $** and **Max turns** — hard limits. Hit either and the run stops. This is
  the guarantee against a surprise bill.
- **Reasoning $** — a soft limit. Cross it and TierRoute stops using the expensive
  model, finishing the job on cheaper ones. The task still completes.
- **Loop threshold** — if the agent repeats the same action over and over, that step
  is killed before it wastes money.

## 5. Review and retry

Finishing isn't the same as finishing correctly. A reviewer model asks: is this
complete and correct?

If yes, you're done. If not, it lists the problems, the Director fixes exactly those,
and the reviewer looks again — up to **Max rounds**. **Min confidence** sets the bar:
below it, or with any issue outstanding, the work isn't accepted and improvement
continues. Nobody has to babysit it.

## 6. The cache

Work already done isn't paid for twice — the saved result comes back instantly, at
zero tokens and zero dollars. Re-running a task, or a retry loop that repeats a step,
is close to free.

You can turn the cache off, clear it when your code has changed, tune how similar a
request must be to count as a match, and expire old entries.

---

## Reading the output

**Planning:**

```
[director] ===== round 1/3 =====
[director] planning on claude-sonnet-4-6 (mode=cli) ...
[director] plan ready: 2 step(s), planner $0.0841
```

Attempt 1 — a second round only happens if the review fails. The planner ran on
Sonnet via your local `claude` and produced 2 steps for about 8 cents. A cached plan
shows `plan CACHE HIT — saved $…` and costs nothing.

**Steps:**

```
[director] step 1/2: [cheap] Read the git diff  -> claude-haiku-4-5   (turn 2, spent $0.08)
[director]   done: $0.1200  (session spent $0.20)
```

An easy step, so it ran on Haiku. `turn 2` is the second model call (the planner was
first). `spent $0.08` is the running total the Max $ cap watches. A cached step shows
`💾 CACHE HIT … (saved $…)` at $0.

**Guardrails**, when they fire: a note that the reasoning budget was crossed and the
premium tier is locked, `🛑 HARD CAP` when a limit stops the run, or
`🛑 CIRCUIT BREAKER` when a stuck loop is killed.

**Review:**

```
[director] reviewing on claude-sonnet-4-6 ...
[director]   review: complete=True confidence=0.9 issues=0 -> accepted=True
[director] task verified complete — done.
```

Accepted. Otherwise it lists the issues and starts another round.

A stray `.` is a heartbeat during a long step. `[ui] run finished` means it wrapped up
cleanly.

## The final report

| Block | What it tells you |
|---|---|
| **MACRO-PLAN** | Task, baseline model, rounds used, whether it completed. Then each step: model used, what it cost, what it would have cost on the baseline, and what it did. |
| **REVIEW** | Complete or not, confidence, issues flagged, short summary. |
| **CACHE** | Steps reused vs done fresh, entries now stored, tokens and dollars saved. Zero on a first run; re-run the task and they light up. |
| **COST** | The headline. Baseline cost, actual spend (tiered steps plus planner and review), then savings: the gross win from tiering minus overhead, giving net savings and percentage. |
