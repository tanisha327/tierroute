"""Entry point: credential-injected, policy-guarded `git`.

Have Claude run this INSTEAD of `git` for commands that talk to GitLab
(push/pull/fetch/clone). Local commands work too:
    python scripts/git_gw.py push origin my-branch

The real token is read from $GATEWAY_GITLAB_TOKEN on the server side and injected
as an HTTPS auth header for that one invocation. Force/mirror/delete pushes are
blocked. See gateway/toolguard.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.toolguard import main_git  # noqa: E402

if __name__ == "__main__":
    sys.exit(main_git())
