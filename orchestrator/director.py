"""The Director: run a tiered plan step by step, swapping models per step, pruning
context, executing real tools, and tracking actual cost against the baseline.

Backends:
  - cli  : one headless `claude -p --model <tier>` per step; Claude Code does the
           real work (files, tests, glab/git via the guard). No API key needed.
  - mock : deterministic, no model calls — for testing the flow.
  - api  : Anthropic Messages API; the Director runs its own tool loop.
"""

import json
import sys

from . import backend, cache as cache_mod, models, planner, reviewer, tools


def _log(msg):
    """Live progress to stderr, flushed so it streams even when redirected."""
    print(msg, file=sys.stderr, flush=True)


STEP_SYSTEM = (
    "You are executing ONE step of a larger, pre-made plan. Do ONLY this step's goal, "
    "using your tools as needed, then reply with a concise result summary the next step "
    "can rely on. Do NOT create a new plan and do NOT invoke the 'director' — you are "
    "already inside a Director step. For any GitLab access use the glab-gw / git-gw guards."
)
MAX_TOOL_ITERS = 6


def _add_usage(acc, usage):
    acc["input_tokens"] += (usage or {}).get("input_tokens", 0) or 0
    acc["output_tokens"] += (usage or {}).get("output_tokens", 0) or 0


