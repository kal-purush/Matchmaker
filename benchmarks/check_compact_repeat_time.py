import argparse
import time
from collections import Counter

from regex_positive_generator import RegexGraphBuilder
from regex_positive_generator.benchmarks.check_graph_build_time import DEFAULT_PATHS, extract_patterns
from regex_positive_generator.experimental import CompactGraphSampler, CompactRegexGraphBuilder
from regex_positive_generator.sampler import GraphSampler


def empty_stats():
    return {
        "build_seconds": 0.0,
        "count_seconds": 0.0,
        "generation_seconds": 0.0,
        "ok": 0,
        "failed": 0,
        "generated_total": 0,
        "total_nodes": 0,
        "errors": Counter(),
        "first_error": "",
    }


def record_error(stats, exc):
    label = type(exc).__name__
    stats["failed"] += 1
    stats["errors"][label] += 1
    if not stats["first_error"]:
        stats["first_error"] = f"{label}: {str(exc)[:160]}"


def measure_current(pattern, *, n, seed, validate):
    try:
        start = time.perf_counter()
        builder = RegexGraphBuilder(pattern, validate=validate)
        build_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        samples = GraphSampler(builder, validate=validate).generate_samples(n=n, seed=seed)
        generation_elapsed = time.perf_counter() - start
    except Exception as exc:
        print(f"Error processing pattern: {pattern}\nError: {exc}")
        raise

    return build_elapsed, 0.0, generation_elapsed, len(samples), len(builder.nodes)


def measure_compact(pattern, *, n, seed, validate):
    try:
        start = time.perf_counter()
        builder = CompactRegexGraphBuilder(pattern, validate=validate)
        build_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        _total = builder.count_paths()
        count_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        samples = CompactGraphSampler(builder, validate=validate).generate_samples(n=n, seed=seed)
        generation_elapsed = time.perf_counter() - start

        print(f"Processed pattern in {build_elapsed:.3f} build s, {count_elapsed:.3f} count s, "
                    f"{generation_elapsed:.3f} generation s")
        if generation_elapsed > 10.0:
            # print(f"  Main count: {main_count}")
            # print(f"  Simplified count: {simple_count}")
            print(f"  Generated {len(samples)} samples")
            print(f"  Pattern: {pattern}")
            print(f"  Sample generated: {samples[0] if samples else '(none)'}")
    except Exception as exc:
        print(f"Error processing pattern: {pattern}\nError: {exc}")
        raise

    return build_elapsed, count_elapsed, generation_elapsed, len(samples), len(builder.nodes)


def add_success(stats, build_elapsed, count_elapsed, generation_elapsed, generated, nodes):
    stats["ok"] += 1
    stats["build_seconds"] += build_elapsed
    stats["count_seconds"] += count_elapsed
    stats["generation_seconds"] += generation_elapsed
    stats["generated_total"] += generated
    stats["total_nodes"] += nodes


def summarize_stats(stats, attempted):
    ok = stats["ok"]
    return {
        "attempted": attempted,
        "ok": ok,
        "failed": stats["failed"],
        "generated_total": stats["generated_total"],
        "build_seconds": stats["build_seconds"],
        "count_seconds": stats["count_seconds"],
        "generation_seconds": stats["generation_seconds"],
        "avg_build_ms": (stats["build_seconds"] / attempted * 1000) if attempted else 0.0,
        "avg_count_ms": (stats["count_seconds"] / attempted * 1000) if attempted else 0.0,
        "avg_generation_ms": (stats["generation_seconds"] / attempted * 1000) if attempted else 0.0,
        "avg_nodes": (stats["total_nodes"] / ok) if ok else 0.0,
        "errors": dict(stats["errors"].most_common()),
        "first_error": stats["first_error"],
    }


def measure_path(path, limit, *, n, seed=0, validate=False):
    patterns = extract_patterns(path, limit)
    current = empty_stats()
    compact = empty_stats()

    for pattern in patterns:
        try:
           add_success(current, *measure_current(pattern, n=n, seed=seed, validate=validate))
        except Exception as exc:
            record_error(current, exc)

        try:
            add_success(compact, *measure_compact(pattern, n=n, seed=seed, validate=validate))
        except Exception as exc:
            record_error(compact, exc) 

    attempted = len(patterns)
    return {
        "path": path,
        "requested_per_pattern": n,
        "current": summarize_stats(current, attempted),
        "compact": summarize_stats(compact, attempted),
    }


def print_summary(label, stats):
    print(
        "{label}: attempted={attempted} ok={ok} failed={failed} generated_total={generated_total} "
        "build_s={build_seconds:.3f} count_s={count_seconds:.3f} generation_s={generation_seconds:.3f} "
        "avg_build_ms={avg_build_ms:.3f} avg_count_ms={avg_count_ms:.3f} "
        "avg_generation_ms={avg_generation_ms:.3f} avg_nodes={avg_nodes:.1f} "
        "errors={errors} first_error={first_error}".format(label=label, **stats)
    )


def main():
    parser = argparse.ArgumentParser(description="Compare current graph unrolling with compact repeat experiment.")
    parser.add_argument("paths", nargs="*", default=DEFAULT_PATHS)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    for path in args.paths:
        result = measure_path(path, args.limit, n=args.n, seed=args.seed, validate=args.validate)
        print(f"{path}: requested={result['requested_per_pattern']}")
        print_summary("current", result["current"])
        print_summary("compact", result["compact"])


if __name__ == "__main__":
    main()
