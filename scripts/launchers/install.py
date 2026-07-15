"""Cross-platform installer for the claude-meter desktop launcher.

This script renders the operating-system-appropriate launcher template shipped
under ``scripts/launchers/`` (Linux ``.desktop``, macOS ``.command``, or Windows
``.bat``), substitutes the ``__REPO_ROOT__`` placeholder with the absolute path
of this repository, and writes the result to a location where it can be
double-clicked:

* Linux:   ``~/.local/share/applications/claude-meter.desktop``
* macOS:   ``~/Desktop/claude-meter.command`` (marked executable)
* Windows: ``%USERPROFILE%\\Desktop\\claude-meter.bat``

Run it directly with ``python scripts/launchers/install.py``. It only relies on
the Python standard library and never hard-codes machine-specific absolute
paths: the repository root is resolved dynamically at run time.
"""

from __future__ import annotations

import stat
import sys
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = "__REPO_ROOT__"
LAUNCHER_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LauncherPlan:
    """Describes how to install the launcher for a single operating system."""

    template_name: str
    output_name: str
    destination_dir: Path
    make_executable: bool
    icon_hint: str


def build_plan(platform: str, home: Path) -> LauncherPlan:
    """Return the launcher plan for the given ``sys.platform`` value.

    Any platform other than ``"win32"`` or ``"darwin"`` is treated as Linux.
    """
    if platform == "win32":
        return LauncherPlan(
            template_name="claude-meter.bat.tmpl",
            output_name="claude-meter.bat",
            destination_dir=home / "Desktop",
            make_executable=False,
            icon_hint=(
                "Windows: right-click the .bat to create a shortcut, then open "
                "Properties -> Change Icon and pick a .ico file."
            ),
        )
    if platform == "darwin":
        return LauncherPlan(
            template_name="claude-meter.command.tmpl",
            output_name="claude-meter.command",
            destination_dir=home / "Desktop",
            make_executable=True,
            icon_hint=(
                "macOS: select the .command file in Finder, press Cmd+I, then "
                "drag an image onto the icon in the Info window's title bar."
            ),
        )
    return LauncherPlan(
        template_name="claude-meter.desktop.tmpl",
        output_name="claude-meter.desktop",
        destination_dir=home / ".local" / "share" / "applications",
        make_executable=False,
        icon_hint=(
            "Linux: the .desktop entry already references assets/icon.png, so "
            "the icon is used automatically once that file exists."
        ),
    )


def render_template(template_path: Path, repo_root: Path) -> str:
    """Read ``template_path`` and substitute the repository-root placeholder."""
    text = template_path.read_text(encoding="utf-8")
    return text.replace(PLACEHOLDER, str(repo_root))


def install_launcher(plan: LauncherPlan, repo_root: Path, launcher_dir: Path) -> Path:
    """Render and write the launcher described by ``plan``; return its path."""
    template_path = launcher_dir / plan.template_name
    rendered = render_template(template_path, repo_root)

    plan.destination_dir.mkdir(parents=True, exist_ok=True)
    output_path = plan.destination_dir / plan.output_name
    output_path.write_text(rendered, encoding="utf-8")

    if plan.make_executable:
        current_mode = output_path.stat().st_mode
        output_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return output_path


def main() -> None:
    """Install the launcher for the current platform and print guidance."""
    plan = build_plan(sys.platform, Path.home())
    output_path = install_launcher(plan, REPO_ROOT, LAUNCHER_DIR)
    icon_path = REPO_ROOT / "assets" / "icon.png"

    print(f"Installed claude-meter launcher: {output_path}")
    print(f"Repository root: {REPO_ROOT}")
    print(f"Suggested icon location: {icon_path}")
    print(plan.icon_hint)
    print(
        "Make sure the `claude-meter` command is on your PATH first "
        "(e.g. `pip install -e .` or `uv tool install .`)."
    )


if __name__ == "__main__":
    main()
