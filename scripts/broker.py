"""Credential broker — holds the token in a separate process.

Run in its own terminal, ideally as a separate OS user (see README). It keeps the
GitLab PAT in its own memory and exposes a small localhost API. Claude's session
reaches it through the guard shims (GATEWAY_BROKER=...) and gets back only
policy-checked, scrubbed output — never the token.

Token source (pick one):
    python scripts/broker.py                       # prompt; memory only
    python scripts/broker.py --token-env NAME      # from the broker's own env
    python scripts/broker.py --port 8765

Security:
  - Binds to 127.0.0.1 by default (--host can widen this; don't, unless you have
    another control in front of it — there is no user authentication here).
  - Same deny-by-default policy as the CLI guard.
  - Accepts only {"tool","args"} and runs it via gateway.toolguard.dispatch. No
    endpoint returns the token or runs arbitrary commands.
  - Not reachable from a web page: /run requires the X-TierRoute-Client header
    (which a browser can only set cross-origin after a CORS preflight, and
    preflights are refused), rejects any request carrying an Origin, and requires
    a loopback Host. Without this, any site the user visited could POST to
    127.0.0.1:8765 and spend the token.
"""

import argparse
import getpass
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import config, toolguard, webguard  # noqa: E402

# Token is captured at startup and kept only in this module-level variable.
_TOKEN = None
_CFG = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, client_header=False):
        """Send 403 and return True if a web page could be driving this request."""
        reason = webguard.check(
            self.headers,
            allowed_origins=(),  # no browser front end: every Origin is refused
            client_header=toolguard.CLIENT_HEADER if client_header else None,
            client_token=toolguard.CLIENT_TOKEN if client_header else None,
        )
        if reason:
            self._send(403, {"error": f"refused: {reason}"})
            return True
        return False

    def do_OPTIONS(self):
        # Refuse CORS preflights with no Allow-* headers, so a cross-origin caller
        # can never reach /run with the custom header it requires.
        self._send(405, {"error": "preflight not supported"})

    def do_GET(self):
        if self.path == "/health":
            if self._refuse():
                return
            self._send(200, {"status": "ok", "token_loaded": bool(_TOKEN)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        if self._refuse(client_header=True):
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n))
        except Exception:
            return self._send(400, {"error": "invalid JSON"})

        tool = req.get("tool")
        args = req.get("args") or []
        if tool not in ("gitlab", "git") or not isinstance(args, list):
            return self._send(
                400, {"error": "expected {tool: gitlab|git, args: [...]}"}
            )

        # The token is injected here, inside the broker, and never returned.
        code, out, err = toolguard.dispatch(tool, args, _TOKEN, _CFG)
        self._send(200, {"exit_code": code, "stdout": out, "stderr": err})

    def log_message(self, *a):  # keep the broker quiet
        pass


def start_broker(token, host="127.0.0.1", port=8765):
    """Start the broker on a background thread and return the server. Used by the
    UI; the token stays in this process's memory."""
    global _TOKEN, _CFG
    _TOKEN = token
    _CFG = config.load()
    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def token_loaded():
    return bool(_TOKEN)


def main():
    global _TOKEN, _CFG
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--token-env",
        default=None,
        help="read the token from this env var of the broker process",
    )
    args = ap.parse_args()

    _CFG = config.load()
    if args.token_env:
        _TOKEN = os.environ.get(args.token_env)
        if not _TOKEN:
            sys.stderr.write(f"[broker] ${args.token_env} is empty; aborting\n")
            return 4
        src = f"env ${args.token_env}"
    else:
        _TOKEN = getpass.getpass("Paste GitLab PAT (kept in memory only): ").strip()
        if not _TOKEN:
            sys.stderr.write("[broker] no token entered; aborting\n")
            return 4
        src = "interactive prompt"

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[broker] listening on http://{args.host}:{args.port}  (token from {src})")
    print(f"[broker] point clients at:  GATEWAY_BROKER=http://{args.host}:{args.port}")
    print("[broker] Ctrl+C to stop. The token is only in this process's memory.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[broker] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
