#!/bin/zsh
# Builds "build/Schedule I.app" (native macOS port) from your Windows copy of Schedule I: ./build.sh [--help]
# Needs an Apple Silicon Mac and the Xcode Command Line Tools. Everything else (Python, .NET, CMake, Unity's macOS
# player, packages) is downloaded, pinned and checksummed, into deps/ - see tools/py/build.py.
set -e
setopt pipefail
ROOT=${0:A:h}
DEPS=$ROOT/deps

if [[ $(uname -s) != Darwin || $(sysctl -n hw.optional.arm64 2>/dev/null) != 1 ]]; then
  echo "error: the port needs a Mac with Apple Silicon" >&2
  exit 1
fi
if [[ $(uname -m) != arm64 ]]; then  # started from a Rosetta (x86_64) shell
  exec arch -arm64 /bin/zsh "$0" "$@"
fi
if ! xcrun --find clang >/dev/null 2>&1; then
  echo "The Xcode Command Line Tools are needed; starting their installer. Run ./build.sh again when it is done." >&2
  xcode-select --install 2>/dev/null || true
  exit 1
fi

# uv provides a pinned Python with the packages of tools/requirements.txt in deps/venv
UV_VERSION=0.11.28
UV_SHA256=33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232
UV=$DEPS/uv/uv
if [[ $("$UV" --version 2>/dev/null) != "uv $UV_VERSION "* ]]; then
  echo "==> uv $UV_VERSION"
  mkdir -p "$DEPS/uv"
  if ! curl -fsSL --retry 3 -o "$DEPS/uv/uv.tar.gz" \
      "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz"; then
    echo "error: downloading uv failed; check your internet connection and run ./build.sh again" >&2
    exit 1
  fi
  if ! echo "$UV_SHA256  $DEPS/uv/uv.tar.gz" | shasum -a 256 -c --status -; then
    echo "error: checksum mismatch for the uv download" >&2
    exit 1
  fi
  tar -xzf "$DEPS/uv/uv.tar.gz" -C "$DEPS/uv" --strip-components 1
  rm "$DEPS/uv/uv.tar.gz"
fi
export UV_PYTHON_INSTALL_DIR=$DEPS/python UV_CACHE_DIR=$DEPS/uv-cache UV_PYTHON_PREFERENCE=only-managed UV_NO_CONFIG=1
VENV=$DEPS/venv
STAMP="$VENV, uv $UV_VERSION, requirements $(shasum -a 256 "$ROOT/tools/requirements.txt" | cut -d' ' -f1)"
if [[ ! -x $VENV/bin/python || $(cat "$VENV/.stamp" 2>/dev/null) != "$STAMP" ]]; then
  echo "==> Python"
  rm -rf "$VENV"
  if ! "$UV" venv --quiet --python 3.12 "$VENV" ||
      ! "$UV" pip install --quiet --python "$VENV/bin/python" -r "$ROOT/tools/requirements.txt"; then
    rm -rf "$VENV"
    echo "error: setting up Python failed (see above); check your internet connection and run ./build.sh again" >&2
    exit 1
  fi
  print -r -- "$STAMP" > "$VENV/.stamp"
fi
exec "$VENV/bin/python" -I "$ROOT/tools/py/build.py" "$@"
