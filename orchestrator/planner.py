"""Macro-Planner: one frontier-model call that turns a task into a tiered plan.

Supports three backends:
  - cli  : `claude -p` headless (uses your local Claude CLI auth; no API key)  [default]
  - mock : canned plan (no calls)
  - api  : Anthropic Messages API with forced submit_plan tool (needs API key)
"""

import json

from . import backend, models

PLANNER_SYSTEM = (
    "You are a Macro-Planner for a coding agent. Keep the plan LEAN — each step is a "
    "separate, expensive process with heavy startup, so avoid unnecessary steps. But "
    "ALWAYS produce AT LEAST TWO steps. Rules:\n"
    "  - A natural 2-step split is: (1) read/understand the relevant code (cheap), then "
    "(2) do the actual work — edit/implement/write (the right tier for the work).\n"
    "  - Use ~2 steps for a simple task; add more ONLY when a part genuinely needs a "
    "DIFFERENT model tier or is independent work. Aim for 2, rarely more than 4; never pad.\n"
    "For EACH step choose the CHEAPEST model tier that can do it well:\n"
    "  - cheap    : reading/scanning, extraction, formatting, writing docs/summaries\n"
    "  - standard : normal implementation, straightforward edits, running tests\n"
    "  - premium  : hard reasoning, root-cause analysis, algorithmic/structural design\n"
    "Only use 'premium' where deep reasoning is truly required. Assign tools per step "
    "from: read_file, write_file, run_powershell, glab, git."
)

SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit the step-by-step execution plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "title": {"type": "string"},
                        "tier": {
                            "type": "string",
                            "enum": ["cheap", "standard", "premium"],
                        },
                        "goal": {"type": "string"},
                        "tools": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "title", "tier", "goal"],
                },
            }
        },
        "required": ["steps"],
    },
}

_JSON_HINT = (
    "Return ONLY a JSON object (no prose, no code fences) with shape: "
    '{"steps":[{"id":1,"title":"...","tier":"cheap|standard|premium",'
    '"goal":"...","tools":["read_file"]}]}'
)


def _parse_json(text):
    t = (text or "").strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1:
        raise RuntimeError("planner did not return JSON: " + t[:200])
    return json.loads(t[s : e + 1])


def _extract_plan(resp):
    for block in resp.get("content", []):
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "submit_plan"
        ):
            return block.get("input", {})
    raise RuntimeError("planner did not return a submit_plan tool call")


def make_plan(task, planner_model, mode="cli", min_steps=2):
    """Return (plan_dict, usage, model_used)."""
    req = f"Produce AT LEAST {min_steps} steps (but keep it lean)."
    if mode == "cli":
        prompt = f"Task:\n{task}\n\n{req}\n\n{_JSON_HINT}"
        data = backend.call_cli(
            models.to_cli(planner_model), PLANNER_SYSTEM, prompt, opts={"planner": True}
        )
        return _parse_json(data["result"]), data["usage"], planner_model

    resp = backend.call_model(
        planner_model,
        PLANNER_SYSTEM,
        [
            {
                "role": "user",
                "content": f"Task:\n{task}\n\n{req}\n\nProduce the plan via submit_plan.",
            }
        ],
        tools=[SUBMIT_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "submit_plan"},
        max_tokens=2048,
        mock=(mode == "mock"),
    )
    return _extract_plan(resp), resp.get("usage", {}), resp.get("model", planner_model)
