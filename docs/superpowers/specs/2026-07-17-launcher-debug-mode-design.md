# Launcher Debug Mode Design

## Goal

Launch the claude-meter dashboard without an attached terminal during normal
startup, while providing an explicit terminal-based debug launcher for each
supported desktop platform.

## Scope

- Linux `.desktop` launchers expose a right-click `Debug` action.
- macOS generates a terminal-free normal `.app` bundle and a terminal-based
  debug `.command` file.
- Windows generates a terminal-free normal `.vbs` launcher and a
  terminal-based debug `.bat` file.
- The debug variants preserve the current terminal-oriented behavior.
- Existing icon paths and `claude-meter start` invocation remain unchanged.

## Design

The launcher installer renders one normal launcher and one debug launcher from
platform-specific templates. Linux uses a single `.desktop` file with
`Actions=Debug`; its main entry has `Terminal=false`, while the desktop action
has `Terminal=true`. macOS and Windows cannot add an equivalent context-menu
action from the existing file formats alone, so the installer places a second
debug launcher beside the normal launcher.

The normal macOS launcher is an application bundle whose executable invokes
`claude-meter start` without opening Terminal. The debug macOS launcher remains
a `.command` script. The normal Windows launcher uses `WScript.Shell` with a
hidden window, while the debug launcher remains a visible-console `.bat` file.

## Files

- `scripts/launchers/`: add or update templates for normal and debug variants.
- `scripts/launchers/install.py`: render both variants and report both paths.
- `install.sh`: generate the Linux action and platform-specific debug files for
  one-line installs.
- `install_bitbucket.sh.tmpl`: mirror the one-line installer behavior.
- `tests/test_launcher_install.py`: verify rendered launcher commands and
  terminal settings.
- `README.md` and `README_BITBUCKET.md`: document normal and debug launchers.

## Verification

- Rendered Linux launcher has `Terminal=false` for normal startup and a
  `Desktop Action Debug` entry with `Terminal=true`.
- macOS and Windows debug files are generated alongside normal files.
- Existing launcher path quoting tests continue to pass.
- Run the project pytest, Ruff, mypy, shell syntax, and Markdown checks.
