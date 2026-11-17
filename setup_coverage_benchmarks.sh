#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN_REQUESTED="${PYTHON_BIN:-}"
PYTHON_BIN="$PYTHON_BIN_REQUESTED"
AUTO_INSTALL_SYSTEM_DEPS="${AUTO_INSTALL_SYSTEM_DEPS:-1}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$ROOT_DIR/.matplotlib-cache}"
mkdir -p "$MPLCONFIGDIR"

log() {
  printf '\n==> %s\n' "$*"
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 13))' >/dev/null 2>&1
}

select_python_bin() {
  if [[ -n "$PYTHON_BIN_REQUESTED" ]]; then
    PYTHON_BIN="$PYTHON_BIN_REQUESTED"
    return
  fi

  local candidates=(python3 python3.16 python3.15 python3.14 python3.13)
  local brew_python_prefix=""
  local candidate
  if has_command brew; then
    brew_python_prefix="$(brew --prefix python 2>/dev/null || true)"
    if [[ -n "$brew_python_prefix" ]]; then
      candidates=("$brew_python_prefix/bin/python3" "${candidates[@]}")
    fi
  fi

  for candidate in "${candidates[@]}"; do
    if has_command "$candidate" && python_is_supported "$candidate"; then
      PYTHON_BIN="$(command -v "$candidate")"
      return
    fi
  done

  PYTHON_BIN=python3
}

java_toolchain_works() {
  has_command java && has_command javac && has_command jar \
    && java -version >/dev/null 2>&1 \
    && javac -version >/dev/null 2>&1
}

# Homebrew's openjdk is keg-only: it is installed but not on PATH, and macOS's
# /usr/bin/java is only a stub. Use it when the default toolchain is unusable.
use_homebrew_jdk_if_present() {
  java_toolchain_works && return
  has_command brew || return
  local jdk_prefix
  jdk_prefix="$(brew --prefix openjdk 2>/dev/null || true)"
  if [[ -n "$jdk_prefix" && -x "$jdk_prefix/bin/javac" ]]; then
    export PATH="$jdk_prefix/bin:$PATH"
    if [[ -d "$jdk_prefix/libexec/openjdk.jdk/Contents/Home" ]]; then
      export JAVA_HOME="$jdk_prefix/libexec/openjdk.jdk/Contents/Home"
    fi
  fi
}

# uv can download and manage a suitable Python itself, so a system Python 3.13+
# is required only when uv is unavailable.
system_python_missing() {
  has_command uv && return 1
  ! has_command "$PYTHON_BIN" || ! python_is_supported "$PYTHON_BIN"
}

install_system_dependencies() {
  local missing=()
  local command_name
  if system_python_missing; then
    missing+=("Python 3.13+")
  fi
  for command_name in node npm mvn make; do
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
    if system_python_missing; then
      formulae+=(python)
    fi
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
    use_homebrew_jdk_if_present
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
  for command_name in node npm mvn make; do
    has_command "$command_name" || {
      printf 'Required command is still unavailable: %s\n' "$command_name" >&2
      exit 1
    }
  done
  if system_python_missing; then
    printf 'Python 3.13 or newer is required. Install uv, or set PYTHON_BIN to a compatible interpreter.\n' >&2
    exit 1
  fi
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

venv_is_supported() {
  [[ -x "$VENV_DIR/bin/python" ]] && python_is_supported "$VENV_DIR/bin/python"
}

create_python_environment() {
  log "Creating/updating Python environment at $VENV_DIR"

  if venv_is_supported; then
    :
  elif has_command uv; then
    # uv downloads a managed Python 3.13+ if no suitable interpreter exists.
    # An explicitly requested PYTHON_BIN is honored.
    [[ -x "$VENV_DIR/bin/python" ]] && log "Recreating incompatible environment ($("$VENV_DIR/bin/python" --version 2>&1))"
    uv venv --clear --python "${PYTHON_BIN_REQUESTED:->=3.13}" "$VENV_DIR"
  else
    if ! python_is_supported "$PYTHON_BIN"; then
      printf 'Python 3.13 or newer is required, but %s reports: %s\n' \
        "$PYTHON_BIN" "$("$PYTHON_BIN" --version 2>&1)" >&2
      printf 'Install uv, or set PYTHON_BIN to a newer interpreter (for example, PYTHON_BIN=python3.13).\n' >&2
      exit 1
    fi
    [[ -x "$VENV_DIR/bin/python" ]] && log "Recreating incompatible environment ($("$VENV_DIR/bin/python" --version 2>&1))"
    "$PYTHON_BIN" -m venv --clear "$VENV_DIR"
  fi

  if ! venv_is_supported; then
    printf 'The environment at %s does not provide Python 3.13 or newer.\n' "$VENV_DIR" >&2
    exit 1
  fi

  PYTHON="$VENV_DIR/bin/python"
  # setuptools is required at build time by EGRET's create_ext.py, which
  # imports distutils (removed from the standard library in Python 3.12).
  python_install --upgrade setuptools wheel
  # Editable install of matchmaker plus the pinned benchmark and test
  # dependencies declared in pyproject.toml.
  python_install -e "${ROOT_DIR}[benchmarks,dev]"
}

# Install packages into $PYTHON's environment. Uses uv when available
# (environments created by `uv venv` contain no pip); otherwise uses pip,
# bootstrapping it with ensurepip if the environment lacks it.
python_install() {
  if has_command uv; then
    uv pip install --python "$PYTHON" "$@"
    return
  fi
  if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    log "Bootstrapping pip in $VENV_DIR"
    "$PYTHON" -m ensurepip --upgrade || {
      printf 'pip is unavailable in %s and ensurepip failed.\n' "$VENV_DIR" >&2
      printf 'Install uv (https://docs.astral.sh/uv/) or recreate the environment with: %s -m venv --clear %s\n' \
        "$PYTHON_BIN" "$VENV_DIR" >&2
      exit 1
    }
  fi
  "$PYTHON" -m pip install --upgrade pip >/dev/null
  "$PYTHON" -m pip install "$@"
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
  "$PYTHON" -c 'import matchmaker, coverage, django, email_validator, exrex, graphviz, marshmallow, matplotlib, numpy, pandas, pydantic, dateutil, regex, seaborn, tqdm, validators, xeger'
  "$PYTHON" benchmarks/python_regex_function_coverage_benchmark.py --help >/dev/null
  "$PYTHON" benchmarks/python_regex_function_combined_tools_coverage_benchmark.py --help >/dev/null
}

main() {
  select_python_bin
  use_homebrew_jdk_if_present
  install_system_dependencies
  select_python_bin
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
    "$PYTHON" "benchmarks/python_regex_function_coverage_benchmark.py"
  printf 'Combined benchmark:\n'
  printf '  %q %q --runs 1 --n-positive 1000 --n-negative 1000 --workers 1\n' \
    "$PYTHON" "benchmarks/python_regex_function_combined_tools_coverage_benchmark.py"
}

main "$@"
