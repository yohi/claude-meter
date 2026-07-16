# Launcher Debug Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make normal desktop launches terminal-free while exposing a terminal-based debug launch path on Linux, macOS, and Windows.

**Architecture:** Keep platform-specific launcher templates and extend the existing installer to render normal and debug variants. Linux uses one `.desktop` file with a `Desktop Action Debug`; macOS uses a normal `.app` bundle plus a debug `.command`; Windows uses a normal hidden-window `.vbs` launcher plus a debug `.bat` file.

**Tech Stack:** POSIX shell, Bash `.command`, Windows batch, freedesktop `.desktop`, Python standard library, pytest.

## Global Constraints

- Preserve `claude-meter start` as the command used by every launcher.
- Preserve existing path escaping for repository roots.
- Normal Linux startup must use `Terminal=false`; only the debug action uses `Terminal=true`.
- Normal macOS and Windows launchers must not open a terminal window.
- Debug launchers must remain visibly terminal-based for troubleshooting.

---

### Task 1: Add launcher templates for debug variants

**Files:**
- Modify: `scripts/launchers/claude-meter.desktop.tmpl`
- Modify: `scripts/launchers/claude-meter.command.tmpl`
- Modify: `scripts/launchers/claude-meter.bat.tmpl`
- Create: `scripts/launchers/claude-meter-debug.command.tmpl`
- Create: `scripts/launchers/claude-meter-debug.bat.tmpl`
- Create: `scripts/launchers/claude-meter.app/Contents/Info.plist.tmpl`
- Create: `scripts/launchers/claude-meter.app/Contents/MacOS/claude-meter.tmpl`
- Create: `scripts/launchers/claude-meter.vbs.tmpl`

**Interfaces:**
- Produces templates consumed by `install.py` and the shell installers.

- [ ] **Step 1: Write the failing template assertions**

Add tests asserting that the Linux template contains a terminal-free default
entry and a `Desktop Action Debug` entry with `Terminal=true`, and that the
debug macOS/Windows templates contain their terminal-oriented commands.

- [ ] **Step 2: Run the focused tests**

Run: `.venv/bin/python -m pytest -q tests/test_launcher_install.py`

Expected: FAIL because the action and debug templates do not exist yet.

- [ ] **Step 3: Update the templates**

Use this Linux structure:

```ini
Terminal=false
Actions=Debug;

[Desktop Action Debug]
Name=デバッグモードで開く
Exec=bash -c 'cd "__REPO_ROOT_SHELL_ARG__" && claude-meter start'
Terminal=true
```

Keep the normal `Exec` command unchanged except for removing the shell-keeping
behavior that requires a terminal. Add a macOS app bundle and Windows VBScript
normal launcher that start the command with no visible terminal, plus debug
templates that run the same command while leaving the shell/console visible.

- [ ] **Step 4: Run the focused tests again**

Run: `.venv/bin/python -m pytest -q tests/test_launcher_install.py`

Expected: PASS for template rendering assertions.

### Task 2: Extend Python launcher installation

**Files:**
- Modify: `scripts/launchers/install.py`
- Test: `tests/test_launcher_install.py`

**Interfaces:**
- `build_plan()` returns a plan that identifies normal and debug output names.
- `install_launcher()` continues returning the path of each rendered launcher.

- [ ] **Step 1: Add failing plan and output tests**

Test Linux output for `Terminal=false`, `Actions=Debug;`, and
`Name=デバッグモードで開く`. Test macOS and Windows plans for the normal
app/VBScript output and debug output names, and verify the macOS app executable
and debug script are executable.

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `.venv/bin/python -m pytest -q tests/test_launcher_install.py`

Expected: FAIL because the plan and installer currently handle one output.

- [ ] **Step 3: Implement the minimal multi-output plan**

Extend `LauncherPlan` with the normal and debug template/output metadata, render
the macOS app bundle directory and Windows VBScript in addition to the debug
files, then print every generated path.
Keep `install_launcher()` as the single-file renderer so existing escaping tests
remain unchanged.

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m pytest -q tests/test_launcher_install.py`

Expected: PASS.

### Task 3: Update one-line shell installers

**Files:**
- Modify: `install.sh`
- Modify: `install_bitbucket.sh.tmpl`
- Modify: `install.ps1`
- Modify: `install_bitbucket.ps1.tmpl`

**Interfaces:**
- One-line installers generate the same normal/debug launcher behavior as the
  local Python launcher installer.

- [ ] **Step 1: Add shell syntax and output checks**

Exercise the launcher generation with mocked `uname`, `claude-meter`, `curl`, and
`uv` commands in a temporary home, then assert the generated Linux desktop file
contains the debug action and the generated platform-specific debug file exists.

- [ ] **Step 2: Run the checks before implementation**

Run: `sh -n install.sh install_bitbucket.sh.tmpl`

Expected: syntax passes, while the new output assertions fail.

- [ ] **Step 3: Implement shell installer parity**

Set `Terminal=false` for normal Linux launch, add the debug desktop action, and
write the macOS app bundle plus `claude-meter-debug.command`. Update both
PowerShell installers to write the Windows VBScript plus
`claude-meter-debug.bat`. Keep the existing icon installation and Bitbucket
Bearer authentication unchanged.

- [ ] **Step 4: Run shell checks**

Run: `sh -n install.sh install_bitbucket.sh.tmpl`

Expected: PASS, with generated launchers matching the Python installer behavior.

### Task 4: Update documentation and run quality gates

**Files:**
- Modify: `README.md`
- Modify: `README_BITBUCKET.md`

- [ ] **Step 1: Document normal and debug launchers**

Explain that Linux exposes `デバッグモードで開く` in the launcher context menu,
while macOS creates an app plus `claude-meter-debug.command`, and Windows
creates a hidden-window launcher plus `claude-meter-debug.bat`.

- [ ] **Step 2: Run all project checks**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy --strict src
sh -n install.sh install_bitbucket.sh.tmpl
markdownlint-cli2 README.md README_BITBUCKET.md
```

Expected: all commands pass, except any pre-existing Markdown violations must
be reported separately without changing unrelated documentation.
