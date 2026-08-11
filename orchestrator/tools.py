"""Agentic tools the Director can execute per step.

Safety model (this may run against a live repo):
  - read_file        : always allowed (reads are safe)
  - write_file       : DRY-RUN unless opts['allow_writes'] is True
  - run_powershell   : DRY-RUN unless opts['allow_shell'] is True
  - glab / git       : routed through the zero-trust guard (policy-limited, token-isolated)

Executors always return a short text result string (fed back as a tool_result).
"""

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GLAB_GW = os.path.join(REPO, "scripts", "glab_gw.py")
_GIT_GW = os.path.join(REPO, "scripts", "git_gw.py")
MAX_OUT = 8000  # truncate tool output fed back to the model


# ---- Anthropic tool schemas ------------------------------------------------
SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read a local file and return its text.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write text to a local file (dry-run unless writes are enabled).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_powershell",
        "description": "Run a PowerShell command (dry-run unless shell is enabled).",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "glab",
        "description": "Run a GitLab CLI command through the zero-trust guard. "
        "Pass args as a list, e.g. ['mr','list'].",
        "input_schema": {
            "type": "object",
            "properties": {"args": {"type": "array", "items": {"type": "string"}}},
            "required": ["args"],
        },
    },
    {
        "name": "git",
        "description": "Run a git command through the zero-trust guard. "
        "Pass args as a list, e.g. ['push','origin','my-branch'].",
        "input_schema": {
            "type": "object",
            "properties": {"args": {"type": "array", "items": {"type": "string"}}},
            "required": ["args"],
        },
    },
]


def _truncate(s):
    s = s or ""
    return (
        s
        if len(s) <= MAX_OUT
        else s[:MAX_OUT] + f"\n...[truncated {len(s) - MAX_OUT} chars]"
    )


def _run(cmd):
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        return f"(exit {p.returncode})\n{_truncate(out)}"
    except FileNotFoundError as e:
        return f"[error] {e}"


def execute(name, inp, opts):
    inp = inp or {}
    if name == "read_file":
        path = inp.get("path", "")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return _truncate(f.read())
        except Exception as e:
            return f"[error reading {path}] {e}"

    if name == "write_file":
        path, content = inp.get("path", ""), inp.get("content", "")
        if not opts.get("allow_writes"):
            return (
                f"[dry-run] would write {len(content)} chars to {path} "
                f"(enable with --allow-writes)"
            )
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"wrote {len(content)} chars to {path}"
        except Exception as e:
            return f"[error writing {path}] {e}"

    if name == "run_powershell":
        command = inp.get("command", "")
        if not opts.get("allow_shell"):
            return (
                f"[dry-run] would run PowerShell: {command} (enable with --allow-shell)"
            )
        return _run(["pwsh", "-NoProfile", "-Command", command])

    if name == "glab":
        return _run([sys.executable, _GLAB_GW] + list(inp.get("args", [])))

    if name == "git":
        return _run([sys.executable, _GIT_GW] + list(inp.get("args", [])))

    return f"[error] unknown tool '{name}'"
