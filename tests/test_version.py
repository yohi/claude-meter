"""Tests for version resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import claude_meter
from claude_meter.version import _REPO_ROOT, get_app_version, resolve_app_version


def _init_repo(path: Path, *, tag: str | None = None) -> None:
    """Initialize a git repository with one commit, optionally tagged.

    Args:
        path: Path to initialize as a git repository.
        tag: Optional tag to apply to the initial commit.
    """
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    # Create a dummy file and commit
    (path / "dummy.txt").write_text("test")
    subprocess.run(["git", "add", "dummy.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    if tag:
        subprocess.run(["git", "tag", tag], cwd=path, check=True, capture_output=True)


def test_resolve_app_version_returns_exact_tag_when_head_is_tagged(tmp_path: Path) -> None:
    """Test that exact tag on HEAD is returned."""
    _init_repo(tmp_path, tag="v9.9.9")
    assert resolve_app_version(tmp_path) == "v9.9.9"


def test_resolve_app_version_returns_short_hash_without_tag(tmp_path: Path) -> None:
    """Test that short commit hash is returned when no tag exists."""
    _init_repo(tmp_path)
    # Get the actual short hash
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_hash = result.stdout.strip()
    assert resolve_app_version(tmp_path) == expected_hash


def test_resolve_app_version_falls_back_to_package_version_without_git_repo(
    tmp_path: Path,
) -> None:
    """Test that package version is returned when git is unavailable."""
    # tmp_path is not a git repository, so both git commands will fail
    assert resolve_app_version(tmp_path) == claude_meter.__version__


def test_get_app_version_matches_resolve_for_real_repo_root() -> None:
    """Test that get_app_version matches resolve_app_version for the real repo."""
    assert get_app_version() == resolve_app_version(_REPO_ROOT)
