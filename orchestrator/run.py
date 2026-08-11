"""CLI: plan a task, execute it with per-step model swapping, report cost.

Examples:
    # dry test with no API key:
    python -m orchestrator.run "Fix the auth timeout bug and open an MR" --mock

    # real run (reads allowed; writes/shell are dry-run unless enabled):
    python -m orchestrator.run "Fix the auth timeout bug and open an MR" \
        --default-model claude-opus-4-8

    # allow it to actually edit files and run PowerShell:
    python -m orchestrator.run "..." --allow-writes --allow-shell
"""

import argparse
import json
import os
import sys

from . import director


def _setup_io():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _selftest(loop_threshold):
    """Demonstrate the circuit breaker without a model: a synthetic process emits a
    looping tool-call stream, and the real monitor must detect and kill it. Needed
    because real models won't loop on command."""
    from . import backend

    print("[selftest] emitting a synthetic looping stream (identical Read calls),")
    print(
        f"[selftest] loop-threshold={loop_threshold} — the monitor should trip and kill it.\n"
    )
    d = backend.selftest_stream(
        loop_threshold=loop_threshold,
        on_progress=lambda n, s: print(
            f"  tool '{n}' called x{s}" + ("   <-- TRIP!" if s > loop_threshold else "")
        ),
    )
    print()
    if d["tripped"]:
        print(f"RESULT: 🛑 CIRCUIT BREAKER TRIPPED — {d['trip_reason']}")
        print(
            f"        killed after {d['tool_calls']} tool calls "
            f"(the runaway emitter would have gone to 20)."
        )
    else:
        print(f"RESULT: NO TRIP (unexpected) — tool_calls={d['tool_calls']}")
    return 0


