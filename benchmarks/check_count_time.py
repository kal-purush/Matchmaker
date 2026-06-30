import argparse
import time
from collections import Counter
from pathlib import Path

from regex_positive_generator import RegexGraphBuilder
from regex_positive_generator.benchmarks.check_graph_build_time import (
    DEFAULT_GRAPH_OUTPUT_DIR,
    DEFAULT_PATHS,
    extract_patterns,
    safe_stem,
)
from regex_positive_generator.visualization import write_png


def measure_path(path, limit, *, seed=0, png_output_dir=None, png_limit=0):
    patterns = extract_patterns(path, limit)
    build_elapsed_total = 0.0
    main_count_elapsed_total = 0.0
    simple_count_elapsed_total = 0.0
    simple_graph_elapsed_total = 0.0

    ok = 0
    errors = Counter()
    first_error = ""
    inexact_main = 0
    inexact_simple = 0
    rendered_pngs = 0
    png_errors = Counter()
    first_png_error = ""
    output_dir = Path(png_output_dir) / f"{safe_stem(path)}_simplified" if png_output_dir else None

    for index, pattern in enumerate(patterns):
        try:
            start = time.perf_counter()
            builder = RegexGraphBuilder(pattern, validate=False)
            build_elapsed = time.perf_counter() - start

            start = time.perf_counter()
            main_count = builder.count_main_space()
            main_count_elapsed = time.perf_counter() - start

            start = time.perf_counter()
            simple_count = builder.count_simplified_space(seed=seed)
            simple_count_elapsed = time.perf_counter() - start

            start = time.perf_counter()
            simplified = builder.build_simplified_graph(seed=seed)
            simple_graph_elapsed = time.perf_counter() - start

            ok += 1
            build_elapsed_total += build_elapsed
            main_count_elapsed_total += main_count_elapsed
            simple_count_elapsed_total += simple_count_elapsed
            simple_graph_elapsed_total += simple_graph_elapsed
            inexact_main += 0 if main_count.exact else 1
            inexact_simple += 0 if simple_count.exact else 1

            if output_dir is not None and (png_limit <= 0 or rendered_pngs < png_limit):
                try:
                    write_png(simplified, output_dir / f"simplified_{index:05d}.png")
                    rendered_pngs += 1
                except Exception as exc:
                    label = type(exc).__name__
                    png_errors[label] += 1
                    if not first_png_error:
                        first_png_error = f"{label}: {str(exc)[:160]}"

        except Exception as exc:
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
        "build_elapsed_seconds": build_elapsed_total,
        "main_count_elapsed_seconds": main_count_elapsed_total,
        "simple_count_elapsed_seconds": simple_count_elapsed_total,
        "simple_graph_elapsed_seconds": simple_graph_elapsed_total,
        "avg_build_ms": (build_elapsed_total / attempted * 1000) if attempted else 0.0,
        "avg_main_count_ms": (main_count_elapsed_total / attempted * 1000) if attempted else 0.0,
        "avg_simple_count_ms": (simple_count_elapsed_total / attempted * 1000) if attempted else 0.0,
        "avg_simple_graph_ms": (simple_graph_elapsed_total / attempted * 1000) if attempted else 0.0,
        "inexact_main": inexact_main,
        "inexact_simple": inexact_simple,
        "errors": dict(errors.most_common()),
        "first_error": first_error,
        "rendered_pngs": rendered_pngs,
        "png_errors": dict(png_errors.most_common()),
        "first_png_error": first_png_error,
        "png_output_dir": str(output_dir) if output_dir else "",
    }


def main():
    parser = argparse.ArgumentParser(description="Measure regex_positive_generator count and simplified graph time.")
    parser.add_argument("paths", nargs="*", default=DEFAULT_PATHS)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--png", action="store_true", help="Render successful simplified graph builds as PNG files.")
    parser.add_argument("--png-limit", type=int, default=25, help="Maximum PNGs to render per input file. Use 0 for all.")
    parser.add_argument("--png-output-dir", default=str(DEFAULT_GRAPH_OUTPUT_DIR))
    args = parser.parse_args()

    for path in args.paths:
        result = measure_path(
            path,
            args.limit,
            seed=args.seed,
            png_output_dir=args.png_output_dir if args.png else None,
            png_limit=args.png_limit,
        )
        print(
            "{path}: attempted={attempted} ok={ok} failed={failed} "
            "build_s={build_elapsed_seconds:.3f} main_count_s={main_count_elapsed_seconds:.3f} "
            "simple_count_s={simple_count_elapsed_seconds:.3f} simple_graph_s={simple_graph_elapsed_seconds:.3f} "
            "avg_build_ms={avg_build_ms:.3f} avg_main_count_ms={avg_main_count_ms:.3f} "
            "avg_simple_count_ms={avg_simple_count_ms:.3f} avg_simple_graph_ms={avg_simple_graph_ms:.3f} "
            "inexact_main={inexact_main} inexact_simple={inexact_simple} errors={errors} first_error={first_error} "
            "rendered_pngs={rendered_pngs} png_errors={png_errors} "
            "first_png_error={first_png_error} png_output_dir={png_output_dir}".format(**result)
        )


if __name__ == "__main__":
    main()
