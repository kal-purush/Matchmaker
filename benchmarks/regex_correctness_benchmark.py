import argparse
import json
import multiprocessing
import os
import queue
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from regex_positive_generator.experimental import CompactGraphSampler, CompactRegexGraphBuilder

try:
    import regex
except ImportError:  # pragma: no cover - optional validation engine
    regex = None

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional progress dependency
    tqdm = None


# DEFAULT_PATHS = ["exponential-regexes.json", "polynomial-regexes.json"]
DEFAULT_CORPUS_PATHS = [
    # "internetSources-regExLib.json",
    # "internetSources-stackoverflow.json",
    str(REPO_ROOT / "regex_correctness_benchmark_selected_patterns.jsonl"),
]
OUTPUT_DIR = Path(__file__).resolve().parent / "correctness_outputs"
DEFAULT_TIMEOUT_SECONDS = 10.0
# Re-validating partial samples after a worker timeout happens in the main process,
# outside the per-pattern subprocess guard, so it needs its own bound: a pathological
# pattern can blow up again on a sample drawn from its own generator.
PER_SAMPLE_VALIDATION_TIMEOUT = 2.0
SAMPLER_BASELINE = "baseline"
SAMPLER_LOOKAROUND_EXPERIMENTAL = "lookaround-experimental"
SAMPLER_CHOICES = (SAMPLER_BASELINE, SAMPLER_LOOKAROUND_EXPERIMENTAL)


class BenchmarkWorkerError(Exception):
    def __init__(self, error_type, message):
        super().__init__(message)
        self.error_type = error_type


class BenchmarkTimeoutError(Exception):
    error_type = "TimeoutError"

    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def iter_patterns_from_json(path):
    path = Path(path)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    stripped = text.lstrip()
    if not stripped:
        return

    if path.suffix == ".jsonl":
        yield from iter_patterns_from_jsonl_text(text)
        return

    if stripped[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            yield from iter_patterns_from_jsonl_text(text)
            return
        values = list(data.values()) if isinstance(data, dict) else data
        for item in values:
            pattern = extract_pattern(item)
            if pattern:
                yield pattern
        return

    yield from iter_patterns_from_jsonl_text(text)


def iter_patterns_from_jsonl_text(text):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("patterns"), list):
            for pattern in item["patterns"]:
                if isinstance(pattern, str) and pattern:
                    yield pattern
        else:
            pattern = extract_pattern(item)
            if pattern:
                yield pattern


def extract_pattern(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("pattern", "regex", "Regex", "regex_pattern"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def find_unescaped_anchor_positions(pattern, anchor):
    positions = []
    escaped = False
    in_class = False
    for index, char in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "[":
            in_class = True
            continue
        if char == "]" and in_class:
            in_class = False
            continue
        if not in_class and char == anchor:
            positions.append(index)
    return positions


def benchmark_ignore_reason(pattern):
    start_positions = find_unescaped_anchor_positions(pattern, "^")
    if start_positions and min(start_positions) > 0:
        return "text_before_start_anchor"

    end_positions = find_unescaped_anchor_positions(pattern, "$")
    if end_positions and max(end_positions) < len(pattern) - 1:
        return "text_after_end_anchor"

    return ""



def load_patterns(corpus_paths, limit):
    records = []
    seen = set()

    for corpus_path in corpus_paths:
        abs_path = os.path.abspath(corpus_path)
        print(f"Loading patterns from {abs_path}...")
        for pattern in iter_patterns_from_json(abs_path):
            if not isinstance(pattern, str) or not pattern or pattern in seen:
                continue
            seen.add(pattern)
            records.append((corpus_path, pattern))
            if limit and len(records) >= limit:
                return records

    return records


# def load_patterns(paths, limit):
#     patterns = []
#     seen = set()
#     for path in paths:
#         for pattern in iter_patterns_from_json(path):
#             if pattern in seen:
#                 continue
#             seen.add(pattern)
#             patterns.append((path, pattern))
#             if limit and len(patterns) >= limit:
#                 return patterns
#     return patterns


def validate_sample(pattern, sample, flags=0, timeout=None):
    engine = regex if regex is not None else re
    kwargs = {"timeout": timeout} if engine is regex and timeout is not None else {}
    return engine.fullmatch(pattern, sample, flags, **kwargs) is not None


def write_jsonl_row(handle, row):
    handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True))
    handle.write("\n")
    handle.flush()


