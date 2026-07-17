"""Tests for scripts/launchers/install.py's template-rendering/escaping logic.

``scripts/launchers/install.py`` is a standalone script (not part of the
``claude_meter`` package), so it is loaded here via ``importlib`` rather than a
normal import.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_INSTALL_PY = Path(__file__).resolve().parents[1] / "scripts" / "launchers" / "install.py"


def _load_launcher_install() -> ModuleType:
    spec = importlib.util.spec_from_file_location("launcher_install", _INSTALL_PY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launcher_install = _load_launcher_install()

# A path containing every character the escaping helpers must neutralize:
# single/double quotes, a backslash, a dollar sign, and a backtick.
TRICKY_NAME = "weird's \"repo\" $(rm -rf) `x` \\ end"
# Same as above but without a single quote, since a literal single quote is
# rejected outright for the .desktop format (see module docstring).
TRICKY_NAME_NO_SQUOTE = 'weird "repo" $(rm -rf) `x` \\ end'


@pytest.mark.skipif(shutil.which("sh") is None, reason="requires a POSIX sh")
def test_escape_posix_shell_roundtrips_special_characters(tmp_path: Path) -> None:
    token = launcher_install._escape_posix_shell(TRICKY_NAME)
    result = subprocess.run(
        ["sh", "-c", f"printf '%s' {token}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == TRICKY_NAME


def test_escape_posix_shell_rejects_nothing_but_quotes_and_specials() -> None:
    # Sanity check: the escaped form is always wrapped in single quotes.
    escaped = launcher_install._escape_posix_shell("/plain/path")
    assert escaped == "'/plain/path'"


def test_escape_cmd_bat_doubles_percent() -> None:
    assert launcher_install._escape_cmd_bat(r"C:\repo %VAR% end") == '"C:\\repo %%VAR%% end"'


def test_escape_cmd_bat_rejects_double_quote() -> None:
    with pytest.raises(ValueError, match="double quote"):
        launcher_install._escape_cmd_bat('C:\\evil"path')


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_render_template_desktop_exec_roundtrips_special_characters(tmp_path: Path) -> None:
    repo_root = tmp_path / TRICKY_NAME_NO_SQUOTE
    repo_root.mkdir()
    template = (
        Path(__file__).resolve().parents[1] / "scripts" / "launchers" / "claude-meter.desktop.tmpl"
    )

    rendered = launcher_install.render_template(template, repo_root, "/bin/claude-meter", "1.0.0")

    exec_line = next(line for line in rendered.splitlines() if line.startswith("Exec=") and "bash -c" not in line)
    assert exec_line == "Exec=/bin/claude-meter start"

    debug_exec_line = next(line for line in rendered.splitlines() if line.startswith("Exec=bash -c"))
    assert '/bin/claude-meter start; exec "$SHELL"' in debug_exec_line

    icon_line = next(line for line in rendered.splitlines() if line.startswith("Icon="))
    assert icon_line == f"Icon={repo_root}/assets/icon.png"

    assert "Terminal=false" in rendered
    assert "Actions=Debug;" in rendered
    assert "[Desktop Action Debug]" in rendered
    assert "Name=デバッグモードで開く" in rendered
    assert rendered.count("Terminal=true") == 1


def test_build_plan_includes_debug_launchers(tmp_path: Path) -> None:
    linux = launcher_install.build_plan("linux", tmp_path)
    assert linux.debug_template_name is None

    macos = launcher_install.build_plan("darwin", tmp_path)
    assert macos.output_name == "claude-meter.app"
    assert macos.debug_output_name == "claude-meter-debug.command"
    assert macos.normal_bundle is True
    assert macos.debug_make_executable is True

    windows = launcher_install.build_plan("win32", tmp_path)
    assert windows.output_name == "claude-meter.vbs"
    assert windows.debug_output_name == "claude-meter-debug.bat"


def test_render_template_vbs_escapes_double_quotes(tmp_path: Path) -> None:
    template = tmp_path / "launcher.vbs.tmpl"
    template.write_text("path = __REPO_ROOT__\ncmd = __CLAUDE_METER_PATH__\n", encoding="utf-8")

    rendered = launcher_install.render_template(
        template, Path('C:/a "quoted"/repo'), 'C:/a "quoted"/bin/claude-meter', "1.0.0"
    )

    assert rendered == (
        'path = "C:/a ""quoted""/repo"\n'
        'cmd = "C:/a ""quoted""/bin/claude-meter"\n'
    )


def test_render_template_substitutes_version_in_plist(tmp_path: Path) -> None:
    template = tmp_path / "Info.plist.tmpl"
    template.write_text(
        "<string>__VERSION__</string>\n<key>__REPO_ROOT__</key>\n", encoding="utf-8"
    )
    rendered = launcher_install.render_template(
        template, Path("/repo"), "/bin/claude-meter", "2.3.4"
    )
    assert "<string>2.3.4</string>" in rendered
    assert "<key>'/repo'</key>" in rendered


def test_install_macos_launcher_creates_app_and_debug_script(tmp_path: Path) -> None:
    plan = launcher_install.build_plan("darwin", tmp_path)
    launcher_dir = _INSTALL_PY.parent

    app_path = launcher_install.install_launcher(plan, Path("/repo"), launcher_dir, "/bin/claude-meter", "1.0.0")
    debug_path = launcher_install.install_template(
        plan.debug_template_name or "",
        plan.destination_dir / (plan.debug_output_name or ""),
        Path("/repo"),
        launcher_dir,
        plan.debug_make_executable,
        "/bin/claude-meter",
        "1.0.0",
    )

    assert app_path == tmp_path / "Desktop" / "claude-meter.app"
    info_plist = app_path / "Contents" / "Info.plist"
    assert info_plist.is_file()
    plist_text = info_plist.read_text(encoding="utf-8")
    assert "<string>1.0.0</string>" in plist_text
    assert (app_path / "Contents" / "MacOS" / "claude-meter").is_file()
    assert debug_path.name == "claude-meter-debug.command"
    assert debug_path.stat().st_mode & 0o111


def test_render_template_desktop_rejects_single_quote(tmp_path: Path) -> None:
    repo_root = tmp_path / "has'quote"
    template = (
        Path(__file__).resolve().parents[1] / "scripts" / "launchers" / "claude-meter.desktop.tmpl"
    )
    with pytest.raises(ValueError, match="single quote"):
        launcher_install.render_template(template, repo_root, "/bin/claude-meter", "1.0.0")


def test_render_template_rejects_line_breaks(tmp_path: Path) -> None:
    repo_root = tmp_path / "line\nbreak"
    template = (
        Path(__file__).resolve().parents[1] / "scripts" / "launchers" / "claude-meter.command.tmpl"
    )
    with pytest.raises(ValueError, match="line break"):
        launcher_install.render_template(template, repo_root, "/bin/claude-meter", "1.0.0")


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_render_template_command_roundtrips_special_characters(tmp_path: Path) -> None:
    repo_root = tmp_path / TRICKY_NAME
    repo_root.mkdir()
    template = (
        Path(__file__).resolve().parents[1] / "scripts" / "launchers" / "claude-meter.command.tmpl"
    )

    rendered = launcher_install.render_template(template, repo_root, "/bin/claude-meter", "1.0.0")
    cd_line = next(line for line in rendered.splitlines() if line.startswith("cd "))
    result = subprocess.run(
        ["bash", "-c", f"{cd_line} && pwd"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(repo_root.resolve())
    assert "exec '/bin/claude-meter' start" in rendered


def test_render_template_bat_embeds_quoted_cmd_argument(tmp_path: Path) -> None:
    repo_root = tmp_path / "C:\\Users\\Jane Doe\\claude-meter"
    template = (
        Path(__file__).resolve().parents[1] / "scripts" / "launchers" / "claude-meter.bat.tmpl"
    )
    rendered = launcher_install.render_template(
        template, repo_root, r"C:\Users\Jane Doe\claude-meter.exe", "1.0.0"
    )
    cd_line = next(line for line in rendered.splitlines() if line.startswith("cd /d "))
    assert cd_line == f'cd /d "{repo_root}"'
    assert r'cmd /k "C:\Users\Jane Doe\claude-meter.exe" start' in rendered
