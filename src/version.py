"""Application version and revision for health checks."""

import os
import subprocess
from functools import lru_cache

from src.config import PROJECT_ROOT

APP_VERSION = "0.1.0"


@lru_cache
def build_info() -> dict[str, str]:
    commit = os.getenv("APP_GIT_COMMIT")
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True,
                stderr=subprocess.DEVNULL, timeout=2,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            commit = "unknown"
    return {"version": APP_VERSION, "git_commit": commit}
