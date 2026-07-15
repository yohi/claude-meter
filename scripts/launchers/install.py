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

The repository root is substituted into each launcher's *executable* shell/cmd
context (a ``cd``/``bash -c`` argument), so it is escaped per destination
format (POSIX shell single-quoting for ``.command``, ``cmd.exe`` double-quoting
for ``.bat``, and bash double-quote escaping for the ``.desktop`` ``Exec=``
line) before substitution. This prevents a repository path containing quotes,
backslashes, or ``$``/backtick command-substitution characters from breaking or
hijacking the generated launcher. A handful of pathological characters (a
literal single quote for ``.desktop``, a literal double quote for ``.bat``, or
any line break) cannot be safely represented in the destination format at all
and are rejected outright with ``ValueError`` rather than silently producing a
broken or exploitable launcher.
"""

from __future__ import annotations

import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = "__REPO_ROOT__"
# Used only inside the .desktop template's Exec= line, where the repository
# root must be escaped for bash's double-quote context (see
# _escape_bash_double_quoted); the plain PLACEHOLDER above is used for the
# Icon= line, which is an opaque string with no shell involved.
SHELL_ARG_PLACEHOLDER = "__REPO_ROOT_SHELL_ARG__"
LAUNCHER_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]

_BASH_DQ_SPECIAL_RE = re.compile(r'([\\"$`])')


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


def _reject_line_breaks(repo_root: Path) -> str:
    """Return ``repo_root`` as ``str``, refusing paths that would corrupt the
    line-oriented launcher formats (``.desktop``, ``.bat``) or otherwise make
    correct quoting impossible."""
    text = str(repo_root)
    if "\n" in text or "\r" in text:
        raise ValueError(
            "Repository root path contains a line break and cannot be safely "
            f"embedded in a launcher file: {text!r}"
        )
    return text


def _escape_posix_shell(value: str) -> str:
    """Return ``value`` as a single POSIX shell word (quotes included), safe
    to splice into a shell command line unquoted. Single quotes protect every
    character except a literal single quote itself, which is closed out of and
    reopened around (the standard ``'\\''`` trick)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _escape_cmd_bat(value: str) -> str:
    """Return ``value`` as a double-quoted ``cmd.exe`` argument for a ``.bat``
    file. ``%`` is doubled so a literal ``%`` in the path (e.g. a directory
    literally named ``%TEMP%``) cannot trigger batch variable expansion.
    ``"`` cannot legally appear in a Windows path, and cmd.exe has no reliable
    in-quote escape for it, so it is rejected rather than silently mishandled."""
    if '"' in value:
        raise ValueError(
            "Repository root path contains a double quote, which cannot be "
            f"safely embedded in a Windows batch file: {value!r}"
        )
    return '"' + value.replace("%", "%%") + '"'


def _escape_bash_double_quoted(value: str) -> str:
    """Escape characters that are special inside a POSIX/bash double-quoted
    string (``\\``, ``"``, ``$``, `````) so ``value`` can be embedded literally
    inside a ``cd "..."`` argument without triggering command substitution or
    ending the string early. The result is then embedded inside the outer
    *single*-quoted ``bash -c '...'`` argument of the .desktop template's
    Exec= line, which single quotes protect verbatim -- except from a literal
    single quote, which has no in-quote escape in the Desktop Entry
    Specification's Exec grammar and is therefore rejected outright."""
    if "'" in value:
        raise ValueError(
            "Repository root path contains a single quote, which cannot be "
            f"safely embedded in the generated .desktop launcher: {value!r}"
        )
    return _BASH_DQ_SPECIAL_RE.sub(r"\\\1", value)


def render_template(template_path: Path, repo_root: Path) -> str:
    """Read ``template_path`` and substitute the repository-root placeholder(s),
    escaping the value for the destination format so that paths containing
    quotes, backslashes, or ``$``/backtick command-substitution characters
    cannot break or hijack the generated launcher (see module docstring)."""
    text = template_path.read_text(encoding="utf-8")
    repo_root_str = _reject_line_breaks(repo_root)
    # Path.stem strips only the trailing ".tmpl", leaving e.g. "claude-meter.desktop";
    # .suffix on that then yields ".desktop", ".bat", or ".command".
    kind = Path(template_path.stem).suffix

    if kind == ".desktop":
        # Icon= is a plain opaque string (no shell involved): substitute raw.
        text = text.replace(PLACEHOLDER, repo_root_str)
        # Exec= embeds the value inside a bash double-quoted `cd "..."`
        # argument (see _escape_bash_double_quoted).
        shell_arg = _escape_bash_double_quoted(repo_root_str)
        return text.replace(SHELL_ARG_PLACEHOLDER, shell_arg)
    if kind == ".bat":
        return text.replace(PLACEHOLDER, _escape_cmd_bat(repo_root_str))
    # .command (and anything else): a plain POSIX shell script.
    return text.replace(PLACEHOLDER, _escape_posix_shell(repo_root_str))


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
