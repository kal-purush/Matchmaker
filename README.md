# Matchmaker

Matchmaker generates positive and negative test inputs from regular expressions.
This directory contains the implementation accompanying the Matchmaker
submission to ICSE 2027.

The research artifact is under active development. Interfaces, supported regex
constructs, and output policies may change while the submission is under
review.

## What Matchmaker Generates

Given a regex, Matchmaker can generate:

- **Positive inputs** intended to match the complete regex.
- **Boundary-oriented positive inputs** that exercise extreme and
  representative interior character and repetition choices.
- **Local near-misses** produced by violating an eligible regex construct in a
  valid prefix/suffix context.
- **Broader negative inputs** from context-aware mutation families covering
  quantifiers, anchors, backreferences, separators, whitespace, structural
  changes, Unicode values, and stress cases.

With validation enabled, positive candidates are retained only when the regex
engine accepts them, and negative candidates are retained only when the engine
rejects them.

## Implementation Overview

Matchmaker parses a regex into a compact, hierarchical graph. Sequence, choice,
repeat, literal, and character-class nodes preserve the regex structure. Group,
backreference, conditional, anchor, and lookaround nodes carry capture state or
zero-width constraints.

Positive generation counts an approximate indexed choice space and un-ranks
indices into graph visits. It samples a simplified, boundary-oriented graph
first and then uses the fuller represented character space if more samples are
needed. Seeds make representative choices and random index selection
reproducible.

Negative generation reuses the compact graph to derive valid prefixes,
suffixes, capture state, and repetition context. It first constructs localized
violations and then draws from supplemental mutation families. Final regex
validation is the global rejection check.

## Installation

