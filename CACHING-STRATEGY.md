# Caching Strategy — Agentic Plan Caching for the Model Director

A proposal, not shipped code. What to build, in what order, and the trade-offs.

> **Source:** "Agentic Plan Caching: Test-Time Memory for Fast and Cost-Efficient LLM
> Agents" — Zhang, Wornow, Olukotun (Stanford), NeurIPS 2025. Reports **−50.31% cost,
> −27.28% latency, 96.61% of optimal accuracy** across 5 workloads. (The arXiv version
> `2506.14852v1` is the same method on 2 workloads; use the NeurIPS numbers.)
>
> **See also:** `DOCUMENTATION.md` §3, `orchestrator/cache.py`, `orchestrator/director.py`.

---

## 1. The short version

`cache.py` today stores the *result* of the planner call and of each step, keyed by
request text (exact SHA-256, plus a local embedder at cosine ≥ 0.97). That is
query-level semantic caching — which the paper identifies as insufficient for agents:
it caches whole answers, is keyed per model, breaks on small input changes, and can
serve a stale or wrong result on a false-positive hit.

**Agentic Plan Caching (APC)** caches the *plan* instead — the expensive, reusable part:

1. **Extract a keyword** (the intent) from the task with a cheap model.
2. **Look it up** in a `keyword → plan template` store by **exact match** — O(1), no
   threshold to tune, near-zero false positives.
3. **On a hit**, adapt the template into a concrete plan with a cheap model instead of
   the expensive planner.
4. **On a miss**, plan as today. After a *successful* run, distil the plan into a
   context-stripped template and store it.

This targets the Director's most expensive call — the frontier planner bookend that
`DOCUMENTATION.md` already flags as the cost driver. Because templates are text, not KV
state, APC is model-agnostic, so it sidesteps the "per-step tiering is incompatible
with prompt caching" limitation.

**Keep the existing response cache** as a lossless Tier-0 for identical re-runs, and add
APC as a layer above it.

---

## 2. Why chatbot-style caching fails for agents

| Technique | Caches | Why it breaks |
|---|---|---|
| **Context caching** (KV / prompt caching) | internal model KV state | Model-specific, so it can't transfer across the models an agent uses per stage. Needs an exact text match. |
| **Semantic caching** (GPTCache-style) | `query → output` pairs, matched by embedding similarity | Agent outputs depend on external data, not just the query. And any similarity threshold either false-positives (serves a wrong answer) or false-negatives (misses reuse). It never captures the *transformation* from prompt to answer. |

Our `cache.py` is the second row: response-level, model-keyed, fixed cosine 0.97. At
0.97 it almost never gets a semantic hit, so it's effectively exact-only; lower it and
you take on the false-positive risk.

## 3. Design decisions the paper validated

Adopt these as-is:

- **Match on intent keyword, not query embedding.** Query similarity over-weights
  specifics (company names, filenames) and has no good threshold. Keyword matching gave
  materially lower false-positive *and* false-negative rates.
- **Exact match, not fuzzy.** A dict lookup stays under 20 µs at 10⁶ entries. Fuzzy
  lookup was orders of magnitude slower and reintroduced the threshold problem. Offer it
  only as an opt-in: higher hit rate and lower cost, but accuracy drops.
- **Cache a distilled template, not the full history.** Small models choke on long raw
  logs; full-history caching was both less accurate (72% vs 85.5%) *and* more expensive.
  The filtering is what makes templates reusable.
- **Overhead is small.** Keyword extraction plus template generation is ~1.04% of total
  cost, and ~1.31% even at a 0% hit rate.
- **Cold start is real.** Savings ramp as the cache warms. Mitigate by pre-warming from
  known workloads, and auto-disable when the hit rate stays low.
- **Size:** plain LRU. Benefit plateaus once capacity ≈ the number of unique keywords.

---

## 4. Where the Director stands today

In `_one_round` (`director.py`) and `cache.py`:

- **Planner** — `make_key("plan", planner_model, task_for_round)`, then
  `cache.get(..., ns="plan")`. On a miss, run the frontier planner and store the plan JSON.
