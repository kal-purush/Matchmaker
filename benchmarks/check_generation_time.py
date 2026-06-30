import argparse
import time
from collections import Counter

from regex_positive_generator import RegexGraphBuilder
from regex_positive_generator.benchmarks.check_graph_build_time import DEFAULT_PATHS, extract_patterns
from regex_positive_generator.sampler import GraphSampler


def measure_path(path, limit, *, n, seed=0, validate=False):
    patterns = extract_patterns(path, limit)
    build_elapsed_total = 0.0
    count_elapsed_total = 0.0
    generation_elapsed_total = 0.0
    generated_total = 0
    ok = 0
    errors = Counter()
    first_error = ""

    for pattern in patterns:
        try:
            start = time.perf_counter()
            builder = RegexGraphBuilder(pattern, validate=validate)
            build_elapsed = time.perf_counter() - start

            start = time.perf_counter()
            main_count = builder.count_main_space()
            simple_count = builder.count_simplified_space(seed=seed)
            count_elapsed = time.perf_counter() - start

            start = time.perf_counter()
            samples = GraphSampler(builder, validate=validate).generate_samples(n=n, seed=seed)
            generation_elapsed = time.perf_counter() - start

            ok += 1
            generated_total += len(samples)
            build_elapsed_total += build_elapsed
            count_elapsed_total += count_elapsed
            generation_elapsed_total += generation_elapsed
            _ = (main_count, simple_count)
            print(f"Processed pattern in {build_elapsed:.3f} build s, {count_elapsed:.3f} count s, "
                  f"{generation_elapsed:.3f} generation s")
            if generation_elapsed > 10.0:
                print(f"  Main count: {main_count}")
                print(f"  Simplified count: {simple_count}")
                print(f"  Generated {len(samples)} samples")
                print(f"  Pattern: {pattern}")
                print(f"  Sample generated: {samples[0] if samples else '(none)'}")
        except Exception as exc:
            print(f"Error processing pattern: {pattern}\nError: {exc}")
            label = type(exc).__name__
            errors[label] += 1
            if not first_error:
                first_error = f"{label}: {str(exc)[:160]}"

    attempted = len(patterns)
    return {
        "path": path,
        "attempted": attempted,
        "ok": ok,
        "failed": attempted - ok,
        "requested_per_pattern": n,
        "generated_total": generated_total,
        "build_seconds": build_elapsed_total,
        "count_seconds": count_elapsed_total,
        "generation_seconds": generation_elapsed_total,
        "avg_build_ms": (build_elapsed_total / attempted * 1000) if attempted else 0.0,
        "avg_count_ms": (count_elapsed_total / attempted * 1000) if attempted else 0.0,
        "avg_generation_ms": (generation_elapsed_total / attempted * 1000) if attempted else 0.0,
        "errors": dict(errors.most_common()),
        "first_error": first_error,
    }


def main():
    parser = argparse.ArgumentParser(description="Measure regex_positive_generator generation time.")
    parser.add_argument("paths", nargs="*", default=DEFAULT_PATHS)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    for path in args.paths:
        result = measure_path(path, args.limit, n=args.n, seed=args.seed, validate=args.validate)
        print(
            "{path}: attempted={attempted} ok={ok} failed={failed} requested={requested_per_pattern} "
            "generated_total={generated_total} build_s={build_seconds:.3f} count_s={count_seconds:.3f} "
            "generation_s={generation_seconds:.3f} avg_build_ms={avg_build_ms:.3f} "
            "avg_count_ms={avg_count_ms:.3f} avg_generation_ms={avg_generation_ms:.3f} "
            "errors={errors} first_error={first_error}".format(**result)
        )


if __name__ == "__main__":
    main()