Matchmaker requires Python 3.13 or newer. Its only runtime dependency is the
[`regex`](https://pypi.org/project/regex/) package, which provides the intended
parsing, recursion, validation, and timeout behavior.

Install the library directly from a clone of this repository:

```bash
pip install .
# or, with uv
uv pip install .
```

For development, install it in editable mode with the test dependencies:

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
uv run pytest
```

Optional extras:

| Extra        | Contents                                                        |
|--------------|-----------------------------------------------------------------|
| `viz`        | `graphviz`, for rendering graphs as PNG (also needs `dot`)      |
| `benchmarks` | Exact versions of all Python packages used by the evaluation    |
| `dev`        | `pytest`                                                        |

Java, Node.js, and the external regex generators are needed only by the
comparison benchmarks, not by Matchmaker itself; see
[Reproducing the Evaluation](#reproducing-the-evaluation).

## Quick Start

### Positive inputs: evaluated compact sampler

The ICSE evaluation uses the compact graph and the constructive lookaround
sampler:

```python
from matchmaker.compact import CompactRegexGraphBuilder, LookaroundCompactGraphSampler

pattern = r"([A-Z]{2})\d{3}\1"
builder = CompactRegexGraphBuilder(pattern, validate=True)
sampler = LookaroundCompactGraphSampler(builder, validate=True)

positives = sampler.generate_samples(n=20, seed=0)
for sample in positives:
    print(repr(sample))
```

For the baseline compact sampler without targeted constructive lookaround
handling:

```python
from matchmaker.compact import (
    CompactGraphSampler,
    CompactRegexGraphBuilder,
)

builder = CompactRegexGraphBuilder(r"[a-z]{2}\d", validate=True)
positives = CompactGraphSampler(builder, validate=True).generate_samples(
    n=20,
    seed=0,
)
```

`CompactRegexGraphBuilder.generate_samples()` is a shorter form using the
baseline compact sampler:

```python
from matchmaker.compact import CompactRegexGraphBuilder

positives = CompactRegexGraphBuilder(r"[a-z]{2}\d").generate_samples(
    n=20,
    seed=0,
    validate=True,
)
```

### Negative inputs

```python
from matchmaker import generate_negative_samples

negatives = generate_negative_samples(
    r"[A-Z]{2}\d",
    n=20,
    seed=0,
    validate=True,
    use_fullmatch=True,
    timeout_seconds=10,
)

for sample in negatives:
    print(repr(sample))
```

The function returns at most `n` unique strings. It may return fewer when the
represented candidate space is finite or validation filters candidates.

## API Reference

### `CompactRegexGraphBuilder`

```python
CompactRegexGraphBuilder(
    regex_pattern,
    flags=0,
    validate=True,
    simplified=False,
    seed=0,
)
```

- `regex_pattern`: regex source string.
- `flags`: Python regex flags such as `re.IGNORECASE` or `re.MULTILINE`.
- `validate`: records the graph's validation policy; sampler validation is
  configured separately.
- `simplified`: restrict character sets to boundary and representative values.
- `seed`: controls reproducible representative selection in a simplified graph.

Important attributes include `root`, `nodes`, `effective_flags`, and
`requires_match_validation`.

### `CompactGraphSampler`

```python
CompactGraphSampler(builder, validate=True).generate_samples(
    n,
    seed=0,
    on_sample=None,
)
```

- `validate=True` filters candidates with full-string matching.
- `seed` controls representative values and random index selection.
- `on_sample`, when supplied, is called once for each retained sample.

### `LookaroundCompactGraphSampler`

This is the positive sampler used by the current coverage evaluation. It adds
targeted constructive handling for:

- Positive lookahead of the form `(?=.*CLASS)`.
- Negative lookahead with finite prefixes.
- Positive and negative fixed-width finite lookbehind.

Unsupported lookaround shapes are reported by `lookaround_report()` and rely
on final validation rather than constructive satisfaction.

This class was previously named `ExperimentalLookaroundCompactGraphSampler`;
that name remains available as an alias. In benchmark command lines and saved
results, this sampler is still identified as `lookaround-experimental`.

### `generate_negative_samples`

```python
generate_negative_samples(
    pattern,
    n=1000,
    seed=0,
    flags=0,
    validate=True,
    use_fullmatch=True,
    timeout_seconds=None,
    on_sample=None,
)
```

- `validate=True` retains only candidates rejected by the selected engine.
- `use_fullmatch=True` treats the regex as a complete-input specification.
  Set it to `False` to use search semantics.
- `timeout_seconds` bounds generation for the pattern. When omitted, the
  default is 10 seconds and can be changed with
  `REGEX_GRAPH_TIMEOUT_SECONDS`.
- `on_sample` receives each retained sample in output order.

## Validation Engines

The `regex` package is used for parsing and final validation. If it is
unavailable (for example, when running from source without installing),
Matchmaker falls back to Python's standard `re` module, which does not support
constructs such as recursion.
Validation uses `fullmatch` by default.

Validation is important because a local mutation can still match through an
alternative path. Likewise, complex zero-width or stateful constraints can
invalidate a positive graph traversal. Disabling validation exposes raw
generator behavior and is intended primarily for correctness experiments.

## Supported Constructs

The compact graph models the following principal constructs:

- Literals, character classes, wildcard, and predefined categories.
- Concatenation, alternation, and bounded or unbounded repetition.
- Capturing groups, backreferences, and capture-state conditionals.
- Start/end and word-boundary anchors.
- Lookahead and lookbehind nodes, with constructive support limited to the
  forms listed above.
- Selected atomic, possessive, POSIX-class, Unicode-property, and recursive
  syntax through parser normalization and validation.

Support means that the construct can be represented and considered during
generation. It does not imply complete enumeration of its language or fully
constructive handling for every interaction.

## Sampling and Reproducibility

- Outputs are unique within one generation call.
- Reusing the same regex, seed, flags, sampler, and environment produces a
  stable sequence for the covered deterministic paths.
- Simplified character classes retain their least and greatest represented
  values and one seeded interior value when available.
- Repetition ranges use the minimum, maximum, and one seeded interior count
  when available.
- Semantically unbounded repetitions use an effective upper bound of
  `minimum + 3`.
- Repeat bounds above 256 are rejected by the compact representation.
- Count arithmetic saturates rather than constructing arbitrarily large
  integers beyond the configured compact-count limit.

The requested `n` is an upper bound, not a guarantee. Underproduction can occur
for finite languages, duplicate derivations, unrealizable indexed choices,
timeouts, or validation-heavy patterns.

## Known Limitations

- Constructive lookaround support covers selected common forms, not arbitrary
  nested assertions.
- Wildcard generation uses a finite printable-character model and currently
  does not construct newline values for `re.DOTALL`.
- Unicode categories and properties are represented by finite implementation
  domains; they are not complete enumerations of all Unicode semantics.
- Multiline line-start handling is constructive, while internal multiline
  line-end generation is incomplete. Final validation can filter candidates but
  cannot create missing ones.
- Negative mutation families target intended violations, but the implementation
  does not prove that exactly one semantic condition is violated.
- Validation errors are currently treated as non-matches by the internal helper;
  artifact analyses that require strict oracle accounting should record such
  errors separately.
- Signal-based negative-generation timeouts are intended for Unix-like systems
  and the main interpreter thread.

## Public Convenience API

The package also exports:

```python
from matchmaker import generate_positive_samples
```

This convenience function currently uses the older flat `RegexGraphBuilder`
implementation. It is retained for compatibility, but it is not the compact
positive sampler used for the reported ICSE evaluation. Use the compact APIs
shown above when reproducing the paper results.

## Repository Layout

```text
pyproject.toml                       package metadata and extras
src/matchmaker/                      the installable library
  parser.py                          parsing and syntax normalization
  charset.py                         reproducible character representatives
  repeats.py                         effective unbounded-repeat policy
  negative.py                        public negative-generation export
  compact/
    compact_graph.py                 compact hierarchical graph
    compact_sampler.py               indexed positive sampler
    lookaround_sampler.py            constructive lookaround sampler
    counting.py                      saturating count arithmetic
    negative_context.py              prefix/suffix/capture contexts
    negative_choices.py              representative violating values
    negative_families.py             local and supplemental mutations
    negative_sampler.py              negative orchestration and validation
tests/                               unit tests
benchmarks/                          evaluation scripts and saved *_outputs/
ablation/                            ablation studies (see ablation/README.md)
benchmark_generators/                bridges to Generex, RgxGen, RandExp.js, MutRex
egret/                               EGRET source (GPL-3.0, upstream elarsonSU/egret)
data/                                regex corpora used by the benchmarks
visualizations/                      graph rendering script, notebook, figures
mutrex.jar, package.json             MutRex and RandExp.js for the comparison tools
setup_coverage_benchmarks.sh         one-shot benchmark environment setup
```

Only `src/matchmaker/` is installed by `pip install .`. Benchmark code, external
generator bridges, result files, and visualizations are not runtime
dependencies of Matchmaker.

## Running Tests

From the repository root, after `uv pip install -e ".[dev]"`:

```bash
pytest
```

This runs `tests/` and `ablation/tests/`. Ablation tests that need `numpy` are
skipped unless the `benchmarks` extra is installed.

## Reproducing the Evaluation

The benchmarks are run from a clone of this repository, not from an installed
wheel. To prepare the complete environment (a `.venv` with Matchmaker and the
pinned `benchmarks` extra, RandExp.js via npm, the Java generators via Maven,
MutRex, and a compiled EGRET extension), run:

```bash
./setup_coverage_benchmarks.sh
```

The script needs Node.js/npm, a JDK, Maven, make, a C++ compiler, and either
[uv](https://docs.astral.sh/uv/) (which provides Python 3.13 automatically) or
a system Python 3.13+. Missing system packages are installed through Homebrew
or APT; set `AUTO_INSTALL_SYSTEM_DEPS=0` to only check for them. A keg-only
Homebrew `openjdk` is detected automatically, both by the script and by the
Java generator bridges at benchmark time.

Then, from the repository root:

```bash
# Coverage of Python validator functions (individual tools, combined tools)
.venv/bin/python benchmarks/python_regex_function_coverage_benchmark.py \
  --runs 1 --n-positive 1000 --n-negative 1000 --workers 1
.venv/bin/python benchmarks/python_regex_function_combined_tools_coverage_benchmark.py \
  --runs 1 --n-positive 1000 --n-negative 1000 --workers 1
./run_python_regex_coverage_benchmarks.sh          # the 30-run configuration

# Positive and negative correctness on data/regex_correctness_benchmark_selected_patterns.jsonl
.venv/bin/python benchmarks/regex_correctness_benchmark.py --sampler lookaround-experimental
.venv/bin/python benchmarks/regex_negative_correctness_benchmark.py

# Generation speed on data/exponential-regexes.json and data/polynomial-regexes.json
.venv/bin/python benchmarks/regex_generation_speed_benchmark.py

# Ablation studies
.venv/bin/python -m ablation.boundary_positive --runs 10 --workers 8
.venv/bin/python -m ablation.factorial --runs 10 --workers 8
```

Every script accepts `--help`. Saved results from the reported runs are in
`benchmarks/*_outputs/`.

## Research Artifact Notice

This code is provided as the research artifact for an ICSE 2027 submission.
When reporting results, identify the exact commit, Python version, installed
`regex` version, seed, validation setting, and sampler variant. These details can
affect represented character domains, accepted syntax, and generated samples.