def _text_of(resp):
    return "\n".join(
        b.get("text", "")
        for b in resp.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def _step_prompt(task, step, prior):
    """Context pruning: the task, this step's goal, and short prior summaries only —
    never the whole transcript."""
    lines = [
        f"Overall task: {task}",
        "",
        f"Current step: {step['title']}",
        f"Goal: {step['goal']}",
    ]
    if step.get("tools"):
        lines.append(f"Suggested tools: {', '.join(step['tools'])}")
    if prior:
        lines.append("\nContext from earlier steps (summaries only):")
        for p in prior:
            lines.append(f"- [{p['title']}]: {p['result'][:400]}")
    return "\n".join(lines)


def _run_step(mode, model_full, model_cli, prompt, opts, loop_threshold=3):
    """Run one step -> (result_text, usage, tripped, trip_reason).

    The breaker trips when the same tool is called with identical arguments more
    than `loop_threshold` times in a row.
    """
    if mode == "cli":
        # The json path returns real usage and is reliable with tool-using steps;
        # stream-json hangs there. So live loop-detection covers selftest/api/mock
        # only — in cli, the cost/turn caps and --call-timeout bound runaway loops.
        data = backend.call_cli(model_cli, STEP_SYSTEM, prompt, opts)
        return (data["result"] or "(no result)", data["usage"], False, None)

    # mock / api: the Director runs its own tool loop and watches for repeats
    messages = [{"role": "user", "content": prompt}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    last_sig, streak = None, 0
    for _ in range(MAX_TOOL_ITERS):
        resp = backend.call_model(
            model_full,
            STEP_SYSTEM,
            messages,
            tools=tools.SCHEMAS,
            max_tokens=2048,
            mock=(mode == "mock"),
        )
        _add_usage(usage, resp.get("usage"))
        if resp.get("stop_reason") == "tool_use":
            messages.append({"role": "assistant", "content": resp["content"]})
            results = []
            for b in resp["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    sig = (
                        b.get("name", "")
                        + "|"
                        + json.dumps(b.get("input", {}), sort_keys=True)
                    )
                    streak = streak + 1 if sig == last_sig else 1
                    last_sig = sig
                    if streak > loop_threshold:
                        return (
                            "(circuit breaker tripped)",
                            usage,
                            True,
                            f"tool '{b.get('name')}' called with identical arguments "
                            f"{streak}x in a row",
                        )
                    out = tools.execute(b["name"], b.get("input", {}), opts)
                    results.append(
                        {"type": "tool_result", "tool_use_id": b["id"], "content": out}
                    )
            messages.append({"role": "user", "content": results})
            continue
        return _text_of(resp) or "(no text)", usage, False, None
    return "(step hit tool-iteration limit)", usage, False, None


def _budget_exceeded(budget, state):
    """Return a reason string if a hard cap (cost/turns) is crossed, else None."""
    if budget.get("max_cost") and state["spent"] >= budget["max_cost"]:
        return (
            f"cost cap ${budget['max_cost']:.2f} reached "
            f"(session spent ${state['spent']:.2f})"
        )
    if budget.get("max_turns") and state["turns"] >= budget["max_turns"]:
        return f"turn cap {budget['max_turns']} reached (turns {state['turns']})"
    return None


def _one_round(
    task_for_round,
    default_model,
    planner_model,
    mode,
    opts,
    loop_threshold,
    budget,
    state,
    cache,
    min_steps=2,
):
    """Plan and execute one attempt, enforcing the cost/turn caps and the
    frontier-downgrade policy.
    Returns (steps_out, planner_cost, actual, baseline, plan_model, breaker)."""
    reason = _budget_exceeded(budget, state)  # check caps before planning
    if reason:
        _log(
            f"[director]   🛑 HARD CAP: {reason} — halting run to protect your budget."
        )
        return (
            [],
            0.0,
            0.0,
            0.0,
            planner_model,
            {"tripped": True, "kind": "budget", "reason": reason, "step": None},
        )

    # ---- planner (cache-checked) ----
    plan_key = cache_mod.make_key("plan", planner_model, task_for_round)
    hit = cache.get(plan_key, ns="plan", text=task_for_round)
    if hit:
        e = hit["entry"]
        plan, plan_usage, plan_model, planner_cost = (
            e["result"],
            e["usage"],
            planner_model,
            0.0,
        )
        _log(f"[director] 💾 plan CACHE HIT ({hit['how']}) — saved ${e['cost']:.4f}")
    else:
        _log(f"[director] planning on {planner_model} (mode={mode}) ...")
        plan, plan_usage, plan_model = planner.make_plan(
            task_for_round, planner_model, mode, min_steps=min_steps
        )
        planner_cost = models.cost(planner_model, plan_usage)
        state["turns"] += 1
        state["spent"] += planner_cost
        cache.put(
            plan_key,
            "plan",
            planner_model,
            plan,
            plan_usage,
            planner_cost,
            ns="plan",
            text=task_for_round,
        )
    steps = plan.get("steps", [])
    _log(f"[director] plan ready: {len(steps)} step(s), planner ${planner_cost:.4f}")

    steps_out, actual, baseline, prior, breaker = [], 0.0, 0.0, [], None
    n = len(steps)
    for i, step in enumerate(steps, 1):
        # hard cap: this is where the next request gets intercepted
        reason = _budget_exceeded(budget, state)
        if reason:
            breaker = {
                "tripped": True,
                "kind": "budget",
                "reason": reason,
                "step": step.get("title"),
            }
            _log(
                f"[director]   🛑 HARD CAP: {reason} — halting run to protect your budget."
            )
            break

        tier = step.get("tier")
        # once the reasoning budget is crossed, lock the frontier tier out
        rb = budget.get("reasoning_budget")
        if rb and state["spent"] >= rb and not state["downgraded"]:
            state["downgraded"] = True
            _log(
                f"[director]   ⚠ reasoning budget ${rb:.2f} exceeded — FRONTIER LOCKED; "
                f"premium steps now run on the cheap tier."
            )
        if state["downgraded"] and tier == "premium":
            _log(f"[director]     downgraded '{step.get('title')}': premium -> cheap")
            tier = "cheap"

        model_full = models.resolve_tier(tier)
        model_cli = models.to_cli(model_full)
        prompt = _step_prompt(task_for_round, step, prior)

        skey = cache_mod.make_key("step", model_full, prompt)
        hit = cache.get(skey, ns="step", text=prompt)
        if hit:
            e = hit["entry"]
            result, usage, tripped, trip_reason = e["result"], e["usage"], False, None
            a = 0.0  # no model call, no spend
            b = models.cost(default_model, usage)
            _log(
                f"[director] step {i}/{n}: 💾 CACHE HIT ({hit['how']}) — "
                f"{step.get('title')}  (saved ${e['cost']:.4f})"
            )
        else:
            _log(
                f"[director] step {i}/{n}: [{tier}] {step.get('title')}  -> {model_full}"
                f"   (turn {state['turns'] + 1}, spent ${state['spent']:.2f})"
            )
            result, usage, tripped, trip_reason = _run_step(
                mode, model_full, model_cli, prompt, opts, loop_threshold
            )
            a = models.cost(model_full, usage)
            b = models.cost(default_model, usage)
            state["turns"] += 1
            state["spent"] += a
            _log(f"[director]   done: ${a:.4f}  (session spent ${state['spent']:.2f})")
            if not tripped:
                cache.put(
                    skey, "step", model_full, result, usage, a, ns="step", text=prompt
                )

        actual += a
        baseline += b
        steps_out.append(
            {
                "id": step.get("id"),
                "title": step.get("title"),
                "tier": tier,
                "model": model_full,
                "usage": usage,
                "actual_cost": a,
                "baseline_cost": b,
                "result": result,
                "tripped": tripped,
                "cached": bool(hit),
            }
        )
        prior.append({"title": step.get("title"), "result": result})
        if tripped:
            breaker = {
                "tripped": True,
                "kind": "loop",
                "reason": trip_reason,
                "step": step.get("title"),
            }
            _log(f"[director]   🛑 CIRCUIT BREAKER: {trip_reason} — halting run.")
            break
    return steps_out, planner_cost, actual, baseline, plan_model, breaker


def _remediation_task(task, verdict):
    issues = verdict.get("issues") or [verdict.get("summary", "")]
    bullets = "\n".join(f"- {i}" for i in issues if i)
    return (
        f"Original task:\n{task}\n\nA reviewer found the previous attempt "
        f"INCOMPLETE or INCORRECT. Fix ONLY these remaining issues, completely "
        f"and correctly:\n{bullets}"
    )


def run(
    task,
    default_model,
    mode="cli",
    allow_writes=False,
    allow_shell=False,
    review=True,
    max_rounds=3,
    call_timeout=1800,
    loop_threshold=3,
    max_turns=50,
    max_cost=10.0,
    reasoning_budget=0.0,
    planner_model=None,
    review_model=None,
    min_confidence=0.7,
    cache_enabled=True,
    cache_threshold=0.97,
    cache_ttl=0,
    clear_cache=False,
    min_steps=2,
):
    # planner/review fall back to the baseline model unless overridden
    planner_model = planner_model or default_model
    review_model = review_model or default_model
    accepted = None
    cache = cache_mod.DirectorCache(
        enabled=cache_enabled, threshold=cache_threshold, ttl=cache_ttl
    )
    if clear_cache:
        cache.clear()
        _log("[director] cache cleared.")
    opts = {
        "allow_writes": allow_writes,
        "allow_shell": allow_shell,
        "call_timeout": call_timeout,
    }
    budget = {
        "max_turns": max_turns,
        "max_cost": max_cost,
        "reasoning_budget": reasoning_budget,
    }
    state = {"turns": 0, "spent": 0.0, "downgraded": False}

    all_steps, planner_total, actual_steps, baseline_steps, review_total = (
        [],
        0.0,
        0.0,
        0.0,
        0.0,
    )
    verdict, plan_model, rounds, current_task, breaker = (
        None,
        default_model,
        0,
        task,
        None,
    )

    for rnd in range(1, max_rounds + 1):
        rounds = rnd
        _log(f"[director] ===== round {rnd}/{max_rounds} =====")
        s_out, p_cost, a_cost, b_cost, plan_model, breaker = _one_round(
            current_task,
            default_model,
            planner_model,
            mode,
            opts,
            loop_threshold,
            budget,
            state,
            cache,
            min_steps,
        )
        all_steps += s_out
        planner_total += p_cost
        actual_steps += a_cost
        baseline_steps += b_cost

        if breaker:
            _log("[director] run aborted by circuit breaker.")
            break

        if not (review and all_steps):
            break

        # verify
        _log(f"[director] reviewing on {review_model} ...")
        verdict, r_usage = reviewer.review(task, all_steps, review_model, mode)
        rcost = models.cost(review_model, r_usage)
        review_total += rcost
        state["turns"] += 1
        state["spent"] += rcost
        issues = verdict.get("issues") or []
        conf = verdict.get("confidence")
        # Accept only if complete, no issues, and confident enough. complete=true
        # with issues or low confidence counts as not done, so a fix round runs.
        accepted = (
            bool(verdict.get("complete"))
            and not issues
            and (conf is None or conf >= min_confidence)
        )
        _log(
            f"[director]   review: complete={verdict.get('complete')} "
            f"confidence={conf} issues={len(issues)} -> accepted={accepted}"
        )
        if accepted:
            _log("[director] task verified complete — done.")
            break
        if rnd < max_rounds:
            current_task = _remediation_task(task, verdict)
            _log(
                f"[director] NOT accepted ({len(issues)} issue(s), conf {conf}) — "
                f"replanning to fix them (round {rnd + 1}/{max_rounds})"
            )
        else:
            _log(
                "[director] still not accepted at max rounds — stopping. See review issues."
            )

    actual_total = planner_total + actual_steps + review_total
    baseline_total = baseline_steps
    savings = baseline_total - actual_total
    pct = (savings / baseline_total * 100.0) if baseline_total else 0.0

    return {
        "task": task,
        "mode": mode,
        "default_model": default_model,
        "rounds": rounds,
        "max_rounds": max_rounds,
        "turns": state["turns"],
        "downgraded": state["downgraded"],
        "budget": budget,
        "completed": False if breaker else accepted,
        "breaker": breaker,
        "cache": cache.snapshot(),
        "steps": all_steps,
        "review": verdict,
        "cost": {
            "baseline_total": round(baseline_total, 6),
            "actual_total": round(actual_total, 6),
            "planner_overhead": round(planner_total, 6),
            "review_overhead": round(review_total, 6),
            "tiering_savings": round(baseline_steps - actual_steps, 6),
            "net_savings": round(savings, 6),
            "net_savings_pct": round(pct, 2),
        },
    }
