# Benchmark Generators

Self-contained positive and negative regex sample generators for benchmarking.
These modules do not import from the rest of the repository.

## Complete Environment Setup

From the repository root, run:

```bash
./setup_coverage_benchmarks.sh
```

The script creates `.venv`, installs the pinned Python and Node.js dependencies,
checks the JDK, Maven, and `mutrex.jar`, builds EGRET for the active Python ABI,
resolves the Java generator dependencies, and smoke-tests both coverage benchmark
entry points. Missing system packages are installed through Homebrew or APT by
default. Set `AUTO_INSTALL_SYSTEM_DEPS=0` to check without installing them.

## Positive Samples

```bash
python3 benchmark_generators/positive_generator.py '[a-z]{2}\d' --n 20 --mode diverse
python3 benchmark_generators/positive_generator.py '[a-z]{2}\d' --n 20 --mode diverse --no-validate
```

Modes:

- `sample`
- `heuristic`
- `diverse`
- `high_coverage`

## Negative Samples

```bash
python3 benchmark_generators/negative_generator.py '[a-z]{2}\d' --n 20 --mode diverse
python3 benchmark_generators/negative_generator.py '[a-z]{2}\d' --n 20 --mode diverse --no-validate
```

Modes:

- `default`
- `heuristic`
- `diverse`
- `high_coverage`

## Validation Flag

By default, both generators preserve the current internal validation behavior.
Pass `--no-validate` or `validate=False` to disable internal regex matching/filtering.
Use `validate=False` for correctness benchmarks where all returned candidates should be
validated externally with the same oracle for every tool.

## External Generators

### Generex and RgxGen

`java_regex_generators.py` wraps the Java APIs from:

- `com.github.mifmif:generex:1.0.2`
- `com.github.curious-odd-man:rgxgen:3.1`

Requirements:

- A working JDK, not only the macOS `/usr/bin/java` launcher stub.
- Maven, unless you provide the jars yourself with `--classpath` or an environment variable.

On macOS with Homebrew:

```bash
brew install openjdk maven
```

Python usage:

```python
from benchmark_generators.java_regex_generators import generate_generex, generate_rgxgen

regex = r"[a-z]{2}\d"

generex_samples = generate_generex(regex, 20)
rgxgen_samples = generate_rgxgen(regex, 20, seed=123)
rgxgen_negative_samples = generate_rgxgen(regex, 20, negative=True)
```

CLI usage:

```bash
python3 benchmark_generators/java_regex_generators.py generex '[a-z]{2}\d' --n 20
python3 benchmark_generators/java_regex_generators.py rgxgen '[a-z]{2}\d' --n 20 --seed 123
python3 benchmark_generators/java_regex_generators.py rgxgen '[a-z]{2}\d' --n 20 --negative
```

### RandExp.js and MutRex

`external_regex_generators.py` wraps the installed `randexp` Node.js package and
the workspace-root `mutrex.jar`. Set `MUTREX_JAR` to override the JAR path.

```bash
python3 benchmark_generators/external_regex_generators.py randexp '[a-z]{2}\d' --n 20 --seed 123
python3 benchmark_generators/external_regex_generators.py mutrex '[a-z]{2}\d' --n 20 --n-negative 20
```

RandExp.js generates positive samples only. MutRex returns a finite mutation
suite, so it may produce fewer than the requested positive or negative count.
The coverage benchmark checks every MutRex `CONF`/`REJECT` label against Python
`re.fullmatch` and records dialect disagreements as tool failures.

Function coverage benchmark usage:

```bash
python3 benchmarks/python_regex_function_coverage_benchmark.py --n-positive 1000 --n-negative 1000
python3 benchmarks/python_regex_function_coverage_benchmark.py --tools ours,exrex,xeger,egret,generex,rgxgen,randexp,mutrex --n-positive 1000 --n-negative 1000
python3 benchmarks/python_regex_function_coverage_benchmark.py --tools rgxgen --targets uuid_stdlib --n-positive 20 --n-negative 20
```

The default tool set is all eight tools: `ours`, `exrex`, `xeger`, `egret`,
`generex`, `rgxgen`, `randexp`, and `mutrex`. Subprocess-backed tools default to
a 120-second timeout; change it with `--external-tool-timeout-seconds`.

Combined coverage benchmark:

```bash
python3 benchmarks/python_regex_function_combined_tools_coverage_benchmark.py \
  --runs 1 --n-positive 1000 --n-negative 1000 \
  --external-tool-timeout-seconds 120
```

The combined comparison uses seven component tools against RegexInstantiator.

By default, the wrapper asks Maven to resolve the jars. To use local jars instead,
set one of these environment variables or pass `--classpath`:

```bash
export JAVA_REGEX_GENERATOR_JARS=/path/to/jars
export RGXGEN_CLASSPATH=/path/to/rgxgen.jar:/path/to/dependency.jar
export GENEREX_CLASSPATH=/path/to/generex.jar:/path/to/dependency.jar
```
