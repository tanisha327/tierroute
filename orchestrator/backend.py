"""Model backend: call a Claude model for real, or return a deterministic mock.

Standard library only (urllib), so the orchestrator stays dependency-free. Real
mode needs ANTHROPIC_API_KEY; mock mode returns canned responses so the whole
plan -> route -> execute -> cost flow can be tested without one.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# The plan the mock planner always returns.
_MOCK_PLAN = {
    "steps": [
        {
            "id": 1,
            "title": "Scan & context gathering",
            "tier": "cheap",
            "goal": "Read the relevant local files and extract the code/context needed "
            "to locate the bug.",
            "tools": ["read_file"],
        },
        {
            "id": 2,
            "title": "Algorithmic planning",
            "tier": "premium",
            "goal": "Reason about the root cause and design the precise structural fix.",
            "tools": [],
        },
        {
            "id": 3,
            "title": "Implement fix & verify",
            "tier": "standard",
            "goal": "Apply the change to local files and run PowerShell tests to verify.",
            "tools": ["write_file", "run_powershell"],
        },
        {
            "id": 4,
            "title": "Write MR summary",
            "tier": "cheap",
            "goal": "Write a detailed markdown summary of the change for the GitLab MR.",
            "tools": [],
        },
    ]
}


def call_model(
    model, system, messages, tools=None, tool_choice=None, max_tokens=2048, mock=False
):
    """Return an Anthropic Messages response dict."""
    if mock:
        return _mock(model, messages, tools)

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — set it, or run with --mock")
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def _norm_usage(u):
    """Flatten claude-cli usage into {input_tokens, output_tokens} (incl. cache)."""
    u = u or {}
    inp = (
        (u.get("input_tokens", 0) or 0)
        + (u.get("cache_creation_input_tokens", 0) or 0)
        + (u.get("cache_read_input_tokens", 0) or 0)
    )
    return {"input_tokens": inp, "output_tokens": u.get("output_tokens", 0) or 0}


def _wrap_cmd(parts):
    """On Windows, a .cmd/.bat launcher must run via cmd.exe."""
    if not parts:
        return parts
    resolved = shutil.which(parts[0]) or parts[0]
    if resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved] + parts[1:]
    return [resolved] + parts[1:]


def _claude_base():
    # GATEWAY_CLAUDE_CMD overrides how `claude` is launched — useful when it sits
    # behind a wrapper, e.g. GATEWAY_CLAUDE_CMD="my-env-wrapper -- claude".
    override = os.environ.get("GATEWAY_CLAUDE_CMD")
    if override:
        return _wrap_cmd(override.split())
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError(
            "`claude` not found on PATH. Install/launch the Claude Code CLI so "
            "`claude` is reachable, or set GATEWAY_CLAUDE_CMD to your launch command, e.g.\n"
            '  setx GATEWAY_CLAUDE_CMD "my-env-wrapper -- claude"'
        )
    return ["cmd", "/c", exe] if exe.lower().endswith((".cmd", ".bat")) else [exe]


def _perm_flags(opts):
    """Map opts to claude permission flags.

    Uses explicit --allowed-tools, which auto-permits non-interactively. The
    --permission-mode alternatives need a one-time interactive OK that hangs
    headless `-p` runs.
    """
    read = ["Read", "Grep", "Glob", "WebFetch"]
    if opts.get("allow_shell"):  # edits + commands/tests + glab/git via Bash
        return [
            "--allowed-tools",
            *read,
            "Edit",
            "Write",
            "MultiEdit",
            "NotebookEdit",
            "Bash",
        ]
    if opts.get("allow_writes"):  # edits, no shell
        return ["--allowed-tools", *read, "Edit", "Write", "MultiEdit", "NotebookEdit"]
    return ["--allowed-tools", *read]  # read-only (planner & default)


def call_cli(model_alias, system, prompt, opts=None, timeout=None):
    """One headless `claude -p` call via the local CLI — no API key needed.

    System preamble and prompt go on stdin to dodge Windows arg-quoting; only
    simple flags go on the command line. Writes and shell stay off unless opts
    enable them. `timeout` (seconds) catches a hung call, defaulting to
    opts['call_timeout'].
    Returns {result, usage, reported_cost_usd}.
    """
    opts = opts or {}
    if timeout is None:
        timeout = opts.get("call_timeout") or 1800
    cmd = _claude_base() + ["-p", "--model", model_alias, "--output-format", "json"]
    cmd += _perm_flags(opts)

    stdin = (system + "\n\n---\n\n" + prompt) if system else prompt
    env = os.environ.copy()
    env["GATEWAY_IN_DIRECTOR"] = "1"  # anti-recursion marker for CLAUDE.md
    p = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "")[:500]
        try:  # pull a clean error out of a result event
            j = json.loads((p.stdout or "").strip())
            if j.get("is_error") and j.get("result"):
                msg = j["result"]
        except Exception:
            pass
        if "No deployments available" in msg or "429" in msg:
            msg += (
                "\nHint: that model isn't available in your deployment. Override the "
                "tier via GATEWAY_MODEL_CHEAP / _STANDARD / _PREMIUM, or use "
                "--planner-model/--review-model with a model you have."
            )
        raise RuntimeError(f"claude -p failed ({p.returncode}): {msg}")
    out = p.stdout or ""
    data = None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        s = out.find("{")  # tolerate a leading banner
        if s != -1:
            try:
                data = json.loads(out[s:])
            except json.JSONDecodeError:
                data = None
    if data is None:
        raise RuntimeError(
            "claude did not return JSON. Got: "
            + (out[:200] or "<empty>")
            + "\nHint: if GATEWAY_CLAUDE_CMD points at a wrapper, make sure it emits only "
            "claude's JSON on stdout (no banner/log lines). Prefer having `claude` "
            "directly on PATH and unset GATEWAY_CLAUDE_CMD."
        )
    return {
        "result": data.get("result", ""),
        "usage": _norm_usage(data.get("usage")),
        "reported_cost_usd": data.get("total_cost_usd"),
    }


def _tool_sig(name, tool_input):
    return f"{name}|{json.dumps(tool_input or {}, sort_keys=True)}"


def call_cli_stream(
    model_alias,
    system,
    prompt,
    opts=None,
    loop_threshold=3,
    on_progress=None,
    timeout=None,
):
    """Like call_cli, but streams events and trips the circuit breaker — killing the
    process — when the child calls the same tool with identical arguments more than
    `loop_threshold` times in a row.

    Returns {result, usage, reported_cost_usd, tripped, trip_reason, tool_calls}.
    """
    opts = opts or {}
    if timeout is None:
        timeout = opts.get("call_timeout") or 1800
    cmd = _claude_base() + [
        "-p",
        "--model",
        model_alias,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    cmd += _perm_flags(opts)
    stdin = (system + "\n\n---\n\n" + prompt) if system else prompt
    env = os.environ.copy()
    env["GATEWAY_IN_DIRECTOR"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    proc.stdin.write(stdin)
    proc.stdin.close()
    return _monitor_stream(proc, loop_threshold, on_progress, timeout)


def _monitor_stream(proc, loop_threshold, on_progress=None, timeout=1800):
    """Read a stream-json event stream, kill the process on repeated identical tool
    calls, and return the result dict. Shared by the cli path and the self-test."""
    watchdog = threading.Timer(timeout, proc.kill)  # so a hang can't block forever
    watchdog.start()
    last_sig, streak, tool_calls = None, 0, 0
    tripped, trip_reason = False, None
    result_text, usage, reported_cost = (
        "",
        {"input_tokens": 0, "output_tokens": 0},
        None,
    )
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            etype = ev.get("type")
            if etype == "assistant":
                for b in ev.get("message", {}).get("content", []) or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_calls += 1
                        sig = _tool_sig(b.get("name", ""), b.get("input"))
                        streak = streak + 1 if sig == last_sig else 1
                        last_sig = sig
                        if on_progress:
                            on_progress(b.get("name", ""), streak)
                        if streak > loop_threshold:
                            tripped = True
                            trip_reason = (
                                f"tool '{b.get('name')}' called with identical "
                                f"arguments {streak}x in a row"
                            )
                            proc.kill()
                            break
                if tripped:
                    break
            elif etype == "result":
                result_text = ev.get("result", "") or ""
                usage = _norm_usage(ev.get("usage"))
                reported_cost = ev.get("total_cost_usd")
    finally:
        watchdog.cancel()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return {
        "result": result_text,
        "usage": usage,
        "reported_cost_usd": reported_cost,
        "tripped": tripped,
        "trip_reason": trip_reason,
        "tool_calls": tool_calls,
    }


def selftest_stream(loop_threshold=1, on_progress=None):
    """Emit a synthetic looping tool-call stream and run it through the real monitor.
    Proves the breaker catches and kills a runaway, with no model involved."""
    emitter = (
        "import json,sys,time\n"
        "for i in range(20):\n"
        "    print(json.dumps({'type':'assistant','message':{'content':["
        "{'type':'tool_use','id':'t%d'%i,'name':'Read','input':{'file_path':'/tmp/loop.txt'}}]}}),flush=True)\n"
        "    time.sleep(0.15)\n"
        "print(json.dumps({'type':'result','result':'done','usage':{'input_tokens':10,'output_tokens':1},'total_cost_usd':0.0}),flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", emitter],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return _monitor_stream(proc, loop_threshold, on_progress, timeout=60)


def _mock(model, messages, tools):
    names = [t.get("name") for t in (tools or [])]
    if "submit_plan" in names:
        # One modest planning call.
        return {
            "stop_reason": "tool_use",
            "model": model,
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_plan",
                    "name": "submit_plan",
                    "input": _MOCK_PLAN,
                }
            ],
            "usage": {"input_tokens": 2000, "output_tokens": 700},
        }
    last = messages[-1]["content"] if messages else ""
    if isinstance(last, list):
        last = " ".join(b.get("text", "") for b in last if isinstance(b, dict))
    # Steps carry real code/context, so they're far bigger than the planner call.
    # That size gap is what makes tiering pay off.
    return {
        "stop_reason": "end_turn",
        "model": model,
        "content": [
            {
                "type": "text",
                "text": f"[mock:{model}] step completed. context seen: "
                f"{str(last)[:80]}",
            }
        ],
        "usage": {"input_tokens": 6000, "output_tokens": 800},
    }
