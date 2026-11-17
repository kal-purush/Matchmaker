import argparse
from pathlib import Path

from .runner import print_delta_report, run_all, summarize, write_outputs
from .targets import TARGET_SETS, target_specs
from .validator_regexes import REGEX_SETS
from .variants import VARIANTS


def _arguments(study, default_variants):
    parser = argparse.ArgumentParser(description=f"Run the {study.replace('_', '-')} ablation.")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--variants", default=default_variants)
    parser.add_argument("--targets", default="all")
    parser.add_argument("--n-positive", type=int, default=1000)
    parser.add_argument("--n-negative", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stats-iterations", type=int, default=10000)
    parser.add_argument(
        "--target-set",
        choices=TARGET_SETS,
        default="ablation",
        help=(
            "'ablation': functions with source-defined regexes, disjoint from the main "
            "benchmark (ablation/targets.py); 'benchmark': the main benchmark's targets."
        ),
    )
    parser.add_argument(
        "--regexes",
        choices=REGEX_SETS,
        default="validator",
        help=(
            "Only for --target-set benchmark. 'validator' follows what each validator "
            "accepts (ablation/validator_regexes.py); 'benchmark' uses the main "
            "benchmark's regexes."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent / "outputs" / study),
    )
    return parser.parse_args()


def main(study="boundary_positive", default_variants="full,no_pos,random_pos", negative_tags=False):
    """Run a full-versus-variants ablation. Other studies reuse this with their own
    name and default variants (see ablation/local_complement.py)."""
    args = _arguments(study, default_variants)
    variants = [value.strip() for value in args.variants.split(",") if value.strip()]
    unknown_variants = sorted(set(variants) - set(VARIANTS))
    if unknown_variants:
        raise SystemExit(f"Unknown variant(s): {', '.join(unknown_variants)}")
    if args.runs < 1 or args.workers < 1 or args.stats_iterations < 1:
        raise SystemExit("--runs, --workers, and --stats-iterations must be at least 1")
    if args.n_positive < 0 or args.n_negative < 0:
        raise SystemExit("--n-positive and --n-negative must be non-negative")

    from benchmarks.python_regex_function_coverage_benchmark import SEED

    # The ablation target set uses each function's own regex; regex sets apply only
    # to the main benchmark's targets.
    regex_set = args.regexes if args.target_set == "benchmark" else "benchmark"
    available_targets = set(target_specs(args.target_set))
    if args.targets.strip().lower() == "all":
        targets = sorted(available_targets)
    else:
        targets = [value.strip() for value in args.targets.split(",") if value.strip()]
        unknown_targets = sorted(set(targets) - available_targets)
        if unknown_targets:
            raise SystemExit(f"Unknown target(s): {', '.join(unknown_targets)}")
    if not variants or not targets:
        raise SystemExit("At least one variant and one target are required")

    seed = SEED if args.seed is None else args.seed
    jobs = [
        {
            "target": target,
            "variant": variant,
            "run_index": run_index,
            "seed": seed + run_index,
            "n_positive": args.n_positive,
            "n_negative": args.n_negative,
            "out_dir": args.out_dir,
            "regex_set": regex_set,
            "target_set": args.target_set,
            "include_negative_tags": negative_tags,
        }
        for run_index in range(args.runs)
        for target in targets
        for variant in variants
    ]
    rows = run_all(jobs, args.workers)
    summary = summarize(
        rows,
        targets,
        variants,
        stats_iterations=args.stats_iterations,
        seed=seed,
        baseline=variants[0],  # deltas are first variant minus each other variant
    )
    config = {
        "study": study,
        "runs": args.runs,
        "seed": seed,
        "variants": variants,
        "targets": targets,
        "n_positive": args.n_positive,
        "n_negative": args.n_negative,
        "workers": args.workers,
        "stats_iterations": args.stats_iterations,
        "regex_set": regex_set,
        "target_set": args.target_set,
    }
    write_outputs(rows, summary, config, args.out_dir)
    print_delta_report(summary)


if __name__ == "__main__":
    main()
