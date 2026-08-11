"""Minimal local web UI for the broker and the Director.

Launch it (needs `claude` on PATH, or GATEWAY_CLAUDE_CMD set):
    ui           (shim)   or   python scripts/ui.py

The rest happens in the browser, which opens at http://127.0.0.1:8600:
  - paste your GitLab token -> starts the broker in this process, so there's no
    second terminal and the token stays in this server's memory
  - enter a prompt and options -> runs the Director, streams output, Stop anytime

Stdlib only. Single-user, local use.

Locked to this browser tab: every state-changing request must carry a token minted
at launch and embedded in the page, come from this server's own origin, and address
a loopback Host. A run can enable edits and shell in a directory of the caller's
choosing, so without those checks any site the user had open could POST to
127.0.0.1:8600 and get code execution (see gateway/webguard.py).
"""

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import broker  # noqa: E402  (scripts/broker.py)
from gateway import webguard  # noqa: E402

UI_PORT = 8600
BROKER_PORT = 8765
CSRF_HEADER = "X-TierRoute-Token"
# New per launch, so a token from an earlier session is worthless.
CSRF_TOKEN = secrets.token_urlsafe(32)
ALLOWED_ORIGINS = (f"http://127.0.0.1:{UI_PORT}", f"http://localhost:{UI_PORT}")
_state = {"broker": None, "proc": None}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>TierRoute</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}
 h1{font-size:20px;margin:0 0 4px} h2{font-size:13px;color:#555;margin:16px 0 6px;text-transform:uppercase;letter-spacing:.05em}
 fieldset{border:1px solid #ddd;border-radius:8px;margin:12px 0;padding:12px}
 legend{font-size:12px;color:#666;padding:0 6px}
 details{border:1px solid #eee;border-radius:8px;margin:8px 0;padding:6px 12px}
 summary{cursor:pointer;font-size:13px;color:#444;font-weight:600}
 label{display:inline-block;font-size:13px;color:#444;margin:5px 10px 5px 0}
 input,select,textarea{font:inherit;padding:6px;border:1px solid #ccc;border-radius:6px}
 textarea{width:100%;box-sizing:border-box;min-height:70px}
 input[type=number]{width:76px} input[type=text],input[type=password]{width:300px}
 button{font:inherit;padding:8px 16px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
 button.stop{background:#dc2626} button:disabled{background:#9ca3af;cursor:default}
 .row{display:flex;flex-wrap:wrap;gap:6px 4px;align-items:center}
 #out{background:#0b1021;color:#d6e2ff;font:12px/1.5 Consolas,monospace;padding:12px;border-radius:8px;
      white-space:pre-wrap;height:340px;overflow:auto;margin-top:8px}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#d1d5db;margin-right:6px}
 .on{background:#16a34a} .muted{color:#888;font-size:12px}
</style></head><body>
<h1>TierRoute</h1>
<div class="muted">Macro-planner · per-step model tiering · circuit breaker · response cache</div>

<fieldset><legend>1 · Credential broker (GitLab — zero-trust)</legend>
 <div class="row"><span id="bdot" class="dot"></span><span id="bstat" class="muted">broker not started</span></div>
 <div class="row" style="margin-top:6px">
   <input type="password" id="token" placeholder="glpat-… (only needed for GitLab tasks)">
   <button id="bbtn" onclick="startBroker()">Start broker</button>
 </div>
 <div class="muted">Token stays in this server's memory only</div>
</fieldset>

<fieldset><legend>2 · Task</legend>
 <textarea id="prompt" placeholder="e.g. prepare a changes.md summarizing the current local changes"></textarea>
 <div class="row" style="margin-top:6px">
   <label>Workdir <input type="text" id="workdir" value="" placeholder="/path/to/your/repo"></label>
   <label title="enable file edits + running tests"><input type="checkbox" id="allow" checked> allow edits + run tests (-x)</label>
 </div>
</fieldset>

<details><summary>Models (tiering)</summary>
 <div class="row">
   <label>Baseline <select id="default_model"><option value="claude-opus-4-8">Opus 4.8</option>
     <option value="claude-sonnet-4-6">Sonnet 4.6</option><option value="claude-haiku-4-5-20251001">Haiku 4.5</option></select></label>
   <label>Planner <select id="planner_model"><option value="claude-sonnet-4-6">Sonnet 4.6</option>
     <option value="claude-opus-4-8">Opus 4.8</option><option value="claude-haiku-4-5-20251001">Haiku 4.5</option></select></label>
   <label>Review <select id="review_model"><option value="claude-sonnet-4-6">Sonnet 4.6</option>
     <option value="claude-opus-4-8">Opus 4.8</option><option value="claude-haiku-4-5-20251001">Haiku 4.5</option></select></label>
 </div>
 <div class="muted">Baseline = your app's model (cost comparison). Planner/Review run cheaper to save.</div>
</details>

<details><summary>Circuit breaker (cost firewall)</summary>
 <div class="row">
   <label>Max $ (hard cap)<input type="number" id="max_cost" value="5" step="0.5"></label>
   <label>Max turns<input type="number" id="max_turns" value="20"></label>
   <label>Reasoning $ (downgrade)<input type="number" id="reasoning_budget" value="3" step="0.5"></label>
   <label>Loop threshold<input type="number" id="loop_threshold" value="3"></label>
 </div>
 <div class="muted">Hard cap halts the run. Reasoning $ locks the frontier tier (premium→cheap). Loop = same tool+args N× in a row trips.</div>
</details>

<details><summary>Review &amp; retry loop</summary>
 <div class="row">
   <label><input type="checkbox" id="review" checked> review at end</label>
   <label>Max rounds<input type="number" id="max_rounds" value="3"></label>
   <label>Min confidence<input type="number" id="min_confidence" value="0.7" step="0.05" min="0" max="1"></label>
 </div>
 <div class="muted">Reviewer verifies the work; if issues/low-confidence, it replans &amp; fixes up to Max rounds.</div>
</details>

<details><summary>Response cache</summary>
 <div class="row">
   <label><input type="checkbox" id="cache" checked> cache on</label>
   <label><input type="checkbox" id="clear_cache"> clear cache first</label>
   <label>Semantic threshold<input type="number" id="cache_threshold" value="0.97" step="0.01" min="0" max="1.1"></label>
   <label>TTL (s)<input type="number" id="cache_ttl" value="0"></label>
 </div>
 <div class="muted">Repeated plan/steps are served from cache (0 tokens). Clear it if the repo changed. TTL 0 = never expire.</div>
</details>

<fieldset><legend>3 · Run</legend>
 <div class="row">
   <button id="rbtn" onclick="run()">Run</button>
   <button id="sbtn" class="stop" onclick="stop()" disabled>Stop</button>
 </div>
</fieldset>

<h2>Output</h2>
<div id="out"></div>

<script>
const out=document.getElementById('out');
let controller=null;
const $=id=>document.getElementById(id);
// Minted per launch and only ever present in this page; a cross-origin caller
// cannot read it, so it cannot forge a POST below.
const CSRF='__CSRF_TOKEN__';
const post=(url,body,extra)=>fetch(url,Object.assign({method:'POST',
  headers:{'X-TierRoute-Token':CSRF},body:body},extra||{}));
async function refresh(){
  const s=await (await fetch('/status')).json();
  $('bdot').className='dot'+(s.broker?' on':'');
  $('bstat').textContent=s.broker?'broker running (token loaded)':'broker not started';
}
async function startBroker(){
  const t=$('token').value.trim(); if(!t){alert('paste a token');return;}
  $('bbtn').disabled=true;
  await post('/broker/start',JSON.stringify({token:t}));
  $('token').value=''; $('bbtn').disabled=false; refresh();
}
function collect(){
  const num=id=>$(id).value;
  return {prompt:$('prompt').value, workdir:$('workdir').value, allow:$('allow').checked,
    default_model:$('default_model').value, planner_model:$('planner_model').value, review_model:$('review_model').value,
    max_cost:num('max_cost'), max_turns:num('max_turns'), reasoning_budget:num('reasoning_budget'),
    loop_threshold:num('loop_threshold'), review:$('review').checked, max_rounds:num('max_rounds'),
    min_confidence:num('min_confidence'), cache:$('cache').checked, clear_cache:$('clear_cache').checked,
    cache_threshold:num('cache_threshold'), cache_ttl:num('cache_ttl')};
}
async function run(){
  out.textContent=''; $('rbtn').disabled=true; $('sbtn').disabled=false;
  controller=new AbortController();
  try{
    const resp=await post('/run',JSON.stringify(collect()),{signal:controller.signal});
    const reader=resp.body.getReader(); const dec=new TextDecoder();
    while(true){const {done,value}=await reader.read(); if(done)break;
      out.textContent+=dec.decode(value); out.scrollTop=out.scrollHeight;}
  }catch(e){ out.textContent+='\\n[ui] run stopped.\\n'; }
  $('rbtn').disabled=false; $('sbtn').disabled=true;
}
async function stop(){
  if(controller)controller.abort();
  await post('/stop');
  out.textContent+='\\n[ui] stop requested.\\n';
  $('rbtn').disabled=false; $('sbtn').disabled=true;
}
refresh();
</script></body></html>"""


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
            )
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


class UI(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or "{}")
        except Exception:
            return {}

    def _refuse(self, require_token=False):
        """Send 403 and return True if a web page could be driving this request."""
        reason = webguard.check(
            self.headers,
            allowed_origins=ALLOWED_ORIGINS,
            client_header=CSRF_HEADER if require_token else None,
            client_token=CSRF_TOKEN if require_token else None,
        )
        if reason:
            self._json(403, {"error": f"refused: {reason}"})
            return True
        return False

    def do_OPTIONS(self):
        # No Allow-* headers: a cross-origin preflight fails here, so the custom
        # header that POSTs require can never be delivered from another origin.
        self._json(405, {"error": "preflight not supported"})

    def do_GET(self):
        if self._refuse():
            return
        if self.path == "/" or self.path.startswith("/index"):
            b = PAGE.replace("__CSRF_TOKEN__", CSRF_TOKEN).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/status":
            self._json(
                200,
                {
                    "broker": _state["broker"] is not None and broker.token_loaded(),
                    "running": _state["proc"] is not None,
                },
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self._refuse(require_token=True):
            return
        if self.path == "/broker/start":
            token = (self._body().get("token") or "").strip()
            if not token:
                return self._json(400, {"error": "no token"})
            if _state["broker"] is None:
                _state["broker"] = broker.start_broker(token, port=BROKER_PORT)
            else:
                broker._TOKEN = token
            return self._json(200, {"broker": True})
        if self.path == "/stop":
            proc = _state.get("proc")
            if proc:
                _kill_tree(proc)
            return self._json(200, {"stopped": True})
        if self.path == "/run":
            return self._stream_run(self._body())
        self._json(404, {"error": "not found"})

    def _stream_run(self, b):
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "orchestrator.run",
            b.get("prompt", ""),
            "--workdir",
            b.get("workdir", ""),
        ]
        if b.get("backend"):
            cmd += ["--backend", b["backend"]]
        if b.get("allow"):
            cmd.append("-x")
        for flag, key in (
            ("--default-model", "default_model"),
            ("--planner-model", "planner_model"),
            ("--review-model", "review_model"),
        ):
            if b.get(key):
                cmd += [flag, b[key]]
        for flag, key in (
            ("--max-cost", "max_cost"),
            ("--max-turns", "max_turns"),
            ("--reasoning-budget", "reasoning_budget"),
            ("--loop-threshold", "loop_threshold"),
            ("--max-rounds", "max_rounds"),
            ("--min-confidence", "min_confidence"),
            ("--cache-threshold", "cache_threshold"),
            ("--cache-ttl", "cache_ttl"),
        ):
            v = str(b.get(key, "")).strip()
            if v:
                cmd += [flag, v]
        if b.get("review") is False:
            cmd.append("--no-review")
        if not b.get("cache", True):
            cmd.append("--no-cache")
        if b.get("clear_cache"):
            cmd.append("--clear-cache")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

        env = os.environ.copy()
        env["GATEWAY_BROKER"] = f"http://127.0.0.1:{BROKER_PORT}"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=REPO,
                env=env,
                bufsize=1,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            self.wfile.write(f"[ui] failed to launch: {e}\n".encode("utf-8"))
            return
        _state["proc"] = proc

        # A background thread reads the subprocess; the main loop drains the queue
        # and sends a heartbeat during silent steps, so the connection never idles
        # out while a `claude -p` step runs quietly for 30-60s.
        q = queue.Queue()

        def _reader():
            try:
                for line in proc.stdout:
                    q.put(line)
            finally:
                q.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        try:
            while True:
                try:
                    item = q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b".")  # heartbeat / "working"
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                self.wfile.write(item.encode("utf-8", "replace"))
                self.wfile.flush()
            proc.wait()
            self.wfile.write(
                f"\n[ui] run finished (exit {proc.returncode}).\n".encode("utf-8")
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _kill_tree(proc)
        finally:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            _state["proc"] = None

    def log_message(self, *a):
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", UI_PORT), UI)
    url = f"http://127.0.0.1:{UI_PORT}"
    print(f"[ui] serving on {url}  (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[ui] shutting down")


if __name__ == "__main__":
    main()
