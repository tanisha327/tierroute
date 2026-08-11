"""Final review pass: a premium model checks whether the task was done correctly
and completely, inspecting the actual files (read-only) — not just trusting the
step summaries.
"""

import json

from . import backend, models

REVIEW_SYSTEM = (
    "You are a strict senior reviewer. You are given a task and a summary of the work "
    "an agent performed across steps. Verify whether the task was completed CORRECTLY "
    "and COMPLETELY. Read the actual files (read-only) to check — do not just trust the "
    "summaries. Look for missed locations, partial edits, broken references, skipped "
    "requirements, or anything left incomplete. If you find ANY unresolved issue, you "
    'MUST set "complete": false AND set a LOW "confidence" — \'confidence\' means how '
    "sure you are the task is fully and correctly DONE, so it must drop when issues "
    "remain. Never report high confidence alongside issues."
)

_REVIEW_JSON = (
    "Return ONLY a JSON object (no prose, no code fences): "
    '{"complete": true|false, '
    '"confidence": 0.0-1.0 (your confidence that the task is FULLY and '
    "correctly COMPLETE; this MUST be low if any issues remain), "
    '"issues": ["short description", ...], "summary": "one-paragraph verdict"}'
)


def _parse_json(text):
    t = (text or "").strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1:
        return {
            "complete": None,
            "confidence": None,
            "issues": [],
            "summary": t[:500] or "(no verdict returned)",
        }
    try:
        return json.loads(t[s : e + 1])
    except Exception:
        return {"complete": None, "confidence": None, "issues": [], "summary": t[:500]}


def review(task, steps_out, review_model, mode="cli"):
    """Return (verdict_dict, usage)."""
    work = "\n".join(
        f"- Step {s['id']} [{s['title']}]: {s['result'][:500]}" for s in steps_out
    )
    prompt = f"Task:\n{task}\n\nWork performed:\n{work}\n\n{_REVIEW_JSON}"

    if mode == "mock":
        return (
            {
                "complete": True,
                "confidence": 0.9,
                "issues": [],
                "summary": "[mock] work appears complete.",
            },
            {"input_tokens": 900, "output_tokens": 160},
        )

    if mode == "cli":
        # read-only opts so the reviewer can inspect files but not change them
        data = backend.call_cli(
            models.to_cli(review_model), REVIEW_SYSTEM, prompt, opts={}
        )
        return _parse_json(data["result"]), data["usage"]

    resp = backend.call_model(
        review_model,
        REVIEW_SYSTEM,
        [{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    text = "\n".join(
        b.get("text", "")
        for b in resp.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    )
    return _parse_json(text), resp.get("usage", {})
