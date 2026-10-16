#!/usr/bin/env bash
# Build the PX4 dependency indexer against locally unpacked Clang 14 headers.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPS="$ROOT/.deps/libclang14"
LLVM_PREFIX=/usr/lib/llvm-14
CXX="${CXX:-clang++-14}"

if [[ ! -f "$DEPS/usr/lib/llvm-14/include/clang/Tooling/Tooling.h" ]]; then
  "$ROOT/scripts/setup_libclang.sh"
fi

mkdir -p "$ROOT/.deps/bin"
ln -sf "$LLVM_PREFIX/lib/libclang-cpp.so.14" "$DEPS/usr/lib/llvm-14/lib/libclang-cpp.so"

"$CXX" -std=c++17 -fno-rtti -O2 -Wall -Wextra \
  -isystem "$DEPS/usr/lib/llvm-14/include" \
  -isystem /usr/include/llvm-14 \
  -isystem /usr/include/llvm-c-14 \
  "$ROOT/cpp/px4dep_clang_tool/main.cpp" \
  -L "$DEPS/usr/lib/llvm-14/lib" -lclang-cpp \
  -L "$LLVM_PREFIX/lib" -lLLVM-14 \
  -Wl,-rpath,"$LLVM_PREFIX/lib" \
  -o "$ROOT/.deps/bin/px4dep_clang_tool"

echo "built $ROOT/.deps/bin/px4dep_clang_tool"
