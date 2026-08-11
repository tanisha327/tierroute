"""Zero-Trust tool guard.

A credential-isolating wrapper around the `glab` and `git` CLIs:
  - injects the real GitLab token into the tool's subprocess just-in-time
  - enforces a deny-by-default command policy
  - scrubs the token from all output
  - audits every call
The agent/LLM never sees the token.
"""

__version__ = "0.1.0"
