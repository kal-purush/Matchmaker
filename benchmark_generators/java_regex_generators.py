#!/usr/bin/env python3
"""Python wrappers for the Java Generex and RgxGen regex generators.

The public functions in this module take a regex and an integer N, then return
N generated strings by invoking the Java library APIs in a subprocess.

Classpath options, in priority order:
  1. pass classpath=...
  2. set RGXGEN_CLASSPATH, GENEREX_CLASSPATH, or JAVA_REGEX_GENERATOR_CLASSPATH
  3. set JAVA_REGEX_GENERATOR_JARS to a directory of jars or a path-separated
     list of jar files/directories
  4. allow Maven to resolve dependencies automatically (default)
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

try:
    from ._jdk import ensure_jdk_on_path
except ImportError:  # executed as a script: benchmark_generators/ is on sys.path
    from _jdk import ensure_jdk_on_path


ToolName = Literal["rgxgen", "generex"]

RGXGEN_MAVEN_COORDINATE = "com.github.curious-odd-man:rgxgen:3.1"
GENEREX_MAVEN_COORDINATE = "com.github.mifmif:generex:1.0.2"

_BRIDGE_CLASS_NAME = "RegexGeneratorBridge"
_BRIDGE_SOURCE = r'''
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.NoSuchMethodException;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashSet;
import java.util.Random;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;

public class RegexGeneratorBridge {
    public static void main(String[] args) throws Exception {
        if (args.length == 2 && "validate".equals(args[0])) {
            validateJavaRegex(args[1]);
            return;
        }

        if (args.length != 8) {
            throw new IllegalArgumentException("Expected 8 args: tool count regexB64 seed negative unique minLen maxLen");
        }

        String tool = args[0];
        int count = Integer.parseInt(args[1]);
        String pattern = new String(Base64.getDecoder().decode(args[2]), StandardCharsets.UTF_8);
        String seedText = args[3];
        boolean negative = Boolean.parseBoolean(args[4]);
        boolean unique = Boolean.parseBoolean(args[5]);
        int minLen = Integer.parseInt(args[6]);
        int maxLen = Integer.parseInt(args[7]);

        Random random = seedText.isEmpty() ? null : new Random(Long.parseLong(seedText));
        Set<String> seen = unique ? new LinkedHashSet<String>() : null;
        int emitted = 0;
        int attempts = 0;
        int maxAttempts = Math.max(100, count * 100);

        while (emitted < count && attempts < maxAttempts) {
            attempts++;
            String value;
            if ("rgxgen".equals(tool)) {
                value = generateWithRgxGen(pattern, random, negative);
            } else if ("generex".equals(tool)) {
                if (negative) {
                    throw new IllegalArgumentException("Generex does not expose a generate-not-matching API");
                }
                value = generateWithGenerex(pattern, minLen, maxLen);
            } else {
                throw new IllegalArgumentException("Unknown tool: " + tool);
            }

            if (unique && !seen.add(value)) {
                continue;
            }

            System.out.println(Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8)));
            emitted++;
        }

        if (emitted < count) {
            System.err.println("Generated " + emitted + " values after " + attempts + " attempts");
            System.exit(2);
        }
    }

    private static void validateJavaRegex(String patternB64) {
        String pattern = new String(Base64.getDecoder().decode(patternB64), StandardCharsets.UTF_8);
        try {
            Pattern.compile(pattern);
            System.out.println("OK");
        } catch (PatternSyntaxException ex) {
            System.out.println("ERROR");
            System.out.println(Base64.getEncoder().encodeToString(ex.getDescription().getBytes(StandardCharsets.UTF_8)));
            System.out.println(ex.getIndex());
            System.out.println(Base64.getEncoder().encodeToString(ex.getPattern().getBytes(StandardCharsets.UTF_8)));
        }
    }

    private static String generateWithRgxGen(String pattern, Random random, boolean negative) throws Exception {
        Class<?> rgxGenClass = Class.forName("com.github.curiousoddman.rgxgen.RgxGen");
        Method parse = rgxGenClass.getMethod("parse", String.class);
        Object rgxGen = parse.invoke(null, pattern);

        if (negative) {
            return invokeRgxGenStringMethod(rgxGenClass, rgxGen, "generateNotMatching", random);
        }

        return invokeRgxGenStringMethod(rgxGenClass, rgxGen, "generate", random);
    }

    private static String invokeRgxGenStringMethod(Class<?> rgxGenClass, Object rgxGen, String methodName, Random random) throws Exception {
        if (random != null) {
            try {
                return (String) rgxGenClass.getMethod(methodName, Random.class).invoke(rgxGen, random);
            } catch (NoSuchMethodException ignored) {
                // Older jars expose only the unseeded methods.
            }
        }
        return (String) rgxGenClass.getMethod(methodName).invoke(rgxGen);
    }

    private static String generateWithGenerex(String pattern, int minLen, int maxLen) throws Exception {
        Class<?> generexClass = Class.forName("com.mifmif.common.regex.Generex");
        Constructor<?> constructor = generexClass.getConstructor(String.class);
        Object generex = constructor.newInstance(pattern);

        if (minLen >= 0 || maxLen >= 0) {
            if (minLen < 0 || maxLen < 0) {
                throw new IllegalArgumentException("Both minLen and maxLen are required for Generex length bounds");
            }
            return (String) generexClass.getMethod("random", int.class, int.class).invoke(generex, minLen, maxLen);
        }

        return (String) generexClass.getMethod("random").invoke(generex);
    }
}
'''


@dataclass(frozen=True)
class JavaRegexGenerator:
    """Subprocess-backed adapter for Generex and RgxGen."""

    classpath: str | None = None
    java_bin: str = "java"
    javac_bin: str = "javac"
    cache_dir: Path = Path(__file__).resolve().parent / ".java_regex_bridge"
    allow_maven: bool = True
    timeout_seconds: float | None = 120

    def generate(
        self,
        pattern: str,
        n: int,
        tool: ToolName,
        *,
        seed: int | None = None,
        unique: bool = False,
        negative: bool = False,
        min_length: int | None = None,
        max_length: int | None = None,
    ) -> list[str]:
        """Generate N inputs for pattern using ``tool``.

        ``negative=True`` is supported only by RgxGen. ``seed`` is also RgxGen
        only because Generex 1.0.2 does not document a seeded random API.
        """

        if n < 0:
            raise ValueError("n must be >= 0")
        if tool not in ("rgxgen", "generex"):
            raise ValueError("tool must be 'rgxgen' or 'generex'")
        if tool == "generex" and negative:
            raise ValueError("Generex does not support negative generation")
        if tool == "generex" and seed is not None:
            raise ValueError("Generex does not support seeded generation through this wrapper")

        bridge_dir = self._compile_bridge()
        classpath = os.pathsep.join([str(bridge_dir), self._resolve_classpath(tool)])
        regex_b64 = base64.b64encode(pattern.encode("utf-8")).decode("ascii")
        args = [
            self.java_bin,
            "-cp",
            classpath,
            _BRIDGE_CLASS_NAME,
            tool,
            str(n),
            regex_b64,
            "" if seed is None else str(seed),
            str(negative).lower(),
            str(unique).lower(),
            str(-1 if min_length is None else min_length),
            str(-1 if max_length is None else max_length),
        ]

        completed = subprocess.run(
            args,
            text=True,
            capture_output=True,
            check=False,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{tool} generation failed with exit code {completed.returncode}:\n"
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )

        return [
            base64.b64decode(line).decode("utf-8")
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

    def _compile_bridge(self) -> Path:
        _require_working_java_tool(self.java_bin, "Java runtime")
        _require_working_java_tool(self.javac_bin, "Java compiler")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        source_path = self.cache_dir / f"{_BRIDGE_CLASS_NAME}.java"
        class_path = self.cache_dir / f"{_BRIDGE_CLASS_NAME}.class"

        if not source_path.exists() or source_path.read_text() != _BRIDGE_SOURCE:
            source_path.write_text(_BRIDGE_SOURCE)
            if class_path.exists():
                class_path.unlink()

        if not class_path.exists():
            completed = subprocess.run(
                [self.javac_bin, str(source_path)],
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "Failed to compile Java bridge. Install a working JDK and try again.\n"
                    + (completed.stderr.strip() or completed.stdout.strip())
                )

        return self.cache_dir

    def _resolve_classpath(self, tool: ToolName) -> str:
        explicit = self.classpath
        env_specific = os.environ.get("RGXGEN_CLASSPATH" if tool == "rgxgen" else "GENEREX_CLASSPATH")
        env_shared = os.environ.get("JAVA_REGEX_GENERATOR_CLASSPATH")
        env_jars = os.environ.get("JAVA_REGEX_GENERATOR_JARS")

        for candidate in (explicit, env_specific, env_shared):
            if candidate:
                return candidate

        if env_jars:
            jars = list(_expand_jars(env_jars))
            if jars:
                return os.pathsep.join(jars)

        if self.allow_maven:
            return self._maven_classpath(tool)

        raise RuntimeError(
            "No Java classpath configured. Set RGXGEN_CLASSPATH, GENEREX_CLASSPATH, "
            "JAVA_REGEX_GENERATOR_CLASSPATH, or JAVA_REGEX_GENERATOR_JARS."
        )

    def _maven_classpath(self, tool: ToolName) -> str:
        if shutil.which("mvn") is None:
            raise RuntimeError("Maven is not installed; provide a jar classpath explicitly")

        coordinate = RGXGEN_MAVEN_COORDINATE if tool == "rgxgen" else GENEREX_MAVEN_COORDINATE
        pom_path = self.cache_dir / f"{tool}-pom.xml"
        classpath_path = self.cache_dir / f"{tool}-classpath.txt"
        group_id, artifact_id, version = coordinate.split(":")
        pom_path.write_text(
            f"""<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>local.regex</groupId>
  <artifactId>{tool}-bridge</artifactId>
  <version>1</version>
  <dependencies>
    <dependency>
      <groupId>{group_id}</groupId>
      <artifactId>{artifact_id}</artifactId>
      <version>{version}</version>
    </dependency>
  </dependencies>
