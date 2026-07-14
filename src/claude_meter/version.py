"""Version resolution for the running application.

Resolves the application version by checking:
1. Exact git tag on HEAD (e.g., "v0.7.1")
2. Short commit hash if no exact tag (e.g., "f40f99a")
3. Static package version from __version__ as fallback

This ensures the UI displays the actual deployed version, whether running from
a tagged release or a development commit.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from claude_meter import __version__ as _PACKAGE_VERSION

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_git(repo_root: Path, *args: str) -> str | None:
    """Execute a git command in the given repository.

    Args:
        repo_root: Path to the git repository root.
        *args: Git command arguments (e.g., "describe", "--tags", "--exact-match", "HEAD").

    Returns:
        The stripped stdout if successful, None if git is unavailable or the command fails.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
        output = result.stdout.strip()
        return output if output else None
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_app_version(repo_root: Path) -> str:
    """Resolve the application version from git or fallback to package version.

    Args:
        repo_root: Path to the git repository root.

    Returns:
        The version string: exact tag, short commit hash, or package version.
    """
    # Try exact tag on HEAD
    tag = _run_git(repo_root, "describe", "--tags", "--exact-match", "HEAD")
    if tag:
        return tag

    # Try short commit hash
    short_hash = _run_git(repo_root, "rev-parse", "--short", "HEAD")
    if short_hash:
        return short_hash

    # Fallback to package version
    return _PACKAGE_VERSION


@functools.lru_cache(maxsize=1)
def get_app_version() -> str:
    """Get the cached application version for the running repository.

    Returns:
        The version string from resolve_app_version(_REPO_ROOT).
    """
    return resolve_app_version(_REPO_ROOT)