def summarize_samples(pattern, samples, *, nodes=None, timeout=None):
    valid = 0
    invalid_samples = []
    for sample in samples:
        try:
            if validate_sample(pattern, sample, timeout=timeout):
                valid += 1
            else:
                invalid_samples.append(sample)
        except Exception as exc:
            invalid_samples.append(sample)
            if len(invalid_samples) == 1:
                invalid_samples.append(f"validation_error:{type(exc).__name__}:{str(exc)[:120]}")
    result = {
        "generated": len(samples),
        "unique": len(set(samples)),
        "valid": valid,
        "invalid": len(samples) - valid,
        "invalid_examples": invalid_samples[:5],
    }
    if nodes is not None:
        result["nodes"] = nodes
    return result


def build_sampler(builder, validate_generator, sampler):
    if sampler == SAMPLER_BASELINE:
        return CompactGraphSampler(builder, validate=validate_generator)
    if sampler == SAMPLER_LOOKAROUND_EXPERIMENTAL:
        from regex_positive_generator.experimental.compact_sampler_lookaround_experimental import (
            ExperimentalLookaroundCompactGraphSampler,
        )

        return ExperimentalLookaroundCompactGraphSampler(builder, validate=validate_generator)
    raise ValueError(f"Unknown sampler: {sampler}")


def benchmark_pattern(pattern, *, n, seed, validate_generator, sampler, on_sample=None):
    builder = CompactRegexGraphBuilder(pattern, validate=validate_generator)
    generator = build_sampler(builder, validate_generator, sampler)
    samples = generator.generate_samples(
        n=n,
        seed=seed,
        on_sample=on_sample,
    )
    result = summarize_samples(pattern, samples, nodes=len(builder.nodes))
    if hasattr(generator, "lookaround_report"):
        result["lookaround_report"] = generator.lookaround_report()
    return result


def benchmark_pattern_worker(output_queue, pattern, n, seed, validate_generator, sampler):
    try:
        builder = CompactRegexGraphBuilder(pattern, validate=validate_generator)
        output_queue.put({"status": "started", "nodes": len(builder.nodes)})

        def send_partial(sample):
            output_queue.put({"status": "partial_sample", "sample": sample})

        generator = build_sampler(builder, validate_generator, sampler)
        samples = generator.generate_samples(
            n=n,
            seed=seed,
            on_sample=send_partial,
        )
        result = summarize_samples(pattern, samples, nodes=len(builder.nodes))
        if hasattr(generator, "lookaround_report"):
            result["lookaround_report"] = generator.lookaround_report()
        output_queue.put({"status": "ok", "result": result})
    except Exception as exc:
        output_queue.put({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })


def drain_worker_messages(output_queue, partial_samples, state):
    final_payload = None
    while True:
        try:
            payload = output_queue.get_nowait()
        except queue.Empty:
            break

        status = payload.get("status")
        if status == "partial_sample":
            partial_samples.append(payload["sample"])
        elif status == "started":
            state["nodes"] = payload.get("nodes")
        elif status in {"ok", "error"}:
            final_payload = payload
        else:
            final_payload = {
                "status": "error",
                "error_type": "RuntimeError",
                "error": f"Unknown worker message: {status}",
            }
    return final_payload


def benchmark_pattern_with_timeout(pattern, *, n, seed, validate_generator, sampler, timeout_seconds):
    output_queue = multiprocessing.Queue()
    partial_samples = []
    state = {}
    final_payload = None
    process = multiprocessing.Process(
        target=benchmark_pattern_worker,
        args=(output_queue, pattern, n, seed, validate_generator, sampler),
    )
    process.start()
    deadline = time.monotonic() + timeout_seconds
    while process.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        process.join(min(0.05, remaining))
        payload = drain_worker_messages(output_queue, partial_samples, state)
        if payload is not None:
            final_payload = payload

    payload = drain_worker_messages(output_queue, partial_samples, state)
    if payload is not None:
        final_payload = payload

    if process.is_alive():
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join()
        drain_worker_messages(output_queue, partial_samples, state)
        result = summarize_samples(pattern, partial_samples, nodes=state.get("nodes"), timeout=PER_SAMPLE_VALIDATION_TIMEOUT)
        result.update({
            "partial": True,
            "partial_examples": partial_samples[:5],
        })
        raise BenchmarkTimeoutError(f"Pattern timed out after {timeout_seconds:.1f}s", result)

    if final_payload is None:
        if process.exitcode == 0:
            raise RuntimeError("Worker exited without returning a result")
        raise RuntimeError(f"Worker exited with code {process.exitcode}")

    if final_payload["status"] == "ok":
        return final_payload["result"]
    raise BenchmarkWorkerError(final_payload["error_type"], final_payload["error"])