</project>
"""
        )

        completed = subprocess.run(
            [
                "mvn",
                "-q",
                "-f",
                str(pom_path),
                "dependency:build-classpath",
                f"-Dmdep.outputFile={classpath_path}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Maven could not resolve Java dependencies. Provide an explicit classpath instead.\n"
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        return classpath_path.read_text().strip()

    def validate_java_regex(self, pattern: str, tool: ToolName = "rgxgen") -> dict[str, object]:
        """Return Java Pattern.compile validity details for pattern."""

        bridge_dir = self._compile_bridge()
        classpath = os.pathsep.join([str(bridge_dir), self._resolve_classpath(tool)])
        pattern_b64 = base64.b64encode(pattern.encode("utf-8")).decode("ascii")
        completed = subprocess.run(
            [self.java_bin, "-cp", classpath, _BRIDGE_CLASS_NAME, "validate", pattern_b64],
            text=True,
            capture_output=True,
            check=False,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            return {
                "java_regex_valid": None,
                "java_regex_error": completed.stderr.strip() or completed.stdout.strip(),
                "java_regex_error_index": None,
                "java_regex_error_pattern": pattern,
            }

        lines = completed.stdout.splitlines()
        if lines and lines[0] == "OK":
            return {
                "java_regex_valid": True,
                "java_regex_error": None,
                "java_regex_error_index": None,
                "java_regex_error_pattern": None,
            }
        if lines and lines[0] == "ERROR":
            description = base64.b64decode(lines[1]).decode("utf-8") if len(lines) > 1 else ""
            error_index = int(lines[2]) if len(lines) > 2 and lines[2].lstrip("-").isdigit() else None
            error_pattern = base64.b64decode(lines[3]).decode("utf-8") if len(lines) > 3 else pattern
            return {
                "java_regex_valid": False,
                "java_regex_error": description,
                "java_regex_error_index": error_index,
                "java_regex_error_pattern": error_pattern,
            }

        return {
            "java_regex_valid": None,
            "java_regex_error": completed.stdout.strip(),
            "java_regex_error_index": None,
            "java_regex_error_pattern": pattern,
        }


def generate_rgxgen(
    pattern: str,
    n: int,
    *,
    seed: int | None = None,
    unique: bool = False,
    negative: bool = False,
    classpath: str | None = None,
    timeout_seconds: float | None = 120,
) -> list[str]:
    """Generate N matching, or optionally non-matching, strings with RgxGen."""

    return JavaRegexGenerator(classpath=classpath, timeout_seconds=timeout_seconds).generate(
        pattern,
        n,
        "rgxgen",
        seed=seed,
        unique=unique,
        negative=negative,
    )


def generate_generex(
    pattern: str,
    n: int,
    *,
    unique: bool = False,
    min_length: int | None = None,
    max_length: int | None = None,
    classpath: str | None = None,
    timeout_seconds: float | None = 120,
) -> list[str]:
    """Generate N matching strings with Generex."""

    return JavaRegexGenerator(classpath=classpath, timeout_seconds=timeout_seconds).generate(
        pattern,
        n,
        "generex",
        unique=unique,
        min_length=min_length,
        max_length=max_length,
    )


def validate_java_regex(
    pattern: str,
    *,
    classpath: str | None = None,
    tool: ToolName = "rgxgen",
    timeout_seconds: float | None = 120,
) -> dict[str, object]:
    return JavaRegexGenerator(classpath=classpath, timeout_seconds=timeout_seconds).validate_java_regex(
        pattern,
        tool=tool,
    )


def _expand_jars(paths: str) -> Iterable[str]:
    for raw_path in paths.split(os.pathsep):
        path = Path(raw_path).expanduser()
        if path.is_dir():
            yield from sorted(str(jar) for jar in path.glob("*.jar"))
        elif path.suffix == ".jar" and path.exists():
            yield str(path)


def _require_working_java_tool(binary: str, label: str) -> None:
    ensure_jdk_on_path()
    if shutil.which(binary) is None:
        raise RuntimeError(f"{label} not found: {binary}. Install a JDK and try again.")

    completed = subprocess.run(
        [binary, "-version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"{label} is not usable: {binary}. Install a working JDK and try again."
            + (f"\n{detail}" if detail else "")
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate regex samples with Generex or RgxGen")
    parser.add_argument("tool", choices=["generex", "rgxgen"])
    parser.add_argument("regex")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--classpath")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--unique", action="store_true")
    parser.add_argument("--negative", action="store_true", help="RgxGen only")
    parser.add_argument("--min-length", type=int, help="Generex only")
    parser.add_argument("--max-length", type=int, help="Generex only")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--validate-java-regex", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    generator = JavaRegexGenerator(classpath=args.classpath, timeout_seconds=args.timeout_seconds)
    if args.validate_java_regex:
        details = generator.validate_java_regex(args.regex, tool=args.tool)
        print(details)
        raise SystemExit(0 if details.get("java_regex_valid") is True else 1)

    try:
        samples = generator.generate(
            args.regex,
            args.n,
            args.tool,
            seed=args.seed,
            unique=args.unique,
            negative=args.negative,
            min_length=args.min_length,
            max_length=args.max_length,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    for sample in samples:
        print(sample)


if __name__ == "__main__":
    main()
