import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from regex_positive_generator.benchmarks.regex_correctness_benchmark import (
    DEFAULT_CORPUS_PATHS,
    load_patterns,
    validate_sample,
    write_jsonl_row,
)
from regex_positive_generator.experimental import CompactGraphSampler, CompactRegexGraphBuilder
from regex_positive_generator.experimental.compact_sampler_lookaround_experimental import (
    ExperimentalLookaroundCompactGraphSampler,
)


KNOWN_INDICES = {115, 510, 2590, 4494}


def feature_tags(pattern):
    tags = []
    if "(?=" in pattern:
        tags.append("positive_lookahead")
    if "(?!" in pattern:
        tags.append("negative_lookahead")
    if "(?<=" in pattern or "(?<!" in pattern:
        tags.append("lookbehind")
    if re.search(r"\\[1-9]", pattern):
        tags.append("backreference")
    return tags or ["other"]


def summarize_samples(pattern, samples):
    invalid = 0
    invalid_examples = []
    for sample in samples:
        try:
            if not validate_sample(pattern, sample):
                invalid += 1
                if len(invalid_examples) < 5:
                    invalid_examples.append(sample)
        except Exception as exc:
            invalid += 1
            if len(invalid_examples) < 5:
                invalid_examples.append(f"validation_error:{type(exc).__name__}:{str(exc)[:120]}")
    return {
        "generated": len(samples),
        "invalid": invalid,
        "valid": len(samples) - invalid,
        "invalid_examples": invalid_examples,
    }


def run_one_sampler(pattern, sampler_cls, *, n, seed):
    started = time.perf_counter()
    builder = CompactRegexGraphBuilder(pattern)
    sampler = sampler_cls(builder, validate=False)
    samples = sampler.generate_samples(n=n, seed=seed)
    elapsed = time.perf_counter() - started
    summary = summarize_samples(pattern, samples)
    summary["seconds"] = elapsed
    if hasattr(sampler, "lookaround_report"):
        summary["lookaround_report"] = sampler.lookaround_report()
    return summary


def build_error_summary(exc):
    return {
        "generated": 0,
        "valid": 0,
        "invalid": 0,
        "invalid_examples": [],
        "seconds": 0.0,
        "error_type": type(exc).__name__,
        "error": str(exc)[:500],
    }


def compare_pattern(index, source_path, pattern, *, n, seed):
    row = {
        "index": index,
        "source_path": source_path,
        "pattern": pattern,
        "features": feature_tags(pattern),
        "known_case": index in KNOWN_INDICES,
    }
    try:
        baseline = run_one_sampler(pattern, CompactGraphSampler, n=n, seed=seed)
    except Exception as exc:
        baseline = build_error_summary(exc)
    try:
        experimental = run_one_sampler(pattern, ExperimentalLookaroundCompactGraphSampler, n=n, seed=seed)
    except Exception as exc:
        experimental = build_error_summary(exc)

    row["baseline"] = baseline
    row["experimental"] = experimental
    row["delta"] = {
        "generated": experimental["generated"] - baseline["generated"],
        "invalid": experimental["invalid"] - baseline["invalid"],
        "seconds": experimental["seconds"] - baseline["seconds"],
    }
    return row


def print_summary(rows):
    totals = {
        "baseline_generated": sum(row["baseline"]["generated"] for row in rows),
        "experimental_generated": sum(row["experimental"]["generated"] for row in rows),
        "baseline_invalid": sum(row["baseline"]["invalid"] for row in rows),
        "experimental_invalid": sum(row["experimental"]["invalid"] for row in rows),
        "baseline_seconds": sum(row["baseline"]["seconds"] for row in rows),
        "experimental_seconds": sum(row["experimental"]["seconds"] for row in rows),
    }
    improved = sum(row["delta"]["invalid"] < 0 for row in rows)
    regressed = sum(row["delta"]["invalid"] > 0 for row in rows)
    unchanged = len(rows) - improved - regressed

    invalid_by_feature = Counter()
    invalid_delta_by_feature = Counter()
    for row in rows:
        for tag in row["features"]:
            invalid_by_feature[tag] += row["baseline"]["invalid"]
            invalid_delta_by_feature[tag] += row["delta"]["invalid"]

    print("Lookaround Sampler Experiment")
    print("=" * 80)
    print(f"patterns={len(rows)} improved={improved} unchanged={unchanged} regressed={regressed}")
    print(
        "generated baseline={baseline_generated} experimental={experimental_generated} delta={delta}".format(
            delta=totals["experimental_generated"] - totals["baseline_generated"],
            **totals,
        )
    )
    print(
        "invalid baseline={baseline_invalid} experimental={experimental_invalid} delta={delta}".format(
            delta=totals["experimental_invalid"] - totals["baseline_invalid"],
            **totals,
        )
    )
    print(
        "seconds baseline={baseline_seconds:.3f} experimental={experimental_seconds:.3f} delta={delta:.3f}".format(
            delta=totals["experimental_seconds"] - totals["baseline_seconds"],
            **totals,
        )
    )
    print("\nInvalid samples by feature in baseline:")
    for tag, count in invalid_by_feature.most_common():
        print(f"  {tag:<22} {count:>8}")
    print("\nInvalid-sample delta by feature (experimental - baseline):")
    for tag, count in invalid_delta_by_feature.most_common():
        print(f"  {tag:<22} {count:>8}")

    known_rows = [row for row in rows if row["known_case"]]
    if known_rows:
        print("\nKnown cases:")
        for row in known_rows:
            print(
                "  index={index:<5} baseline_invalid={b:<5} experimental_invalid={e:<5} "
                "baseline_generated={bg:<5} experimental_generated={eg:<5} features={features}".format(
                    index=row["index"],
                    b=row["baseline"]["invalid"],
                    e=row["experimental"]["invalid"],
                    bg=row["baseline"]["generated"],
                    eg=row["experimental"]["generated"],
                    features=",".join(row["features"]),
                )
            )


def main():
    parser = argparse.ArgumentParser(description="Compare baseline compact sampler with experimental lookaround sampler.")
    parser.add_argument("paths", nargs="*", default=DEFAULT_CORPUS_PATHS)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--jsonl", default="")
    parser.add_argument("--lookaround-only", action="store_true", default=False)
    args = parser.parse_args()

    records = list(enumerate(load_patterns(args.paths, args.limit)))
    if args.lookaround_only:
        records = [
            (index, record)
            for index, record in records
            if index in KNOWN_INDICES or any(tag != "other" for tag in feature_tags(record[1]))
        ]

    rows = []
    jsonl_handle = open(args.jsonl, "w", encoding="utf-8") if args.jsonl else None
    try:
        for index, (source_path, pattern) in records:
            row = compare_pattern(index, source_path, pattern, n=args.n, seed=args.seed + index)
            rows.append(row)
            if jsonl_handle:
                write_jsonl_row(jsonl_handle, row)
    finally:
        if jsonl_handle:
            jsonl_handle.close()

    print_summary(rows)

    if not args.jsonl:
        print("\nUse --jsonl PATH to save per-pattern comparison rows.")


if __name__ == "__main__":
    main()
