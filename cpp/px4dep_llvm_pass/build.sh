#!/usr/bin/env bash
# Build the M6 targeted LLVM pass against the system LLVM 14 installation.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${HERE}/../.." && pwd)"
BUILD_DIR="${PROJECT_ROOT}/.deps/llvm-pass-build"
OUT_DIR="${PROJECT_ROOT}/.deps/bin"
LLVM_CMAKE_DIR="${LLVM_CMAKE_DIR:-$(llvm-config-14 --cmakedir)}"

mkdir -p "${BUILD_DIR}" "${OUT_DIR}"

cmake -S "${HERE}" -B "${BUILD_DIR}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_DIR="${LLVM_CMAKE_DIR}" \
  -DCMAKE_CXX_COMPILER="${CXX:-clang++-14}"

cmake --build "${BUILD_DIR}" --parallel

cp "${BUILD_DIR}/px4dep_member_topic.so" "${OUT_DIR}/px4dep_member_topic.so"
echo "built: ${OUT_DIR}/px4dep_member_topic.so"
