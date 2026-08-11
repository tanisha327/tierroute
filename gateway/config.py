"""Configuration for the Zero-Trust tool guard.

config.json (if present) is merged over the DEFAULTS below. Point GATEWAY_CONFIG
at another file to use it instead.
"""

import copy
import json
import os

# Parent of this package. Anchors config.json and relative paths so they resolve
# the same no matter what the working directory is.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "tools": {
        "gitlab": {
            "binary": "glab",  # bare name if on PATH, else an absolute path
            "token_env": "GATEWAY_GITLAB_TOKEN",  # where the PAT is read from
            "host": "gitlab.com",  # hostname only, no scheme
            "audit_log": "data/tool_audit.log",
            "policy": {
                "mode": "allowlist",  # "allowlist" | "blocklist"
                "allow": [
                    "auth status",
                    "api",
                    "mr list",
                    "mr view",
                    "mr diff",
                    "mr create",
                    "mr update",  # writes: open/update MRs (enabled)
                    "issue list",
                    "issue view",
                    "repo view",
                    "repo clone",
                    "ci list",
                    "ci view",
                    "ci status",
                    "release list",
                    "release view",
                ],
                "deny": [  # always blocked
                    "repo delete",
                    "repo archive",
                    "mr merge",
                    "mr delete",
                    "mr close",
                    "issue delete",
                    "issue create",
                    "issue close",
                    "issue update",
                    "release delete",
                    "release create",
                    "auth login",
                    "auth logout",
                ],
                "api_allowed_methods": ["GET"],  # `glab api` restricted to these
            },
        },
        # Guarded `git` for pushing commits, which glab can't do. The token goes in
        # as an HTTPS auth header; force-push, mirror and branch-delete are blocked.
        "git": {
            "binary": "git",
            "token_env": "GATEWAY_GITLAB_TOKEN",
            "username": "oauth2",  # GitLab: user=oauth2, pass=PAT
            "audit_log": "data/tool_audit.log",
            "policy": {
                "mode": "blocklist",  # git is mostly local/safe
                "allow": [],
                "deny": ["push --mirror"],  # + force/delete blocked in code
            },
        },
    },
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    # Resolve config.json against the repo root, not the CWD, so it's found wherever
    # the broker/CLI was launched from. Override with $GATEWAY_CONFIG or `path`.
    if path is None:
        path = os.environ.get("GATEWAY_CONFIG") or os.path.join(
            _REPO_ROOT, "config.json"
        )
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, json.load(f))
    # Same for relative audit_log paths.
    for tconf in cfg.get("tools", {}).values():
        al = tconf.get("audit_log")
        if al and not os.path.isabs(al):
            tconf["audit_log"] = os.path.join(_REPO_ROOT, al)
    return cfg
