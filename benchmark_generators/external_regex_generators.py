#!/usr/bin/env python3
"""Subprocess adapters for RandExp.js and MutRex."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from ._jdk import ensure_jdk_on_path
except ImportError:  # executed as a script: benchmark_generators/ is on sys.path
    from _jdk import ensure_jdk_on_path


REPO_ROOT = Path(__file__).resolve().parent.parent
RANDEXP_BRIDGE = Path(__file__).resolve().with_name("randexp_bridge.js")
MUTREX_BRIDGE_CLASS = "MutRexBridge"
MUTREX_BRIDGE_SOURCE = r'''
import java.nio.charset.StandardCharsets;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.util.Base64;
import java.util.HashSet;
import java.util.Set;
import java.util.logging.Level;
import java.util.logging.Logger;
import dk.brics.automaton.OORegexConverter;
import regex.distinguishing.DistinguishingString;
import regex.mutrex.ds.DSSet;
import regex.mutrex.main.GeneratorType;
import regex.mutrex.main.MutRex;
import regex.operators.Char2MetaChar;

public class MutRexBridge {
    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException("Expected one base64-encoded regex argument");
        }
        Logger.getLogger(OORegexConverter.class.getName()).setLevel(Level.OFF);
        Logger.getLogger(Char2MetaChar.class.getName()).setLevel(Level.OFF);

        String pattern = new String(Base64.getDecoder().decode(args[0]), StandardCharsets.UTF_8);
        PrintStream originalOut = System.out;
        DSSet samples;
        try {
            System.setOut(new PrintStream(new ByteArrayOutputStream()));
            samples = MutRex.generateStrings(
                pattern,
                GeneratorType.BASIC,
                GeneratorType.Orientation.RANDOM
            );
        } finally {
            System.setOut(originalOut);
        }
        Set<String> emitted = new HashSet<String>();
        for (DistinguishingString sample : samples) {
            String value = sample.getDs();
            String label = sample.isConfirming() ? "CONF" : "REJECT";
            String key = label + "\u0000" + value;
            if (!emitted.add(key)) {
                continue;
            }
            String encoded = Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8));
            System.out.println(label + "\t" + encoded);
        }
    }
}
'''


class ExternalGeneratorError(RuntimeError):
    """Generator failure with JSON-serializable diagnostic details."""

    def __init__(self, message: str, *, failure_details: dict[str, Any] | None = None):
        super().__init__(message)
        self.failure_details = failure_details or {}


@dataclass(frozen=True)
class MutRexSample:
    value: str
    expected_match: bool


def validate_javascript_regex(
    pattern: str,
    *,
    node_bin: str = "node",
    timeout_seconds: float | None = 120,
) -> dict[str, Any]:
    _require_binary(node_bin, "Node.js runtime")
    encoded = base64.b64encode(pattern.encode("utf-8")).decode("ascii")
    try:
        completed = subprocess.run(
            [node_bin, str(RANDEXP_BRIDGE), "validate", encoded],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "javascript_regex_valid": None,
            "javascript_regex_error": f"Validation timed out after {exc.timeout}s",
        }
    if completed.returncode != 0:
        return {
            "javascript_regex_valid": None,
            "javascript_regex_error": completed.stderr.strip() or completed.stdout.strip(),
        }
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "javascript_regex_valid": None,
            "javascript_regex_error": f"Unexpected validation output: {completed.stdout.strip()}",
        }


def generate_randexp(
    pattern: str,
    n: int,
    *,
    seed: int = 0,
    node_bin: str = "node",
    timeout_seconds: float | None = 120,
) -> list[str]:
    if n < 0:
        raise ValueError("n must be >= 0")
    if n == 0:
        return []
    _require_binary(node_bin, "Node.js runtime")
    encoded = base64.b64encode(pattern.encode("utf-8")).decode("ascii")
    try:
        completed = subprocess.run(
            [node_bin, str(RANDEXP_BRIDGE), "generate", encoded, str(n), str(seed)],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalGeneratorError(
            f"RandExp.js timed out after {exc.timeout}s",
            failure_details={
                "tool": "RandExp.js",
                "regex_engine": "JavaScript RegExp / randexp",
                "regex_preserved": True,
                "timeout_seconds": exc.timeout,
            },
        ) from exc
    if completed.returncode != 0:
        validity = validate_javascript_regex(
            pattern,
            node_bin=node_bin,
            timeout_seconds=timeout_seconds,
        )
        if validity.get("javascript_regex_valid") is True:
            prefix = "JavaScript regex valid, RandExp.js failed"
        elif validity.get("javascript_regex_valid") is False:
            prefix = f"JavaScript regex invalid: {validity.get('javascript_regex_error')}"
        else:
            prefix = "JavaScript regex validity unknown"
        detail = completed.stderr.strip() or completed.stdout.strip()
        failure_details = {
            "tool": "RandExp.js",
            "regex_engine": "JavaScript RegExp / randexp",
            "regex_preserved": True,
            **validity,
            "generator_error": detail[:2000],
        }
        raise ExternalGeneratorError(
            f"{prefix}: {detail}"[:2000],
            failure_details=failure_details,
        )
    return [
        base64.b64decode(line).decode("utf-8")
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def generate_mutrex(
    pattern: str,
    n_positive: int,
    n_negative: int,
    *,
    jar_path: str | os.PathLike[str] | None = None,
    java_bin: str = "java",
    javac_bin: str = "javac",
    timeout_seconds: float | None = 120,
) -> list[MutRexSample]:
    if n_positive < 0 or n_negative < 0:
        raise ValueError("MutRex sample counts must be >= 0")
    if n_positive == 0 and n_negative == 0:
        return []
    _require_binary(java_bin, "Java runtime")
    _require_binary(javac_bin, "Java compiler")
    jar = _resolve_mutrex_jar(jar_path)
    bridge_dir = _compile_mutrex_bridge(jar, javac_bin, timeout_seconds)
    classpath = os.pathsep.join([str(bridge_dir), str(jar)])
    encoded = base64.b64encode(pattern.encode("utf-8")).decode("ascii")
    try:
        completed = subprocess.run(
            [java_bin, "-cp", classpath, MUTREX_BRIDGE_CLASS, encoded],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalGeneratorError(
            f"MutRex timed out after {exc.timeout}s",
            failure_details={
                "tool": "MutRex",
                "regex_engine": "dk.brics.automaton",
                "regex_preserved": True,
                "timeout_seconds": exc.timeout,
            },
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ExternalGeneratorError(
            f"MutRex failed: {detail}"[:2000],
            failure_details={
                "tool": "MutRex",
                "regex_engine": "dk.brics.automaton",
                "regex_preserved": True,
                "generator_error": detail[:2000],
            },
        )

    positives: list[MutRexSample] = []
    negatives: list[MutRexSample] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            label, encoded_value = line.split("\t", 1)
            sample = MutRexSample(
                value=base64.b64decode(encoded_value).decode("utf-8"),
                expected_match=label == "CONF",
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise ExternalGeneratorError(
                f"Unexpected MutRex bridge output: {line!r}",
                failure_details={
                    "tool": "MutRex",
                    "regex_engine": "dk.brics.automaton",
                    "regex_preserved": True,
                    "bridge_output": line[:1000],
                },
            ) from exc
        (positives if sample.expected_match else negatives).append(sample)
    return positives[:n_positive] + negatives[:n_negative]


def _resolve_mutrex_jar(jar_path: str | os.PathLike[str] | None) -> Path:
    candidate = Path(jar_path or os.environ.get("MUTREX_JAR", REPO_ROOT / "mutrex.jar")).expanduser()
    if not candidate.is_file():
        raise ExternalGeneratorError(
            f"MutRex JAR not found: {candidate}",
            failure_details={
                "tool": "MutRex",
                "regex_engine": "dk.brics.automaton",
                "regex_preserved": True,
                "jar_path": str(candidate),
            },
        )
    return candidate.resolve()


def _compile_mutrex_bridge(jar: Path, javac_bin: str, timeout_seconds: float | None) -> Path:
    bridge_dir = Path(__file__).resolve().parent / ".external_regex_bridge"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    source_path = bridge_dir / f"{MUTREX_BRIDGE_CLASS}.java"
    class_path = bridge_dir / f"{MUTREX_BRIDGE_CLASS}.class"
    if not source_path.exists() or source_path.read_text(encoding="utf-8") != MUTREX_BRIDGE_SOURCE:
        source_path.write_text(MUTREX_BRIDGE_SOURCE, encoding="utf-8")
        class_path.unlink(missing_ok=True)
    if not class_path.exists():
        try:
            completed = subprocess.run(
                [javac_bin, "-cp", str(jar), str(source_path)],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExternalGeneratorError(f"MutRex bridge compilation timed out after {exc.timeout}s") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ExternalGeneratorError(f"Failed to compile MutRex bridge: {detail}")
    return bridge_dir


def _require_binary(binary: str, label: str) -> None:
    if binary in ("java", "javac"):
        ensure_jdk_on_path()
    if shutil.which(binary) is None:
        raise ExternalGeneratorError(f"{label} not found: {binary}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate samples with RandExp.js or MutRex")
    parser.add_argument("tool", choices=("randexp", "mutrex"))
    parser.add_argument("regex")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--n-negative", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.tool == "randexp":
        samples = generate_randexp(
            args.regex,
            args.n,
            seed=args.seed,
            timeout_seconds=args.timeout_seconds,
        )
        for sample in samples:
            print(repr(sample))
        return
    for sample in generate_mutrex(
        args.regex,
        args.n,
        args.n_negative,
        timeout_seconds=args.timeout_seconds,
    ):
        label = "CONF" if sample.expected_match else "REJECT"
        print(f"{label}\t{sample.value!r}")


if __name__ == "__main__":
    main()
