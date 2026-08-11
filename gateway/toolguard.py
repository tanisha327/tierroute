"""Zero-Trust tool guard for the `glab` and `git` CLIs.

Every command: policy-check -> inject the PAT into the subprocess -> scrub the
token from output -> audit.

Token sources:
  - env var (default): read from GATEWAY_GITLAB_TOKEN in this process.
  - broker (GATEWAY_BROKER=http://127.0.0.1:8765): forwarded to scripts/broker.py,
    which holds the token in its own process. This process never sees it.

    python scripts/glab_gw.py mr list
    python scripts/glab_gw.py --gw-dry-run repo delete foo/bar   # check, don't run

Exit codes: 0 ok/dry-run | 3 policy-denied | 4 misconfigured | else = tool's code.
"""

import base64
import json
import os
import subprocess
import sys
import time

from . import config

REDACTION = "glpat-***REDACTED***"
_FORCE_FLAGS = {"--force", "-f", "--delete", "--mirror"}
# Header the guard sends to the broker. Browsers can't set a custom header on a
# cross-origin request without a CORS preflight, which the broker refuses — so this
# also blocks a web page from driving the broker (see scripts/broker.py).
CLIENT_HEADER = "X-TierRoute-Client"
CLIENT_TOKEN = "tool-guard"


def _command_path(argv):
    """Leading positional tokens (before the first flag), e.g. ['mr','list']."""
    path = []
    for tok in argv:
        if tok.startswith("-"):
            break
        path.append(tok)
    return path


def _api_method(argv):
    """Extract the HTTP method for a `glab api` call (defaults to GET)."""
    for i, tok in enumerate(argv):
        if tok in ("-X", "--method") and i + 1 < len(argv):
            return argv[i + 1].upper()
        if tok.startswith("--method="):
            return tok.split("=", 1)[1].upper()
    return "GET"


def _matches(cmd_path_str, pattern):
    return cmd_path_str == pattern or cmd_path_str.startswith(pattern + " ")


def check_policy(argv, policy):
    """Return (decision, reason, cmd_path_str). decision in {'allow','deny'}."""
    path = _command_path(argv)
    cmd = " ".join(path)
    if not cmd:
        return "deny", "empty command", cmd

    # 1. explicit deny list always wins
    for d in policy.get("deny", []):
        if _matches(cmd, d):
            return "deny", f"matches deny rule '{d}'", cmd

    # 2. `glab api` method restriction
    if path and path[0] == "api":
        method = _api_method(argv)
        allowed = [m.upper() for m in policy.get("api_allowed_methods", ["GET"])]
        if method not in allowed:
            return "deny", f"api method {method} not in {allowed}", cmd

    # 3. allowlist mode requires a positive match
    if policy.get("mode", "allowlist") == "allowlist":
        for a in policy.get("allow", []):
            if _matches(cmd, a):
                return "allow", f"matches allow rule '{a}'", cmd
        return "deny", "no matching allow rule (deny-by-default)", cmd

    # blocklist mode: anything not denied is allowed
    return "allow", "not denied (blocklist mode)", cmd


def _scrub(text, token):
    if not text:
        return ""
    if token:
        text = text.replace(token, REDACTION)
    return text


