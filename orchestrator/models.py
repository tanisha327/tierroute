"""Model tiers and pricing.

A tier is the capability level the planner tags each step with; it maps to a
concrete Claude model id here. Prices are USD per million tokens, prefix-matched
so dated model ids resolve.
"""

import os

# tier -> model id. Override per environment (not every deployment has every
# model) via GATEWAY_MODEL_CHEAP / _STANDARD / _PREMIUM.
TIERS = {
    "cheap": os.environ.get("GATEWAY_MODEL_CHEAP", "claude-haiku-4-5"),
    "standard": os.environ.get("GATEWAY_MODEL_STANDARD", "claude-sonnet-4-6"),
    "premium": os.environ.get("GATEWAY_MODEL_PREMIUM", "claude-opus-4-8"),
}

# aliases the planner might emit
_ALIASES = {
    "flash": "cheap",
    "haiku": "cheap",
    "small": "cheap",
    "sonnet": "standard",
    "mid": "standard",
    "medium": "standard",
    "opus": "premium",
    "frontier": "premium",
    "large": "premium",
    "reasoning": "premium",
}

# model-id-prefix -> (input $/Mtok, output $/Mtok)
PRICES = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.80, 4.0),
}
_DEFAULT_PRICE = (3.0, 15.0)


# Short alias -> model id. Always pass the concrete id to `claude --model`: a short
# alias like "haiku" can be re-resolved to a dated version that isn't deployed.
_ALIAS_TO_ID = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}


def resolve_tier(tier):
    """Map a tier name/alias to a concrete model id. Unknown -> standard."""
    t = (tier or "standard").strip().lower()
    t = _ALIASES.get(t, t)
    return TIERS.get(t, TIERS["standard"])


def to_cli(model):
    """Model id for `claude --model`. Tiers and aliases resolve; full ids pass through."""
    m = (model or "").strip()
    if m in TIERS:  # cheap/standard/premium
        return TIERS[m]
    if m in _ALIASES:  # flash/mid/frontier/…
        return TIERS[_ALIASES[m]]
    if m in _ALIAS_TO_ID:  # haiku/sonnet/opus
        return _ALIAS_TO_ID[m]
    return m


def rate_for(model):
    model = model or ""
    best = None
    for prefix, rate in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), rate)
    return best[1] if best else _DEFAULT_PRICE


def cost(model, usage):
    usage = usage or {}
    inp, out = rate_for(model)
    return ((usage.get("input_tokens", 0) or 0) / 1e6) * inp + (
        (usage.get("output_tokens", 0) or 0) / 1e6
    ) * out
