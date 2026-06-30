#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
AUTO_INSTALL_SYSTEM_DEPS="${AUTO_INSTALL_SYSTEM_DEPS:-1}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$ROOT_DIR/.matplotlib-cache}"
mkdir -p "$MPLCONFIGDIR"

log() {
  printf '\n==> %s\n' "$*"
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

java_toolchain_works() {
  has_command java && has_command javac && has_command jar \
    && java -version >/dev/null 2>&1 \
    && javac -version >/dev/null 2>&1
}

install_system_dependencies() {
  local missing=()
  local command_name
  for command_name in "$PYTHON_BIN" node npm mvn make; do
    if ! has_command "$command_name"; then
      missing+=("$command_name")
    fi
  done
  if ! java_toolchain_works; then
    missing+=("JDK")
  fi
  if ! has_command clang++ && ! has_command g++; then
    missing+=("C++ compiler")
  fi

  if [[ ${#missing[@]} -eq 0 ]]; then
    return
  fi

  if [[ "$AUTO_INSTALL_SYSTEM_DEPS" != "1" ]]; then
    printf 'Missing system dependencies: %s\n' "${missing[*]}" >&2
    printf 'Install them or rerun with AUTO_INSTALL_SYSTEM_DEPS=1.\n' >&2
    exit 1
  fi

  log "Installing missing system dependencies: ${missing[*]}"
  if has_command brew; then
    local formulae=()
    has_command "$PYTHON_BIN" || formulae+=(python)
    has_command node || formulae+=(node)
    if ! java_toolchain_works; then
      formulae+=(openjdk)
    fi
    has_command mvn || formulae+=(maven)
    if ! has_command clang++ && ! has_command g++; then
      formulae+=(llvm)
    fi
    if [[ ${#formulae[@]} -gt 0 ]]; then
      brew install "${formulae[@]}"
    fi
    if brew --prefix openjdk >/dev/null 2>&1; then
      export PATH="$(brew --prefix openjdk)/bin:$PATH"
    fi
  elif has_command apt-get; then
    local sudo_command=()
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
      has_command sudo || {
        printf 'sudo is required to install system packages.\n' >&2
        exit 1
      }
      sudo_command=(sudo)
    fi
    "${sudo_command[@]}" apt-get update
    "${sudo_command[@]}" apt-get install -y \
      python3 python3-venv python3-pip nodejs npm openjdk-17-jdk maven build-essential
  else
    printf 'No supported package manager found. Install: Python 3, Node.js/npm, a JDK, Maven, make, and a C++ compiler.\n' >&2
    exit 1
  fi
}

verify_system_dependencies() {
  local command_name
  for command_name in "$PYTHON_BIN" node npm mvn make; do
    has_command "$command_name" || {
      printf 'Required command is still unavailable: %s\n' "$command_name" >&2
      exit 1
    }
  done
  java_toolchain_works || {
    printf 'A working JDK (java, javac, and jar) is required.\n' >&2
    exit 1
  }
  if has_command clang++; then
    CXX_BIN="${CXX_BIN:-clang++}"
  elif has_command g++; then
    CXX_BIN="${CXX_BIN:-g++}"
  else
    printf 'A C++ compiler is required to build EGRET.\n' >&2
    exit 1
  fi
}

create_python_environment() {
  log "Creating/updating Python environment at $VENV_DIR"
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  PYTHON="$VENV_DIR/bin/python"
  "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12 or newer is required"'
  "$PYTHON" -m pip install --upgrade pip setuptools wheel
  if [[ ! -f "$ROOT_DIR/requirement.txt" ]]; then
    printf 'Missing Python requirements file: %s\n' "$ROOT_DIR/requirement.txt" >&2
    exit 1
  fi
  "$PYTHON" -m pip install -r "$ROOT_DIR/requirement.txt"
}

install_node_dependencies() {
  log "Installing Node.js dependencies"
  if [[ -f package-lock.json ]]; then
    npm ci
  else
    npm install
  fi
}

build_egret_extension() {
  log "Building EGRET extension for $($PYTHON --version 2>&1)"
  if (
    cd egret
    "$PYTHON" -c 'import egret_ext' >/dev/null 2>&1
  ); then
    printf 'EGRET extension is already importable.\n'
    return
  fi

  (
    cd egret/src
    make -B libegret.a CXX="$CXX_BIN"
    "$PYTHON" create_ext.py build_ext --inplace
    extension_path="$(find . -maxdepth 1 -type f \( -name 'egret_ext*.so' -o -name 'egret_ext*.pyd' \) -print -quit)"
    if [[ -z "$extension_path" ]]; then
      printf 'EGRET build completed but no extension module was produced.\n' >&2
      exit 1
    fi
    cp -f "$extension_path" ..
  )

  (
    cd egret
    "$PYTHON" -c 'import egret_ext; print("EGRET extension ready:", egret_ext.__file__)'
  )
}

verify_workspace_artifacts() {
  log "Checking workspace artifacts"
  if [[ ! -f "$ROOT_DIR/mutrex.jar" ]]; then
    printf 'Missing MutRex JAR: %s\n' "$ROOT_DIR/mutrex.jar" >&2
    exit 1
  fi
  jar tf "$ROOT_DIR/mutrex.jar" >/dev/null
}

prewarm_external_generators() {
  log "Resolving and smoke-testing external generators"
  "$PYTHON" benchmark_generators/java_regex_generators.py generex '[a-z]{2}\d' --n 1 --timeout-seconds 120 >/dev/null
  "$PYTHON" benchmark_generators/java_regex_generators.py rgxgen '[a-z]{2}\d' --n 1 --seed 1 --timeout-seconds 120 >/dev/null
  "$PYTHON" benchmark_generators/external_regex_generators.py randexp '[a-z]{2}\d' --n 1 --seed 1 --timeout-seconds 120 >/dev/null
  "$PYTHON" benchmark_generators/external_regex_generators.py mutrex '[a-z]{2}\d' --n 1 --n-negative 1 --timeout-seconds 120 >/dev/null
}

verify_python_imports() {
  log "Verifying benchmark imports"
  "$PYTHON" -c 'import coverage, django, email_validator, exrex, graphviz, marshmallow, matplotlib, numpy, pydantic, dateutil, regex, tqdm, xeger'
  "$PYTHON" regex_positive_generator/benchmarks/python_regex_function_coverage_benchmark.py --help >/dev/null
  "$PYTHON" regex_positive_generator/benchmarks/python_regex_function_combined_tools_coverage_benchmark.py --help >/dev/null
}

main() {
  install_system_dependencies
  verify_system_dependencies
  create_python_environment
  install_node_dependencies
  verify_workspace_artifacts
  build_egret_extension
  prewarm_external_generators
  verify_python_imports

  log "Coverage benchmark environment is ready"
  printf 'Individual benchmark:\n'
  printf '  %q %q --runs 1 --n-positive 1000 --n-negative 1000 --workers 1\n' \
    "$PYTHON" "regex_positive_generator/benchmarks/python_regex_function_coverage_benchmark.py"
  printf 'Combined benchmark:\n'
  printf '  %q %q --runs 1 --n-positive 1000 --n-negative 1000 --workers 1\n' \
    "$PYTHON" "regex_positive_generator/benchmarks/python_regex_function_combined_tools_coverage_benchmark.py"
}

main "$@"