- **Steps** — `make_key("step", model_full, prompt)`; a hit reuses the step result verbatim.
- **Matching** — exact SHA of `(kind, model, text)` plus a hash-based local embedder at
  cosine ≥ `--cache-threshold` (default 0.97), namespaced by kind.
- **On a hit** — skips the `claude -p` spawn entirely: 0 tokens, $0.
- **Persistence** — `data/director_cache.json`, with `--clear-cache` and `--cache-ttl`.

| Property | Today | With APC |
|---|---|---|
| What's reused | whole planner/step *answers* | plan *structure*, re-adapted per context |
| Match key | request text (SHA / cosine) | intent keyword (exact) |
| Model coupling | keyed by model, so no cross-tier reuse | model-agnostic templates |
| Input drift | brittle when exact, risky when loosened | tolerant; specifics are abstracted out |
| Staleness risk | **high** — a cached answer can be wrong if the repo changed | **lower** — a plan shape, re-executed live |
| Best case | identical re-runs, the fix loop | *similar* tasks across repos and sessions |

They're complementary: response cache for identical replays, APC for the
similar-but-not-identical tasks it can't safely serve.

---

## 5. Mapping onto our architecture

The Director is already a Plan-Act agent, so the mapping is direct:

| Paper concept | Director equivalent |
|---|---|
| Query `q` | the `task` for the round |
| `ExtractKeyword` | one `claude -p` call on the cheap tier |
| `keyword → template` cache | a new `data/plan_templates.json`, exact dict lookup |
| Large planner (miss) | today's `planner.py` on `--planner-model`, unchanged |
| Small planner (hit) | a new adapt call on a cheap model |
| Actor | the per-step `claude -p` executors, unchanged |
| Plan template | the step list (tier + goal + tools) with paths, names and numbers removed |
| `GenerateTemplate` | rule-based prune, then one cheap-model generalize call |

Two things are specific to this project:

1. **Here, the data-dependent context is the repository** — filenames, branch, error
   text. The template must abstract those out (`/repo/src/x.py` → `<file>`) and be
   re-adapted per repo. This is exactly why template caching beats answer caching for
   us: a cached *answer* about one repo is useless, even dangerous, for another, but a
   cached *plan shape* ("read changed files → summarise → write changes.md → open MR")
   transfers cleanly.
2. **The review loop is the correctness gate.** The paper stores templates from
   successful runs; we have a stronger signal, so only templatize when the round was
   *accepted* (`complete && no issues && confidence ≥ --min-confidence`). And if an
   adapted plan produces a bad result, the existing review→replan loop catches it and can
   fall back to the full planner — a safety net APC alone doesn't have.

---

## 6. Proposed design

### New module: `orchestrator/plan_cache.py`

```
extract_keyword(task, model, backend) -> str      # cheap call; normalised for stable keys
lookup(keyword) -> template | None                # exact dict hit; fuzzy behind a flag
adapt(template, task, context, model, backend) -> plan
generalize(accepted_plan, keyword, model, backend) -> template
    # 1) rule-based: keep each step's (tier, goal, tools); drop rationale
    # 2) cheap model: replace concrete paths/names/numbers with placeholders
store(keyword, template)                          # LRU-capped, persisted to JSON
stats() -> {hits, misses, cost_saved_vs_full_planner, entries}
```

### `director.py` — the plan phase of `_one_round`

```python
keyword = (
    plan_cache.extract_keyword(task_for_round, keyword_model, backend)
    if enabled
    else None
)
template = plan_cache.lookup(keyword) if keyword else None

if template is not None:  # HIT
    plan, usage, cost = plan_cache.adapt(
        template, task_for_round, ctx, adapter_model, backend
    )
else:  # MISS — existing path, response cache still Tier-0
    plan, usage, cost = planner.make_plan(task_for_round, planner_model, backend, ...)

# ... steps and review loop unchanged ...

if enabled and template is None and accepted:
    plan_cache.store(
        keyword, plan_cache.generalize(plan, keyword, generalize_model, backend)
    )
```

