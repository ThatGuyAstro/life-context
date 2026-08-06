from __future__ import annotations

import sys
from pathlib import Path


# The parent project is intentionally an empty namespace worktree in this
# agent lane, so pytest may choose the test directory as its import root.
# Make the repository root explicit for the package import used by these
# tests without adding package files outside the owned directories.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

