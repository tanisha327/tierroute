"""Launcher for the Macro-Planner + Model Director from anywhere.

Adds the repo root to sys.path so `orchestrator` is importable regardless of CWD,
then delegates to orchestrator.run.main. Use --workdir to point it at a target repo.

    python scripts/director.py "task" \
        --workdir /path/to/your/repo --default-model claude-opus-4-8
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.run import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
