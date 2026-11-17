"""Locate a usable JDK when the `java` on PATH is missing or a stub.

On macOS, /usr/bin/java is a placeholder that fails unless a JDK is registered
with the system, and Homebrew's openjdk is keg-only (installed but not on
PATH). This helper finds a working JDK and puts it on PATH (and sets
JAVA_HOME) for the current process, so java, javac, and Maven subprocesses
all use it.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

_HOMEBREW_OPENJDK_PREFIXES = ("/opt/homebrew/opt/openjdk", "/usr/local/opt/openjdk")


def _java_works(java: str) -> bool:
    try:
        return subprocess.run([java, "-version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _candidate_java_homes() -> Iterator[Path]:
    if os.environ.get("JAVA_HOME"):
        yield Path(os.environ["JAVA_HOME"])
    for prefix in _HOMEBREW_OPENJDK_PREFIXES:
        yield Path(prefix) / "libexec" / "openjdk.jdk" / "Contents" / "Home"
        yield Path(prefix)
    java_home_tool = Path("/usr/libexec/java_home")
    if java_home_tool.exists():
        try:
            completed = subprocess.run([str(java_home_tool)], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return
        if completed.returncode == 0 and completed.stdout.strip():
            yield Path(completed.stdout.strip())


@functools.cache
def ensure_jdk_on_path() -> None:
    """Prepend a working JDK's bin directory to PATH if the current one is unusable."""

    java = shutil.which("java")
    if java and shutil.which("javac") and _java_works(java):
        return
    for home in _candidate_java_homes():
        bin_dir = home / "bin"
        java_path = bin_dir / "java"
        if java_path.exists() and (bin_dir / "javac").exists() and _java_works(str(java_path)):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            os.environ["JAVA_HOME"] = str(home)
            return
