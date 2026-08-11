"""Response cache for the Director.

Stores the result of the planner call and of each step, keyed by the request. A hit
skips the `claude -p` spawn entirely — 0 tokens, $0. Persisted to disk so savings
carry across runs.

  Tier 1 — exact:    SHA-256 of (kind + model + request text), O(1)
  Tier 2 — semantic: near-identical request text via a small local embedder
                     (cosine >= threshold), namespaced so kinds never cross.

Dependency-free. Cached results go stale if the repo changed since they were
stored — use --clear-cache or a TTL for a fresh run.
"""

import hashlib
import json
import math
import os
import re
import threading
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIM = 512
_WORD = re.compile(r"[a-z0-9]+")


def make_key(*parts):
    return hashlib.sha256(
        "\x1f".join(p or "" for p in parts).encode("utf-8")
    ).hexdigest()


def _embed(text):
    words = _WORD.findall((text or "").lower())
    feats = list(words) + [words[i] + "_" + words[i + 1] for i in range(len(words) - 1)]
    compact = " ".join(words)
    feats += [compact[i : i + 4] for i in range(len(compact) - 3)]
    vec = [0.0] * _DIM
    for f in feats:
        h = int(hashlib.md5(f.encode("utf-8")).hexdigest(), 16)
        vec[h % _DIM] += 1.0 if (h // _DIM) % 2 == 0 else -1.0
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n else vec


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


class DirectorCache:
    def __init__(self, path=None, enabled=True, threshold=0.97, ttl=0):
        self.enabled = enabled
        self.threshold = threshold
        self.ttl = ttl  # seconds; 0 = never expire
        self.path = path or os.path.join(_REPO, "data", "director_cache.json")
        self._lock = threading.Lock()
        self._exact = {}  # key -> entry
        self._sem = {}  # namespace -> [entry(+vec)]
        self.stats = {"hits": 0, "misses": 0, "tokens_saved": 0, "cost_saved": 0.0}
        self._load()

    def _fresh(self, e):
        return not self.ttl or (time.time() - e.get("created", 0)) <= self.ttl

    def _load(self):
        if not self.enabled or not os.path.exists(self.path):
            return
        try:
            data = json.load(open(self.path, encoding="utf-8"))
        except Exception:
            return
        for e in data.get("entries", []):
            self._exact[e["key"]] = e
            if e.get("text"):
                ev = dict(e)
                ev["vec"] = _embed(e["text"])
                self._sem.setdefault(e.get("ns", ""), []).append(ev)

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        slim = [
            {
                k: e[k]
                for k in (
                    "key",
                    "ns",
                    "text",
                    "kind",
                    "model",
                    "result",
                    "usage",
                    "cost",
                    "created",
                )
            }
            for e in self._exact.values()
        ]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": slim}, f)
        os.replace(tmp, self.path)

    def _record_hit(self, e, how):
        self.stats["hits"] += 1
        u = e.get("usage") or {}
        self.stats["tokens_saved"] += (u.get("input_tokens", 0) or 0) + (
            u.get("output_tokens", 0) or 0
        )
        self.stats["cost_saved"] += e.get("cost", 0.0) or 0.0
        return {"entry": e, "how": how}

    def get(self, key, ns="", text=""):
        """Return {'entry','how'} on a hit, else None (and count a miss)."""
        if not self.enabled:
            return None
        with self._lock:
            e = self._exact.get(key)
            if e and self._fresh(e):
                return self._record_hit(e, "exact")
            if text and self.threshold <= 1.0:
                qv = _embed(text)
                best, score = None, 0.0
                for cand in self._sem.get(ns, []):
                    if not self._fresh(cand):
                        continue
                    s = _cos(qv, cand["vec"])
                    if s > score:
                        best, score = cand, s
                if best and score >= self.threshold:
                    return self._record_hit(best, f"semantic:{score:.3f}")
            self.stats["misses"] += 1
            return None

    def put(self, key, kind, model, result, usage, cost, ns="", text=""):
        if not self.enabled:
            return
        with self._lock:
            e = {
                "key": key,
                "ns": ns,
                "text": text,
                "kind": kind,
                "model": model,
                "result": result,
                "usage": usage,
                "cost": cost,
                "created": time.time(),
            }
            self._exact[key] = e
            if text:
                ev = dict(e)
                ev["vec"] = _embed(text)
                self._sem.setdefault(ns, []).append(ev)
            self._save()

    def clear(self):
        with self._lock:
            self._exact.clear()
            self._sem.clear()
            self._save()

    def snapshot(self):
        s = dict(self.stats)
        s["cost_saved"] = round(s["cost_saved"], 6)
        s["entries"] = len(self._exact)
        return s
