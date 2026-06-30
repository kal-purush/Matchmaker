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

## Requirements

- Python 3.13 is the tested artifact environment.
- The third-party [`regex`](https://pypi.org/project/regex/) package is optional
  but recommended for the intended parsing, validation, and timeout behavior.

All other generator dependencies are from the Python standard library. Java,
Node.js, and external regex generators are needed only by comparison
benchmarks, not by Matchmaker itself.

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install regex
```

The repository currently has no package metadata for installation, so run the
examples from the repository root or add that directory to `PYTHONPATH`.

## Quick Start

### Positive inputs: evaluated compact sampler

The ICSE evaluation uses the compact graph and the constructive lookaround
sampler:

```python
from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.compact_sampler_lookaround_experimental import (
    ExperimentalLookaroundCompactGraphSampler,
)

pattern = r"([A-Z]{2})\d{3}\1"
builder = CompactRegexGraphBuilder(pattern, validate=True)
sampler = ExperimentalLookaroundCompactGraphSampler(builder, validate=True)

positives = sampler.generate_samples(n=20, seed=0)
for sample in positives:
    print(repr(sample))
```

For the baseline compact sampler without targeted constructive lookaround
handling:

```python
from regex_positive_generator.experimental import (
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
from regex_positive_generator.experimental import CompactRegexGraphBuilder

positives = CompactRegexGraphBuilder(r"[a-z]{2}\d").generate_samples(
    n=20,
    seed=0,
    validate=True,
)
```

### Negative inputs

```python
from regex_positive_generator import generate_negative_samples

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

### `ExperimentalLookaroundCompactGraphSampler`

This is the positive sampler used by the current coverage evaluation. It adds
targeted constructive handling for:

- Positive lookahead of the form `(?=.*CLASS)`.
- Negative lookahead with finite prefixes.
- Positive and negative fixed-width finite lookbehind.

Unsupported lookaround shapes are reported by `lookaround_report()` and rely
on final validation rather than constructive satisfaction.

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

When installed, the `regex` package is preferred for parsing and final
validation. Otherwise Matchmaker falls back to Python's standard `re` module.
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
from regex_positive_generator import generate_positive_samples
```

This convenience function currently uses the older flat `RegexGraphBuilder`
implementation. It is retained for compatibility, but it is not the compact
positive sampler used for the reported ICSE evaluation. Use the compact APIs
shown above when reproducing the paper results.

## Source Layout

```text
regex_positive_generator/
  parser.py                         parsing and syntax normalization
  charset.py                        reproducible character representatives
  repeats.py                        effective unbounded-repeat policy
  negative.py                       public negative-generation export
  experimental/
    compact_graph.py                compact hierarchical graph
    compact_sampler.py              indexed positive sampler
    compact_sampler_lookaround_experimental.py
                                     constructive lookaround sampler
    counting.py                     saturating count arithmetic
    negative_context.py             prefix/suffix/capture contexts
    negative_choices.py             representative violating values
    negative_families.py            local and supplemental mutations
    negative_sampler.py             negative orchestration and validation
  tests/                             unit tests
  benchmarks/                        evaluation scripts and saved outputs
```

Benchmark code, external generator bridges, result files, and visualizations
are not runtime dependencies of Matchmaker.

## Running Tests

From the repository root:

```bash
.venv/bin/python -m unittest \
  regex_positive_generator.tests.test_compact_repeat \
  regex_positive_generator.tests.test_experimental_lookaround_sampler \
  regex_positive_generator.tests.test_negative_sampler
```

Some tests import benchmark helpers for correctness accounting, but benchmark
executables and external comparison tools are not required by the generator
APIs themselves.

## Research Artifact Notice

This code is provided as the research artifact for an ICSE 2027 submission.
When reporting results, identify the exact commit, Python version, installed
`regex` version, seed, validation setting, and sampler variant. These details can
affect represented character domains, accepted syntax, and generated samples.
