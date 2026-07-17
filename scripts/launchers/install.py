"""Cross-platform installer for the claude-meter desktop launcher.

This script renders the operating-system-appropriate launcher template shipped
under ``scripts/launchers/`` (Linux ``.desktop``, macOS ``.app`` bundle, or Windows
``.vbs``), substitutes the ``__REPO_ROOT__`` placeholder with the absolute path
of this repository and the ``__CLAUDE_METER_PATH__`` placeholder with the
absolute path of the ``claude-meter`` executable, and writes the result to a
location where it can be double-clicked:

* Linux:   ``~/.local/share/applications/claude-meter.desktop``
* macOS:   ``~/Desktop/claude-meter.app`` (bundle with executable)
* Windows: ``%USERPROFILE%\\Desktop\\claude-meter.vbs``

Run it directly with ``python scripts/launchers/install.py``. It only relies on
the Python standard library and never hard-codes machine-specific absolute
paths: the repository root and command path are resolved dynamically at run
time.

The repository root is substituted into each launcher's *executable* shell/cmd
context (a ``cd``/``bash -c`` argument or a WScript ``CurrentDirectory``
assignment), so it is escaped per destination format (POSIX shell
single-quoting for ``.command``, ``cmd.exe`` double-quoting for ``.bat``,
VBScript double-quoting for ``.vbs``, and bash double-quote escaping for the
``.desktop`` ``Exec=`` line) before substitution. This prevents a repository path
containing quotes, backslashes, or ``$``/backtick command-substitution characters
from breaking or hijacking the generated launcher. A handful of pathological
characters (a literal single quote for ``.desktop``, a literal double quote for
``.bat``/``.vbs``, or any line break) cannot be safely represented in the
destination format at all and are rejected outright with ``ValueError`` rather
than silently producing a broken or exploitable launcher.

The ``claude-meter`` executable path is resolved with ``shutil.which`` so that
GUI launches do not depend on the shell's ``PATH`` being inherited.
"""

from __future__ import annotations

import re
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = "__REPO_ROOT__"
CMD_PLACEHOLDER = "__CLAUDE_METER_PATH__"
VERSION_PLACEHOLDER = "__VERSION__"
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
    debug_template_name: str | None = None
    debug_output_name: str | None = None
    debug_make_executable: bool = False
    normal_bundle: bool = False