def run(paths, *, limit, n, seed, validate_generator, sampler, jsonl_path, timeout_seconds):
    patterns = load_patterns(paths, limit)
    # print(f"Loaded {len(patterns)} unique patterns from {len(paths)} files.")
    # print(patterns[0] if patterns else "No patterns loaded.")
    attempted = len(patterns)
    ok = 0
    ignored = 0
    generated = 0
    valid = 0
    invalid = 0
    errors = Counter()
    first_error = ""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_handle = open(jsonl_path, "w", encoding="utf-8") if jsonl_path else None
    try:
        iterator = enumerate(patterns)
        if tqdm is not None:
            iterator = tqdm(iterator, total=attempted, desc="Benchmarking patterns", unit="pattern")

        for index, (source_path, pattern) in iterator:
            row = {
                "index": index,
                "source_path": source_path,
                "pattern": pattern,
                "requested": n,
                "timeout_seconds": timeout_seconds,
            }
            # ignore_reason = benchmark_ignore_reason(pattern)
            # if ignore_reason:
            #     ignored += 1
            #     row.update({"status": "ignored", "ignore_reason": ignore_reason})
            #     if jsonl_handle:
            #         write_jsonl_row(jsonl_handle, row)
            #     continue

            try:
                result = benchmark_pattern_with_timeout(
                    pattern,
                    n=n,
                    seed=seed + index,
                    validate_generator=validate_generator,
                    sampler=sampler,
                    timeout_seconds=timeout_seconds,
                )
                generated += result["generated"]
                valid += result["valid"]
                invalid += result["invalid"]
                if result["generated"] == 0:
                    label = "NoSamplesGenerated"
                    errors[label] += 1
                    if not first_error:
                        first_error = f"{label}: generator returned zero samples"
                    row.update({
                        "status": "no_samples",
                        "error_type": label,
                        "error": "Generator returned zero samples.",
                        **result,
                    })
                else:
                    ok += 1
                    row.update({"status": "ok", **result})
            except BenchmarkTimeoutError as exc:
                errors[exc.error_type] += 1
                if not first_error:
                    first_error = f"{exc.error_type}: {str(exc)[:160]}"
                generated += exc.result["generated"]
                valid += exc.result["valid"]
                invalid += exc.result["invalid"]
                row.update({
                    "status": "timeout",
                    "error_type": exc.error_type,
                    "error": str(exc)[:500],
                    **exc.result,
                })
            except Exception as exc:
                label = getattr(exc, "error_type", type(exc).__name__)
                errors[label] += 1
                if not first_error:
                    first_error = f"{label}: {str(exc)[:160]}"
                row.update({"status": "error", "error_type": label, "error": str(exc)[:500]})

            if jsonl_handle:
                write_jsonl_row(jsonl_handle, row)
    finally:
        if jsonl_handle:
            jsonl_handle.close()

    return {
        "attempted": attempted,
        "ok": ok,
        "ignored": ignored,
        "failed": attempted - ok - ignored,
        "requested_per_pattern": n,
        "sampler": sampler,
        "generated": generated,
        "valid": valid,
        "invalid": invalid,
        "errors": dict(errors.most_common()),
        "first_error": first_error,
        "jsonl_path": str(jsonl_path) if jsonl_path else "",
    }


def main():
    parser = argparse.ArgumentParser(description="Validate compact experimental positive samples against their regexes.")
    parser.add_argument("paths", nargs="*", default=DEFAULT_CORPUS_PATHS)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--validate-generator", action="store_true")
    parser.add_argument(
        "--sampler",
        choices=SAMPLER_CHOICES,
        default=SAMPLER_BASELINE,
        help="Positive sampler implementation to benchmark. Defaults to the current production sampler.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--jsonl", default=str(OUTPUT_DIR / "regex_correctness_benchmark_results.jsonl"))
    args = parser.parse_args()

    result = run(
        args.paths,
        limit=args.limit,
        n=args.n,
        seed=args.seed,
        validate_generator=args.validate_generator,
        sampler=args.sampler,
        jsonl_path=Path(args.jsonl) if args.jsonl else None,
        timeout_seconds=args.timeout,
    )
    print(
        "attempted={attempted} ok={ok} ignored={ignored} failed={failed} requested={requested_per_pattern} "
        "sampler={sampler} "
        "generated={generated} valid={valid} invalid={invalid} errors={errors} "
        "first_error={first_error} jsonl_path={jsonl_path}".format(**result)
    )


if __name__ == "__main__":
    main()