### New flags

| Flag | Default | Purpose |
|---|---|---|
| `--plan-cache` / `--no-plan-cache` | on | enable the APC layer |
| `--keyword-model` | cheap | model for keyword extraction |
| `--adapter-model` | cheap/standard | model that adapts a template on a hit |
| `--plan-cache-fuzzy F` | off | opt-in fuzzy keyword match at threshold F |
| `--plan-cache-size N` | 200 | LRU capacity |
| `--plan-cache-clear` | off | wipe the template store |
| `--plan-cache-min-hitrate F` | 0.05 | auto-disable if the hit rate stays below F |

Prompts adapt the paper's Appendix B.4 (keyword extraction, cache generation, cache
adaptation) to coding tasks and our step-JSON schema.

### Telemetry

Track hit rate, **cost saved = full-planner cost − adapter cost per hit**, template
count, the cold-start curve, and worst-case overhead at a 0% hit rate. Surface it in the
run report and the UI next to the response-cache stats.

---

## 7. Phased delivery

| Phase | Work |
|---|---|
| **0. Harness** | Separate planner cost from step cost so savings are attributable. Capture a small suite of representative tasks to measure hit rate and regressions. |
| **1. Keyword + exact match** | `plan_cache.py` with extract, lookup and store; wire into `director.py`; store from accepted runs; adapt on hit. Highest value — cuts the planner bookend on repeated task shapes. |
| **2. Template quality** | The two-stage filter. Verify adapted plans keep the same step and tier structure, and that the review loop accepts them at parity with fresh plans. |
| **3. Robustness** | LRU eviction and capacity, `--plan-cache-clear`, auto-disable on low hit rate, opt-in fuzzy matching, pre-warming to blunt cold start. |
| **4. Telemetry** | Hit rate, cost saved, cold-start curve and zero-hit overhead in the report and UI. |

## 8. How it fits the wider cache picture

APC sits *above* the query-level tiers the Director already has:

```
request
  ├─ Tier 0/1: exact-match response cache   (identical replays)      ← cache.py today
  ├─ Tier 2:   semantic response cache      (near-identical queries) ← today, risky below ~0.97
  └─ Tier 3:   Agentic Plan Cache           (keyword → template)     ← this doc
```

It also answers the open risk of lowering the semantic threshold. Matching on intent and
re-executing an *adapted plan* against live context — rather than returning a cached
*answer* — captures cross-repo reuse without the false-positive hazard.

## 9. Risks

| Risk | Mitigation |
|---|---|
| **Cold start** — no early savings | Pre-warm from a known-tasks file. Overhead is ~1% even at zero hits, so it's safe to leave on. |
| **Keyword instability** — same task worded differently, reuse missed | Normalise keywords; keep the extraction prompt tight and tested; offer opt-in fuzzy matching. |
| **Bad adapted plan on a hit** | The review→replan loop catches it. On repeated failure, fall back to the full planner and evict the template. |
| **Template staleness** | Templates hold plan shape, not answers, so they degrade gracefully. Add TTL, versioning, and a clear flag. |
| **Small-model adaptation quality** | Choose `--adapter-model` deliberately — the paper found model choice still matters, and cheapest isn't always best. Measure hit vs miss acceptance parity. |
| **Overhead on low-hit workloads** | Auto-disable via `--plan-cache-min-hitrate`; keep keyword and generalize calls on the cheap tier. |
| **The no-API-key constraint** | Every APC call is just another `claude -p --model <cheap>` through the existing `backend.py`. No key, no new dependency. |

## 10. Success criteria

- **Cost** — measurable reduction in planner spend on repeated task shapes, approaching
  the paper's ~40–50% planning-cost reduction on a warm cache, with total overhead ≤ 1–2%
  at zero hits.
- **Quality** — acceptance rate on cache-hit runs within noise of cache-miss runs.
- **Safety** — no false-positive reuse across unrelated tasks; no stale answers served,
  since plans are re-executed live.
- **Observability** — hit rate, planner cost saved, template count and cold-start curve
  visible in the report and UI.
