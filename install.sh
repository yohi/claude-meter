#!/bin/sh
# claude-meter one-line installer (Linux / macOS, POSIX sh).
#
# Intended usage:
#     curl -fsSL https://raw.githubusercontent.com/yohi/claude-meter/master/install.sh | sh
#
# This downloads and runs a remote script directly in your shell. For safety,
# review the contents of this script before piping it into `sh`.
#
# What it does:
#   1. Installs the `claude-meter` command from GitHub (uv, then pip3, then
#      python3 -m pip as fallbacks).
#   2. Verifies the command is on your PATH.
#   3. Runs `claude-meter init`.
#   4. Creates a double-clickable desktop launcher that runs `claude-meter start`
#      (no local repository clone required).

set -eu

# Base git+https URL (without a ref). A specific ref (a release tag, by
# default) is appended by resolve_ref()/install_package() below so that a
# plain `curl | sh` never installs an unreviewed `master` HEAD.
REPO_BASE_URL="git+https://github.com/yohi/claude-meter.git"
GITHUB_RELEASES_LATEST_URL="https://github.com/yohi/claude-meter/releases/latest"
UV_INSTALL_DOCS="https://docs.astral.sh/uv/getting-started/installation/"

# Print a progress message to stdout.
info() {
	printf '==> %s\n' "$1"
}

# Print an error message to stderr.
err() {
	printf 'Error: %s\n' "$1" >&2
}

# Resolve the git ref to install. Defaults to the latest published release
# tag (resolved via GitHub's /releases/latest redirect) so this installer is
# pinned to a reviewed, stable release instead of the master branch's
# unreviewed HEAD. Set CLAUDE_METER_REF to override (e.g. to a specific tag,
# a commit SHA, or "master" to opt back into HEAD).
resolve_ref() {
	if [ -n "${CLAUDE_METER_REF:-}" ]; then
		printf '%s' "$CLAUDE_METER_REF"
		return
	fi
	if command -v curl >/dev/null 2>&1; then
		tag="$(curl -fsSL -o /dev/null -w '%{url_effective}' "$GITHUB_RELEASES_LATEST_URL" 2>/dev/null | sed -n 's#.*/tag/##p' || true)"
		if [ -n "$tag" ]; then
			printf '%s' "$tag"
			return
		fi
	fi
	err "Could not determine the latest release tag; falling back to the master branch (unreviewed HEAD)."
	printf 'master'
}

# Install the claude-meter package using the first available installer.
# Separated into its own function so tests can source this script (with
# CLAUDE_METER_INSTALL_LIB=1) and exercise the launcher logic without running
# a real package installation.
install_package() {
	ref="$(resolve_ref)"
	repo_url="${REPO_BASE_URL}@${ref}"
	if command -v uv >/dev/null 2>&1; then
		info "Installing claude-meter ${ref} with uv (uv tool install)..."
		uv tool install "$repo_url"
	elif command -v pip3 >/dev/null 2>&1; then
		info "uv not found; installing claude-meter ${ref} with pip3 (--user)..."
		pip3 install --user "$repo_url"
	elif command -v python3 >/dev/null 2>&1; then
		info "uv/pip3 not found; installing claude-meter ${ref} with python3 -m pip (--user)..."
		python3 -m pip install --user "$repo_url"
	else
		err "No suitable installer found: none of uv, pip3, or python3 is available."
		err "Install uv first, then re-run this script: $UV_INSTALL_DOCS"
		exit 1
	fi
}

# Ensure the claude-meter command is reachable on PATH after installation.
ensure_on_path() {
	# `curl | sh` runs this installer in a subshell that never sources
	# ~/.bashrc or ~/.zshrc, so a PATH update written there by uv/pip only
	# takes effect in a *new* shell. Prepend the common per-user install
	# directory now so this run can find `claude-meter` without requiring the
	# user to open a new shell and re-run the script.
	export PATH="$HOME/.local/bin:$PATH"
	if ! command -v claude-meter >/dev/null 2>&1; then
		err "claude-meter was installed but is not on your PATH."
		err "Open a new shell and re-run this script, or add the install location (e.g. ~/.local/bin) to your PATH."
		exit 1
	fi
}

# Run first-time initialization.
run_init() {
	info "Initializing claude-meter (claude-meter init)..."
	claude-meter init
}

# Create an OS-appropriate desktop launcher that simply calls `claude-meter
# start`. This deliberately does NOT depend on a local repository clone: there
# is no `cd` into a repo directory and no reference to repo-local files.
create_launcher() {
	os="$(uname -s)"
	if [ "$os" = "Darwin" ]; then
		launcher="$HOME/Desktop/claude-meter.command"
		info "Creating macOS launcher: $launcher"
		mkdir -p "$HOME/Desktop"
		cat >"$launcher" <<'LAUNCHER'
#!/bin/bash
claude-meter start
LAUNCHER
		chmod +x "$launcher"
	else
		launcher_dir="$HOME/.local/share/applications"
		launcher="$launcher_dir/claude-meter.desktop"
		info "Creating Linux desktop launcher: $launcher"
		mkdir -p "$launcher_dir"
		cat >"$launcher" <<'LAUNCHER'
[Desktop Entry]
Type=Application
Name=claude-meter
Comment=Local ClaudeCode usage and cost dashboard
Exec=bash -c 'claude-meter start; exec "$SHELL"'
Icon=utilities-terminal
Terminal=true
Categories=Utility;Development;
LAUNCHER
	fi
}

main() {
	info "Starting claude-meter installation..."
	install_package
	ensure_on_path
	run_init
	create_launcher
	info "Done. Launch the dashboard any time with: claude-meter start"
}

# Only run main when executed directly. Sourcing with CLAUDE_METER_INSTALL_LIB=1
# exposes the functions above for testing without triggering installation.
if [ "${CLAUDE_METER_INSTALL_LIB:-}" != "1" ]; then
	main "$@"
fi
