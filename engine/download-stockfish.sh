#!/usr/bin/env bash
# Download a Stockfish binary into this directory as ./stockfish.
#
# The binary is intentionally NOT committed to git (it is >100 MB). Run this
# once after cloning. Override the build with SF_ASSET to match your CPU/OS.
#
#   ./download-stockfish.sh
#   SF_ASSET=stockfish-ubuntu-x86-64-avx512 ./download-stockfish.sh
#
# Browse all available builds (Linux/macOS/Windows, different CPU levels) at:
#   https://github.com/official-stockfish/Stockfish/releases/latest
set -euo pipefail

cd "$(dirname "$0")"

# A widely-compatible modern x86-64 Linux build by default. Pick a different
# asset for your hardware (e.g. -avx512, -bmi2, -sse41-popcnt) or OS.
SF_ASSET="${SF_ASSET:-stockfish-ubuntu-x86-64-avx2}"
BASE="https://github.com/official-stockfish/Stockfish/releases/latest/download"
URL="${BASE}/${SF_ASSET}.tar"

echo "Downloading ${SF_ASSET} ..."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fSL -o "$tmp/sf.tar" "$URL"
tar -xf "$tmp/sf.tar" -C "$tmp"

# The tarball extracts to a 'stockfish/' dir containing the executable.
bin="$(find "$tmp" -type f -name 'stockfish*' ! -name '*.tar' | head -n1)"
if [ -z "$bin" ]; then
  echo "Could not find the stockfish executable in the archive." >&2
  exit 1
fi
cp "$bin" ./stockfish
chmod +x ./stockfish
echo "Installed: $(pwd)/stockfish"
./stockfish --help >/dev/null 2>&1 || ./stockfish bench >/dev/null 2>&1 || true
echo "Done. (If it fails to run, pick a different SF_ASSET for your CPU.)"
