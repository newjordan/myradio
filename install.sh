#!/bin/sh
# myradio installer: curl -fsSL https://myradio.garden/install.sh | sh
#
# Installs myradio for this user (no administrator rights), then runs `myradio setup`, which downloads the music
# model and engine and makes a test song. Works on a Mac with Apple silicon, Linux x86-64 with an NVIDIA graphics
# card, and an NVIDIA DGX Spark. Read it before you run it; it is short.
#
# Settings (optional): MYRADIO_SOURCE (what to install; default the GitHub repository), MYRADIO_SKIP_SETUP=1,
# MYRADIO_DATA_DIR (where your station lives; default ~/.myradio).
set -eu

SOURCE="${MYRADIO_SOURCE:-git+https://github.com/newjordan/myradio}"

say() { printf '%s\n' "$*"; }
fail() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

say ""
say "  myradio · your own radio station"
say ""

os="$(uname -s)"; arch="$(uname -m)"
case "$os/$arch" in
  Darwin/arm64) ;;
  Darwin/*) fail "myradio needs a Mac with Apple silicon (M1 or newer)." ;;
  Linux/x86_64|Linux/aarch64)
    command -v nvidia-smi >/dev/null 2>&1 || fail "myradio needs an NVIDIA graphics card with its driver installed (nvidia-smi was not found)." ;;
  *) fail "$os on $arch is not supported yet. myradio runs on macOS (Apple silicon) and Linux with NVIDIA. Windows support is being tested." ;;
esac
command -v curl >/dev/null 2>&1 || fail "curl is needed."

# uv installs Python and myradio for this user only.
if ! command -v uv >/dev/null 2>&1; then
  say "▶ Installing uv (Python manager, for this user only)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH
  command -v uv >/dev/null 2>&1 || fail "uv did not install. See https://docs.astral.sh/uv/ and run this again."
fi

say "▶ Installing myradio"
uv tool install --force --quiet --python 3.12 "$SOURCE" || fail "myradio did not install from $SOURCE."
BIN="$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
PATH="$BIN:$PATH"; export PATH
command -v myradio >/dev/null 2>&1 || fail "myradio installed, but $BIN is not on your PATH."
uv tool update-shell >/dev/null 2>&1 || true

if ! command -v ffmpeg >/dev/null 2>&1; then
  say "  note: ffmpeg is not installed. The studio needs it to play and stream audio."
  if [ "$os" = Darwin ]; then say "        Install it with: brew install ffmpeg"; else say "        Install it with: sudo apt install ffmpeg"; fi
fi

if [ "${MYRADIO_SKIP_SETUP:-0}" = 1 ]; then
  say ""; say "✓ myradio is installed. Next: myradio setup"; exit 0
fi
myradio setup || fail "Setup stopped. Fix what it says and run: myradio setup (it picks up where it stopped)."
say "Open a new terminal (so your PATH updates), then run: myradio"
