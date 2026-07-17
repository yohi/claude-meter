#!/bin/sh
# claude-meter one-line installer (Linux / macOS, POSIX sh).
#
# Intended usage:
#     (tmp="$(mktemp)" && curl -fsSL -o "$tmp" \
#       https://raw.githubusercontent.com/yohi/claude-meter/master/install.sh \
#       && sh "$tmp"; rc=$?; rm -f "$tmp"; exit $rc)
#
# Downloading to a temp file first (rather than piping curl directly into
# `sh`) means `sh` only runs after `curl` has exited successfully, so a
# connection drop mid-transfer can never execute a truncated script; the
# temp file is removed afterwards regardless of the outcome. For safety,
# review the downloaded script's contents (the temp file above) before it
# runs.
#
# What it does:
#   1. Installs the `claude-meter` command from GitHub (uv, then pip3, then
#      python3 -m pip as fallbacks), pinned to the latest published release
#      tag (see resolve_ref() below).
#   2. Verifies the command is on your PATH.
#   3. Runs `claude-meter init`.
#   4. Creates a double-clickable desktop launcher that runs `claude-meter start`
#      (no local repository clone required).
#
# Note: this script itself is fetched from the `master` branch above, so the
# bootstrap fetch is not integrity-pinned; only the *installed* claude-meter
# package version is pinned to a release tag by resolve_ref(), which aborts
# rather than silently falling back to an unreviewed `master` HEAD if that
# resolution fails. To pin the bootstrap fetch too, download this script from
# a specific tag/commit URL instead (e.g. .../raw/<tag-or-sha>/install.sh).

set -eu

# Base git+https URL (without a ref). A specific ref (a release tag, by
# default) is appended by resolve_ref()/install_package() below so that a
# plain one-line install never installs an unreviewed `master` HEAD.
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
# Print a warning message to stderr.
warn() {
	printf 'Warning: %s\n' "$1" >&2
}

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
	err "Could not determine the latest release tag from ${GITHUB_RELEASES_LATEST_URL}."
	err "Refusing to install an unreviewed master HEAD. Set CLAUDE_METER_REF to a"
	err 'specific tag, a commit SHA, or "master" to opt in explicitly, then re-run this script.'
	exit 1
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
	# Running this installer with `sh` (whether piped or from a downloaded
	# temp file) executes it in a subshell that never sources ~/.bashrc or
	# ~/.zshrc, so a PATH update written there by uv/pip only
	# takes effect in a *new* shell. Resolve the *actual* per-user scripts
	# directory for the Python that performed the install -- this varies by
	# platform/interpreter (e.g. ~/Library/Python/X.Y/bin on macOS framework
	# Python vs. ~/.local/bin on most Linux distros) -- and prepend it, along
	# with the common ~/.local/bin fallback, so this run can find
	# `claude-meter` without requiring the user to open a new shell and
	# re-run the script.
	user_scripts_dir=""
	if command -v python3 >/dev/null 2>&1; then
		user_scripts_dir="$(python3 -c "import sysconfig; print(sysconfig.get_path('scripts', 'posix_user'))" 2>/dev/null || true)"
	fi
	if [ -n "$user_scripts_dir" ]; then
		export PATH="$user_scripts_dir:$HOME/.local/bin:$PATH"
	else
		export PATH="$HOME/.local/bin:$PATH"
	fi
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
	icon_path="$HOME/.local/share/icons/claude-meter.png"
	# Embed the *resolved* absolute path rather than a bare `claude-meter`
	# command name: double-clicking a desktop/GUI launcher does not always
	# inherit this session's PATH (see ensure_on_path above), so relying on
	# PATH lookup again here would silently fail in exactly the case this
	# launcher exists to solve.
	claude_meter_path="$(command -v claude-meter)"
	if [ "$os" = "Darwin" ]; then
		launcher="$HOME/Desktop/claude-meter.app"
		debug_launcher="$HOME/Desktop/claude-meter-debug.command"
		info "Creating macOS launcher: $launcher"
		mkdir -p "$HOME/Desktop"
		mkdir -p "$launcher/Contents/MacOS"
		cat >"$launcher/Contents/MacOS/claude-meter" <<LAUNCHER
#!/bin/bash
"$claude_meter_path" start
LAUNCHER
		chmod +x "$launcher/Contents/MacOS/claude-meter"
		cat >"$launcher/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>claude-meter</string>
    <key>CFBundleIdentifier</key>
    <string>com.claude-meter.launcher</string>
    <key>CFBundleName</key>
    <string>claude-meter</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
</dict>
</plist>
PLIST
		cat >"$debug_launcher" <<LAUNCHER
#!/bin/bash
"$claude_meter_path" start
LAUNCHER
		chmod +x "$debug_launcher"
	else
		launcher_dir="$HOME/.local/share/applications"
		launcher="$launcher_dir/claude-meter.desktop"
		info "Creating Linux desktop launcher: $launcher"
		mkdir -p "$launcher_dir"
		cat >"$launcher" <<LAUNCHER
[Desktop Entry]
Type=Application
Name=claude-meter
Comment=Local ClaudeCode usage and cost dashboard
Exec=bash -c '"$claude_meter_path" start; exec "\$SHELL"'
Icon=$icon_path
Terminal=false
Categories=Utility;Development;
Actions=Debug;

[Desktop Action Debug]
Name=デバッグモードで開く
Exec=bash -c '"$claude_meter_path" start; exec "\$SHELL"'
Terminal=true
LAUNCHER
	fi
}

# Install the icon separately because assets/icon.png is outside the Python
# package included in the uv/pip distribution.
install_icon() {
	icon_path="$HOME/.local/share/icons/claude-meter.png"
	icon_url="https://raw.githubusercontent.com/yohi/claude-meter/${ref}/assets/icon.png"
	info "Installing application icon: $icon_path"
	if curl -fsSL -o "$icon_path" "$icon_url"; then
		return 0
	fi
	warn "Failed to download icon from $icon_url; continuing without it"
	rm -f "$icon_path"
	return 0
	curl -fsSL -o "$icon_path" "$icon_url"
}

main() {
	info "Starting claude-meter installation..."
	install_package
	ensure_on_path
	run_init
	install_icon
	create_launcher
	info "Done. Launch the dashboard any time with: claude-meter start"
}

# Only run main when executed directly. Sourcing with CLAUDE_METER_INSTALL_LIB=1
# exposes the functions above for testing without triggering installation.
if [ "${CLAUDE_METER_INSTALL_LIB:-}" != "1" ]; then
	main "$@"
fi