def build_plan(platform: str, home: Path) -> LauncherPlan:
    """Return the launcher plan for the given ``sys.platform`` value.

    Any platform other than ``"win32"`` or ``"darwin"`` is treated as Linux.
    """
    if platform == "win32":
        return LauncherPlan(
            template_name="claude-meter.vbs.tmpl",
            output_name="claude-meter.vbs",
            destination_dir=home / "Desktop",
            make_executable=False,
            debug_template_name="claude-meter-debug.bat.tmpl",
            debug_output_name="claude-meter-debug.bat",
            icon_hint=(
                "Windows: run claude-meter.vbs normally; use "
                "claude-meter-debug.bat for a visible console. To set an icon, "
                "create a shortcut and open "
                "Properties -> Change Icon and pick a .ico file."
            ),
        )
    if platform == "darwin":
        return LauncherPlan(
            template_name="claude-meter.app/Contents/MacOS/claude-meter.tmpl",
            output_name="claude-meter.app",
            destination_dir=home / "Desktop",
            make_executable=True,
            debug_template_name="claude-meter-debug.command.tmpl",
            debug_output_name="claude-meter-debug.command",
            debug_make_executable=True,
            normal_bundle=True,
            icon_hint=(
                "macOS: double-click claude-meter.app normally; use "
                "claude-meter-debug.command for a visible Terminal."
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


def _escape_vbs_string(value: str) -> str:
    """Return ``value`` as a double-quoted VBScript string literal.

    Embedded double quotes are escaped by doubling them (``""``), which is
    the VBScript convention. Other characters that cannot be represented
    safely in a VBScript string literal are rejected outright."""
    if '\n' in value or '\r' in value:
        raise ValueError(
            "Path contains a line break and cannot be safely embedded in a "
            f"VBScript string literal: {value!r}"
        )
    return '"' + value.replace('"', '""') + '"'


def render_template(template_path: Path, repo_root: Path, cmd_path: str, version: str) -> str:
    """Read ``template_path`` and substitute the repository-root and command
    placeholders, escaping the values for the destination format so that paths
    containing quotes, backslashes, or ``$``/backtick command-substitution
    characters cannot break or hijack the generated launcher (see module
    docstring)."""
    text = template_path.read_text(encoding="utf-8")
    repo_root_str = _reject_line_breaks(repo_root)
    # Path.stem strips only the trailing ".tmpl", leaving e.g. "claude-meter.desktop";
    # .suffix on that then yields ".desktop", ".bat", or ".command".
    kind = Path(template_path.stem).suffix

    text = text.replace(VERSION_PLACEHOLDER, version)

    if kind == ".desktop":
        # Icon= is a plain opaque string (no shell involved): substitute raw.
        text = text.replace(PLACEHOLDER, repo_root_str)
        # Exec= embeds the command path directly; it is already an absolute
        # path and only needs bash double-quote escaping for the single-quoted
        # `bash -c '...'` context of the Debug action.
        text = text.replace(CMD_PLACEHOLDER, _escape_bash_double_quoted(cmd_path))
        # Legacy placeholder kept for the Icon= line only.
        shell_arg = _escape_bash_double_quoted(repo_root_str)
        return text.replace(SHELL_ARG_PLACEHOLDER, shell_arg)
    if kind == ".bat":
        text = text.replace(PLACEHOLDER, _escape_cmd_bat(repo_root_str))
        return text.replace(CMD_PLACEHOLDER, _escape_cmd_bat(cmd_path))
    if kind == ".vbs":
        text = text.replace(PLACEHOLDER, _escape_vbs_string(repo_root_str))
        return text.replace(CMD_PLACEHOLDER, _escape_vbs_string(cmd_path))
    # .command (and anything else): a plain POSIX shell script.
    text = text.replace(PLACEHOLDER, _escape_posix_shell(repo_root_str))
    return text.replace(CMD_PLACEHOLDER, _escape_posix_shell(cmd_path))


def install_launcher(plan: LauncherPlan, repo_root: Path, launcher_dir: Path, cmd_path: str, version: str) -> Path:
    """Render and write the launcher described by ``plan``; return its path."""
    if plan.normal_bundle:
        return install_app_bundle(plan, repo_root, launcher_dir, cmd_path, version)
    return install_template(
        plan.template_name,
        plan.destination_dir / plan.output_name,
        repo_root,
        launcher_dir,
        plan.make_executable,
        cmd_path,
        version,
    )


def install_template(
    template_name: str,
    output_path: Path,
    repo_root: Path,
    launcher_dir: Path,
    make_executable: bool,
    cmd_path: str,
    version: str,
) -> Path:
    template_path = launcher_dir / template_name
    rendered = render_template(template_path, repo_root, cmd_path, version)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # The output path is always derived from LauncherPlan literals plus
    # Path.home(); no external/user-controlled path components reach here.
    output_path.write_text(rendered, encoding="utf-8")  # NOSONAR python:S2083

    if make_executable:
        current_mode = output_path.stat().st_mode
        output_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return output_path


def install_app_bundle(plan: LauncherPlan, repo_root: Path, launcher_dir: Path, cmd_path: str, version: str) -> Path:
    bundle_path = plan.destination_dir / plan.output_name
    contents_path = bundle_path / "Contents"
    install_template(
        plan.template_name,
        contents_path / "MacOS" / "claude-meter",
        repo_root,
        launcher_dir,
        True,
        cmd_path,
        version,
    )
    install_template(
        "claude-meter.app/Contents/Info.plist.tmpl",
        contents_path / "Info.plist",
        repo_root,
        launcher_dir,
        False,
        cmd_path,
        version,
    )
    return bundle_path


def _resolve_cmd_path() -> str:
    """Return the absolute path to the ``claude-meter`` executable.

    GUI launches do not always inherit the caller's PATH, so templates embed
    a resolved executable path rather than relying on a bare command name.
    """
    cmd = shutil.which("claude-meter")
    if cmd is None:
        raise RuntimeError(
            "claude-meter executable not found on PATH. Install the package first "
            "(e.g. `pip install -e .` or `uv tool install .`)."
        )
    return str(Path(cmd).resolve())


def _resolve_version() -> str:
    """Read the project version from ``pyproject.toml``."""
    pyproject = REPO_ROOT / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return str(data["project"]["version"])


def main() -> None:
    """Install the launcher for the current platform and print guidance."""
    cmd_path = _resolve_cmd_path()
    version = _resolve_version()
    plan = build_plan(sys.platform, Path.home())
    output_path = install_launcher(plan, REPO_ROOT, LAUNCHER_DIR, cmd_path, version)
    icon_path = REPO_ROOT / "assets" / "icon.png"

    print(f"Installed claude-meter launcher: {output_path}")
    if plan.debug_template_name is not None and plan.debug_output_name is not None:
        debug_path = install_template(
            plan.debug_template_name,
            plan.destination_dir / plan.debug_output_name,
            REPO_ROOT,
            LAUNCHER_DIR,
            plan.debug_make_executable,
            cmd_path,
            version,
        )
        print(f"Installed claude-meter debug launcher: {debug_path}")
    print(f"Repository root: {REPO_ROOT}")
    print(f"Command path: {cmd_path}")
    print(f"Version: {version}")
    print(f"Suggested icon location: {icon_path}")
    print(plan.icon_hint)
    print(
        "Make sure the `claude-meter` command is on your PATH first "
        "(e.g. `pip install -e .` or `uv tool install .`)."
    )


if __name__ == "__main__":
    main()
