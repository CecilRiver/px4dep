#!/usr/bin/env bash
# Fetch and unpack the Ubuntu libclang-14-dev package into .deps/.
# This avoids requiring root on machines where sudo is unavailable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="$ROOT/.deps/libclang14"
HEADER="$DEPS/usr/lib/llvm-14/include/clang/Tooling/Tooling.h"

if [[ -f "$HEADER" ]]; then
  echo "libclang-14-dev headers already present at $DEPS"
  exit 0
fi

LLVM_LIB=/usr/lib/llvm-14/lib
if [[ ! -e "$LLVM_LIB/libclang-cpp.so.14" ]]; then
  echo "error: $LLVM_LIB/libclang-cpp.so.14 not found (install llvm-14 runtime libraries)" >&2
  exit 1
fi

mkdir -p "$ROOT/.deps/pkg" "$DEPS"
cd "$ROOT/.deps/pkg"
apt-get download libclang-14-dev
DEB="$(ls -1t libclang-14-dev_*.deb | head -n 1)"
dpkg-deb -x "$DEB" "$DEPS"
ln -sf "$LLVM_LIB/libclang-cpp.so.14" "$DEPS/usr/lib/llvm-14/lib/libclang-cpp.so"
echo "unpacked $DEB into $DEPS"
