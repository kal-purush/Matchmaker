import argparse
import json
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from regex_positive_generator import RegexGraphBuilder
from regex_positive_generator.visualization import write_png


DEFAULT_PATHS = [
    # str(REPO_ROOT / "polynomial-regexes.json"),
    str(REPO_ROOT / "exponential-regexes.json"),
]
DEFAULT_GRAPH_OUTPUT_DIR = Path(__file__).resolve().parent / "graph_outputs"
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


def extract_patterns(path, limit):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    values = list(data.values()) if isinstance(data, dict) else data
    patterns = []
    for item in values:
        if isinstance(item, str):
            patterns.append(item)
        elif isinstance(item, dict):
            for key in ("pattern", "regex", "Regex", "regex_pattern"):
                value = item.get(key)
                if isinstance(value, str):
                    patterns.append(value)
                    break
        if limit and len(patterns) >= limit:
            break
    return patterns


def safe_stem(path):
    return Path(path).stem.replace(" ", "_")


def measure_path(path, limit, *, png_output_dir=None, png_limit=0):
    patterns = extract_patterns(path, limit)
    build_elapsed_total = 0.0
    ok = 0
    total_nodes = 0
    errors = Counter()
    first_error = ""
    rendered_pngs = 0
    png_errors = Counter()
    first_png_error = ""
    output_dir = Path(png_output_dir) / safe_stem(path) if png_output_dir else None

    for index, pattern in enumerate(patterns):
        try:
            pattern_start = time.perf_counter()
            builder = RegexGraphBuilder(pattern, validate=False)
            pattern_elapsed = time.perf_counter() - pattern_start
            ok += 1
            total_nodes += len(builder.nodes)
            if output_dir is not None and (png_limit <= 0 or rendered_pngs < png_limit):
                try:
                    write_png(builder, output_dir / f"graph_{index:05d}.png")
                    rendered_pngs += 1
                except Exception as exc:
                    label = type(exc).__name__
                    png_errors[label] += 1
                    if not first_png_error:
                        first_png_error = f"{label}: {str(exc)[:160]}"
        except Exception as exc:
            print(f"Error processing pattern: {pattern}\nError: {exc}")
            label = type(exc).__name__
            errors[label] += 1
            if not first_error:
                first_error = f"{label}: {str(exc)[:160]}"
            pattern_elapsed = time.perf_counter() - pattern_start

        build_elapsed_total += pattern_elapsed
        # print(f"Processed pattern in {pattern_elapsed:.3f} seconds: {pattern}")

    attempted = len(patterns)
    return {
        "path": path,
        "attempted": attempted,
        "ok": ok,
        "failed": attempted - ok,
        "elapsed_seconds": build_elapsed_total,
        "avg_ms_per_pattern": (build_elapsed_total / attempted * 1000) if attempted else 0.0,
        "avg_nodes": (total_nodes / ok) if ok else 0.0,
        "errors": dict(errors.most_common()),
        "first_error": first_error,
        "rendered_pngs": rendered_pngs,
        "png_errors": dict(png_errors.most_common()),
        "first_png_error": first_png_error,
        "png_output_dir": str(output_dir) if output_dir else "",
    }


def main():
    parser = argparse.ArgumentParser(description="Measure regex_positive_generator graph build time.")
    parser.add_argument("paths", nargs="*", default=DEFAULT_PATHS)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--png", action="store_true", help="Render successful graph builds as PNG files.")
    parser.add_argument("--png-limit", type=int, default=25, help="Maximum PNGs to render per input file. Use 0 for all.")
    parser.add_argument("--png-output-dir", default=str(DEFAULT_GRAPH_OUTPUT_DIR))
    args = parser.parse_args()

    for path in args.paths:
        result = measure_path(
            path,
            args.limit,
            png_output_dir=args.png_output_dir if args.png else None,
            png_limit=args.png_limit,
        )
        print(
            "{path}: attempted={attempted} ok={ok} failed={failed} "
            "elapsed_s={elapsed_seconds:.3f} avg_ms={avg_ms_per_pattern:.3f} "
            "avg_nodes={avg_nodes:.1f} errors={errors} first_error={first_error} "
            "rendered_pngs={rendered_pngs} png_errors={png_errors} "
            "first_png_error={first_png_error} png_output_dir={png_output_dir}".format(**result)
        )


if __name__ == "__main__":
    main()
