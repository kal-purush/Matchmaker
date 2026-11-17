# Ablation studies

Run the module commands from the repository root, in an environment with
Matchmaker and the `benchmarks` extra installed (for example, the `.venv`
created by `./setup_coverage_benchmarks.sh`):

```sh
python -m ablation.boundary_positive \
  --runs 1 --n-positive 200 --n-negative 200 \
  --targets uuid_pydantic,url_django_core

python -m ablation.boundary_positive --runs 10 --workers 8
python -m ablation.factorial --runs 10 --workers 8
```

## Boundary-positive study

The boundary-positive study compares the full system (`full`) against two
ablations. All three use the same negative samples and the same seed for each
run and target:

| Variant | Positive samples |
|---|---|
| `full` | Boundary strategy (`LookaroundCompactGraphSampler`) |
| `no_pos` | None, so the result shows what the negatives alone cover |
| `random_pos` | The same number, drawn uniformly at random (`RandomPositiveSampler`) |
| `walk_pos` | Opt-in: a plain random walk over the regex graph (`ablation/random_walk_sampler.py`): random branch, repeat count and character at each node, `*`/`+` up to 20 repeats, repeated strings kept |
| `balanced_pos` | Experimental, not in the default run: the boundary sampler with each alternation branch picked uniformly (`matchmaker.compact.balanced_sampler`); add it with `--variants full,no_pos,random_pos,balanced_pos` |

### Generation regexes

By default (`--regexes validator`) the study generates samples from the regexes in
[`validator_regexes.py`](validator_regexes.py), not from the main benchmark's.
Each benchmark regex is a practical approximation, and most validators accept
more than it does: `uuid.UUID` ignores dashes, `ipaddress` accepts compressed
IPv6, `fromisoformat` accepts any date/time separator, and so on. Negatives
generated from the benchmark regex are then often accepted by the validator
(742 of 1000 for `domain_stdlib_idna`), so `no_pos` still reaches the success
path and the study can't measure what positives contribute. The validator
regexes are written from each validator's code so that their negatives are
really rejected. The main benchmark is unchanged.

To see, for both regex sets, how many positives each validator rejects and how
many negatives it accepts:

```sh
python -m ablation.validator_regexes
```

A few mismatches remain where a regex can't express the validator exactly
(punycode label lengths in `email_validator`, WHATWG tab removal and IPv4
numbers in pydantic URLs). To reproduce the earlier behaviour, pass
`--regexes benchmark`.

### Running one comparison

The default runs all three variants. To run one comparison only:

```sh
python -m ablation.boundary_positive --variants full,no_pos
python -m ablation.boundary_positive --variants full,random_pos
```

The study reports each delta as `full − variant` in percentage points, paired by
run and target. A positive delta means the boundary positives cover more code.
At the end of a run it prints a summary for each variant and metric, with the
mean delta, bootstrap 95% CI, Holm-adjusted p-value, and counts of targets
where `full` is better, tied, or worse. The same numbers are written to
`delta_table.tex` and to `paired_tests` in `summary.json`.

## Outputs

Each coverage job runs in its own worker process. The factorial study runs both
the natural local-fault budget and the extended-local budget.

Boundary-study outputs are `results.jsonl`, `summary.json`, `table.tex`
(absolute coverage), `delta_table.tex` (deltas against `full`), and
`coverage_reports/run_XXX/<target>/<variant>/`. Factorial outputs are
`results.jsonl`, `summary.json`, `attributable.json`,
`factorial_table.tex`, `factorial_table_extended.tex`, and
`coverage_reports/{natural,extended_local}/run_XXX/<target>/<variant>/`.
Each coverage report directory contains the validator coverage JSON and HTML
report.