def _audit(tcfg, cmd, decision, reason, exit_code, tool="gitlab"):
    path = tcfg.get("audit_log")
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": tool,
        "command": cmd,  # positional command path only; never the token
        "decision": decision,
        "reason": reason,
        "exit_code": exit_code,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def dispatch(tool, argv, token, cfg=None):
    """Policy-check, inject `token`, run, scrub, audit -> (exit_code, stdout, stderr).

    `token` is a parameter so this works for both the CLI and the broker, without
    caring where the token is stored.
    """
    cfg = cfg or config.load()
    if tool == "gitlab":
        tcfg = cfg["tools"]["gitlab"]
        decision, reason, cmd = check_policy(argv, tcfg["policy"])
        label = "glab"
    elif tool == "git":
        tcfg = cfg["tools"]["git"]
        decision, reason, cmd = check_git_policy(argv, tcfg["policy"])
        label = "git"
    else:
        return 4, "", f"[tool-guard] unknown tool '{tool}'\n"

    if decision == "deny":
        _audit(tcfg, cmd, "deny", reason, 3, tool=tool)
        return 3, "", f"[tool-guard] DENIED: `{label} {cmd}` -> {reason}\n"

    if not token:
        _audit(tcfg, cmd, "allow", reason + " (no token)", 4, tool=tool)
        return 4, "", "[tool-guard] no token available to inject\n"

    extra = None  # git only: base64 auth header, also needs scrubbing
    env = os.environ.copy()
    if tool == "gitlab":
        env["GITLAB_TOKEN"] = token
        if tcfg.get("host"):
            env["GITLAB_HOST"] = tcfg["host"]
        full = [tcfg.get("binary", "glab")] + argv
    else:  # git: inject PAT as an HTTPS Basic auth header for this call only
        extra = base64.b64encode(
            f"{tcfg.get('username', 'oauth2')}:{token}".encode()
        ).decode()
        full = [
            tcfg.get("binary", "git"),
            "-c",
            f"http.extraHeader=Authorization: Basic {extra}",
        ] + argv

    try:
        # UTF-8 with replacement: tool output is UTF-8, but Windows would decode it
        # as cp1252 and crash the reader thread.
        proc = subprocess.run(
            full,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        _audit(tcfg, cmd, "allow", reason + " (binary not found)", 4, tool=tool)
        return 4, "", f"[tool-guard] '{tcfg.get('binary')}' not found\n"

    out = _scrub(proc.stdout, token)
    err = _scrub(proc.stderr, token)
    if extra:
        out = out.replace(extra, REDACTION)
        err = err.replace(extra, REDACTION)
    _audit(tcfg, cmd, "allow", reason, proc.returncode, tool=tool)
    return proc.returncode, out or "", err or ""


def _forward_to_broker(broker_url, tool, argv):
    """Run the command via the broker and print its scrubbed result.

    The token lives in the broker's process, not this one.
    """
    import urllib.request

    payload = json.dumps({"tool": tool, "args": argv}).encode("utf-8")
    req = urllib.request.Request(
        broker_url.rstrip("/") + "/run",
        data=payload,
        headers={"Content-Type": "application/json", CLIENT_HEADER: CLIENT_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.loads(r.read())
    except Exception as e:
        sys.stderr.write(f"[tool-guard] broker unreachable at {broker_url}: {e}\n")
        return 4
    sys.stdout.write(resp.get("stdout") or "")
    sys.stderr.write(resp.get("stderr") or "")
    return int(resp.get("exit_code", 4))


def _run_cli(tool, argv, cfg=None):
    """CLI entry: broker mode if GATEWAY_BROKER set, else local env-var mode."""
    cfg = cfg or config.load()
    tcfg = cfg["tools"][tool]
    label = "glab" if tool == "gitlab" else "git"

    dry_run = False
    if "--gw-dry-run" in argv:
        dry_run = True
        argv = [a for a in argv if a != "--gw-dry-run"]

    # Broker mode: forward; token never touches this process/session.
    broker = os.environ.get("GATEWAY_BROKER")
    if broker and not dry_run:
        return _forward_to_broker(broker, tool, argv)

    checker = check_policy if tool == "gitlab" else check_git_policy
    decision, reason, cmd = checker(argv, tcfg["policy"])
    if decision == "deny":
        _audit(tcfg, cmd, "deny", reason, 3, tool=tool)
        sys.stderr.write(f"[tool-guard] DENIED: `{label} {cmd}` -> {reason}\n")
        return 3

    token = os.environ.get(tcfg["token_env"])
    if dry_run or not token:
        note = "dry-run" if dry_run else f"no token in ${tcfg['token_env']}"
        _audit(tcfg, cmd, "allow", f"{reason} ({note}, not executed)", 0, tool=tool)
        sys.stdout.write(
            f"[tool-guard] ALLOWED ({note}). Would run: {label} {' '.join(argv)}\n"
            f"[tool-guard] token source: ${tcfg['token_env']} "
            f"({'present' if token else 'MISSING'})\n"
        )
        return 0

    code, out, err = dispatch(tool, argv, token, cfg)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


def run_glab(argv, cfg=None):
    return _run_cli("gitlab", argv, cfg)


def check_git_policy(argv, policy):
    """Policy for git: block force/mirror/delete pushes; else blocklist/allowlist."""
    path = _command_path(argv)
    cmd = " ".join(path)
    sub = path[0] if path else ""
    if not sub:
        return "deny", "empty git command", cmd

    if sub == "push":
        toks = set(argv)
        if toks & _FORCE_FLAGS:
            return "deny", "force/mirror/delete push blocked", cmd
        if any(a.startswith("--force") and a != "--force-with-lease" for a in argv):
            return "deny", "force push blocked", cmd
        # A leading '+' on a refspec is a force push with no flag involved
        # (`git push origin +main:main`), so flag matching alone isn't enough.
        if any(a.startswith("+") for a in argv[1:]):
            return "deny", "force push blocked (leading '+' in refspec)", cmd
        # `:branch` with an empty source deletes the remote branch, same as --delete.
        if any(
            a.startswith(":") and len(a) > 1 and not a.startswith("-") for a in argv[1:]
        ):
            return "deny", "remote branch delete blocked (empty-source refspec)", cmd

    for d in policy.get("deny", []):
        if _matches(cmd, d):
            return "deny", f"matches deny rule '{d}'", cmd

    if policy.get("mode", "blocklist") == "allowlist":
        for a in policy.get("allow", []):
            if _matches(cmd, a):
                return "allow", f"matches allow rule '{a}'", cmd
        return "deny", "no matching allow rule (deny-by-default)", cmd

    return "allow", "not denied (blocklist mode)", cmd


def run_git(argv, cfg=None):
    return _run_cli("git", argv, cfg)


def _setup_io():
    """Force UTF-8 on stdout/stderr so non-ASCII output can't crash the write on
    Windows (cp1252 consoles). Saves needing PYTHONUTF8=1."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    _setup_io()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: glab_gw.py [--gw-dry-run] <glab args...>\n")
        return 4
    return run_glab(argv)


def main_git(argv=None):
    _setup_io()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: git_gw.py [--gw-dry-run] <git args...>\n")
        return 4
    return run_git(argv)


if __name__ == "__main__":
    sys.exit(main())