def main(argv=None):
    _setup_io()
    ap = argparse.ArgumentParser(description="Macro-Planner + Model Director")
    ap.add_argument(
        "task", nargs="?", default="", help="the coding task to plan and execute"
    )
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="prove the circuit breaker works using a synthetic looping "
        "tool-call stream (no models involved)",
    )
    ap.add_argument(
        "--default-model",
        default="claude-opus-4-8",
        help="the model you'd otherwise use for every step. This is the cost "
        "baseline savings are measured against (default claude-opus-4-8)",
    )
    ap.add_argument(
        "--planner-model",
        default=None,
        help="model for the one-time planning call "
        "(default: same as --default-model). Use a cheaper one to save.",
    )
    ap.add_argument(
        "--review-model",
        default=None,
        help="model for the final review call "
        "(default: same as --default-model). Use a cheaper one to save.",
    )
    ap.add_argument(
        "--backend",
        choices=["cli", "mock", "api"],
        default="cli",
        help="cli = headless `claude -p` (local Claude CLI auth, no key); "
        "mock = canned; api = Anthropic API (needs key)",
    )
    ap.add_argument("--mock", action="store_true", help="shorthand for --backend mock")
    ap.add_argument("--allow-writes", action="store_true", help="allow file edits")
    ap.add_argument(
        "--allow-shell", action="store_true", help="allow running commands/tests"
    )
    ap.add_argument(
        "-x",
        "--go",
        action="store_true",
        help="shorthand: enable BOTH --allow-writes and --allow-shell",
    )
    ap.add_argument(
        "--review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="premium-model review of the work at the end (default on)",
    )
    ap.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        help="max verify->replan->fix rounds until review passes (default 3)",
    )
    ap.add_argument(
        "--min-steps",
        type=int,
        default=2,
        help="minimum number of steps the planner must produce (default 2)",
    )
    ap.add_argument(
        "--min-confidence",
        type=float,
        default=0.7,
        help="review must report >= this confidence (and no issues) to be "
        "accepted; otherwise a fix round runs (default 0.7)",
    )
    ap.add_argument(
        "--call-timeout",
        type=int,
        default=1800,
        help="per model-call safety timeout in seconds (default 1800 = 30 min)",
    )
    ap.add_argument(
        "--loop-threshold",
        type=int,
        default=3,
        help="trip the circuit breaker if a tool is called with identical "
        "args more than N times in a row (default 3)",
    )
    ap.add_argument(
        "--max-cost",
        type=float,
        default=10.0,
        help="hard cap: halt the session once spend reaches $N (default 10; 0=off)",
    )
    ap.add_argument(
        "--max-turns",
        type=int,
        default=50,
        help="hard cap: halt the session after N model turns (default 50; 0=off)",
    )
    ap.add_argument(
        "--reasoning-budget",
        type=float,
        default=0.0,
        help="once spend reaches $N, stop using the premium tier and run "
        "premium steps on the cheap tier instead (0=off)",
    )
    ap.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="response cache: reuse plan/step results, skip repeat spawns (default on)",
    )
    ap.add_argument(
        "--clear-cache",
        action="store_true",
        help="clear the response cache before running (use if the repo changed)",
    )
    ap.add_argument(
        "--cache-threshold",
        type=float,
        default=0.97,
        help="Tier-2 semantic cache cutoff (0-1; >1 disables semantic, exact only)",
    )
    ap.add_argument(
        "--cache-ttl",
        type=int,
        default=0,
        help="ignore cached entries older than N seconds (0 = never expire)",
    )
    ap.add_argument(
        "--workdir",
        default=os.environ.get("GATEWAY_DIRECTOR_WORKDIR"),
        help="target repo dir (defaults to $GATEWAY_DIRECTOR_WORKDIR)",
    )
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args(argv)

    if args.workdir:
        os.chdir(args.workdir)

    if args.selftest:
        return _selftest(args.loop_threshold)

    mode = "mock" if args.mock else args.backend
    allow_writes = args.allow_writes or args.go
    allow_shell = args.allow_shell or args.go
    report = director.run(
        args.task,
        args.default_model,
        mode=mode,
        allow_writes=allow_writes,
        allow_shell=allow_shell,
        review=args.review,
        max_rounds=args.max_rounds,
        call_timeout=args.call_timeout,
        loop_threshold=args.loop_threshold,
        max_turns=args.max_turns,
        max_cost=args.max_cost,
        reasoning_budget=args.reasoning_budget,
        planner_model=args.planner_model,
        review_model=args.review_model,
        min_confidence=args.min_confidence,
        cache_enabled=args.cache,
        cache_threshold=args.cache_threshold,
        cache_ttl=args.cache_ttl,
        clear_cache=args.clear_cache,
        min_steps=args.min_steps,
    )

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    c = report["cost"]
    print("=" * 72)
    print("MACRO-PLAN")
    print("=" * 72)
    print(f"task          : {report['task']}")
    print(f"default model : {report['default_model']}  (baseline)")
    print(
        f"rounds        : {report['rounds']}/{report['max_rounds']}    "
        f"completed: {report['completed']}"
    )
    print()
    for s in report["steps"]:
        print(f"  step {s['id']}: {s['title']}")
        print(f"    tier={s['tier']:<8} model={s['model']}")
        print(f"    cost=${s['actual_cost']:.6f}  (baseline ${s['baseline_cost']:.6f})")
        print(f"    result: {s['result'][:120]}")
    print()

    r = report.get("review")
    if r:
        print("=" * 72)
        print(f"REVIEW  (by {report['default_model']})")
        print("=" * 72)
        print(f"  complete : {r.get('complete')}    confidence: {r.get('confidence')}")
        for issue in r.get("issues") or []:
            print(f"  - issue  : {issue}")
        print(f"  summary  : {r.get('summary', '')[:300]}")
        print()

    if report.get("downgraded"):
        print(
            "⚠ FRONTIER LOCKED this run — reasoning budget exceeded, premium steps "
            "were downgraded to the cheap tier.\n"
        )

    brk = report.get("breaker")
    if brk:
        kind = brk.get("kind", "loop")
        label = {"loop": "LOOP DETECTED", "budget": "HARD CAP REACHED"}.get(kind, kind)
        print("=" * 72)
        print(f"🛑 CIRCUIT BREAKER TRIPPED — {label}")
        print("=" * 72)
        if brk.get("step"):
            print(f"  step   : {brk.get('step')}")
        print(f"  reason : {brk.get('reason')}")
        print(
            f"  turns  : {report.get('turns')}   session spend: ${report['cost']['actual_total']:.4f}"
        )
        print("  -> run halted to protect your budget.")
        print()

    review_ovh = c.get("review_overhead", 0)
    tiered_steps = c["actual_total"] - c["planner_overhead"] - review_ovh
    cache = report.get("cache") or {}
    if cache.get("hits") or cache.get("misses"):
        print("=" * 72)
        print("CACHE")
        print("=" * 72)
        served = cache.get("hits", 0) + cache.get("misses", 0)
        rate = (cache.get("hits", 0) / served * 100) if served else 0
        print(
            f"  hits/misses  : {cache.get('hits', 0)}/{cache.get('misses', 0)}  "
            f"({rate:.0f}% hit rate)   entries stored: {cache.get('entries', 0)}"
        )
        print(f"  tokens saved : {cache.get('tokens_saved', 0):,}")
        print(
            f"  $ saved      : ${cache.get('cost_saved', 0):.6f}   "
            f"(work served from cache instead of spawning claude)"
        )
        print()

    print("=" * 72)
    print("COST")
    print("=" * 72)
    print(f"  baseline (every step on {report['default_model']}, no planner/review):")
    print(f"      ${c['baseline_total']:.6f}")
    print("  actual (what we spent):")
    print(f"      tiered steps     : ${tiered_steps:.6f}")
    print(f"      planner overhead : ${c['planner_overhead']:.6f}")
    print(f"      review overhead  : ${review_ovh:.6f}")
    print("      -------------------------------")
    print(f"      actual total     : ${c['actual_total']:.6f}")
    print("  savings:")
    print(f"      from tiering (steps on cheaper models) : ${c['tiering_savings']:.6f}")
    print(
        f"      minus planner + review overhead        : -${c['planner_overhead'] + review_ovh:.6f}"
    )
    print(
        f"      NET SAVINGS vs baseline                : ${c['net_savings']:.6f}"
        f"  ({c['net_savings_pct']:.1f}%)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
