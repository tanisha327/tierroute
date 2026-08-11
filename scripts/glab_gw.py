"""Entry point: credential-injected, policy-guarded `glab`.

Have Claude run this INSTEAD of `glab`:
    python scripts/glab_gw.py mr list

The real token is read from $GATEWAY_GITLAB_TOKEN on the server side and injected
into glab's environment only. See gateway/toolguard.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.toolguard import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
