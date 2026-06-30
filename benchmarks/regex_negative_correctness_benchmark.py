import argparse
import multiprocessing
import queue
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from regex_positive_generator import generate_negative_samples
from regex_positive_generator.benchmarks.regex_correctness_benchmark import (
    BenchmarkTimeoutError,
    BenchmarkWorkerError,
    DEFAULT_CORPUS_PATHS,
    DEFAULT_TIMEOUT_SECONDS,
    OUTPUT_DIR,
    PER_SAMPLE_VALIDATION_TIMEOUT,
    load_patterns,
    validate_sample,
    write_jsonl_row,
)

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional progress dependency
    tqdm = None


EXAMPLE_LIMIT = 120
TIMEOUT_PARTIAL_VALIDATION_LIMIT = 25
WORKER_RESULT_GRACE_SECONDS = 1.0


def summarize_example(sample):
    if len(sample) <= EXAMPLE_LIMIT:
        return sample
    return {
        "length": len(sample),
        "prefix": sample[:EXAMPLE_LIMIT],
        "suffix": sample[-20:],
    }


def summarize_negative_samples(pattern, samples, *, timeout=None):
    matched = 0
    matched_examples = []
    validation_errors = []
    for sample in samples:
        try:
            if validate_sample(pattern, sample, timeout=timeout):
                matched += 1
                if len(matched_examples) < 5:
                    matched_examples.append(summarize_example(sample))
        except Exception as exc:
            matched += 1
            if len(validation_errors) < 5:
                validation_errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")
                matched_examples.append(summarize_example(sample))

    return {
        "generated": len(samples),
        "unique": len(set(samples)),
        "rejected": len(samples) - matched,
        "matched": matched,
        "matched_examples": matched_examples[:5],
        "validation_errors": validation_errors,
    }


def summarize_timeout_partial_samples(pattern, samples):
    preview_samples = samples[:TIMEOUT_PARTIAL_VALIDATION_LIMIT]
    result = summarize_negative_samples(pattern, preview_samples, timeout=PER_SAMPLE_VALIDATION_TIMEOUT)
    result.update({
        "generated": len(samples),
        "unique": len(set(samples)),
        "partial_validation_limit": TIMEOUT_PARTIAL_VALIDATION_LIMIT,
        "partial_validated": len(preview_samples),
        "partial_unvalidated": max(0, len(samples) - len(preview_samples)),
    })
    return result


def benchmark_pattern(pattern, *, n, seed, validate_generator, on_sample=None):
    samples = generate_negative_samples(
        pattern,
        n=n,
        seed=seed,
        validate=validate_generator,
        on_sample=on_sample,
    )
    return summarize_negative_samples(pattern, samples)


def benchmark_pattern_worker(output_queue, pattern, n, seed, validate_generator):
    partial_samples = []

    try:
        output_queue.put({"status": "started"})

        def send_partial(sample):
            partial_samples.append(sample)
            output_queue.put({"status": "partial_sample", "sample": sample})

        result = benchmark_pattern(
            pattern,
            n=n,
            seed=seed,
            validate_generator=validate_generator,
            on_sample=send_partial,
        )
        output_queue.put({"status": "ok", "result": result})
    except TimeoutError as exc:
        if partial_samples:
            result = summarize_timeout_partial_samples(pattern, partial_samples)
            result.update({
                "partial": True,
                "partial_examples": [summarize_example(sample) for sample in partial_samples[:5]],
            })
            output_queue.put({
                "status": "partial",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "result": result,
            })
            return
        output_queue.put({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
    except Exception as exc:
        output_queue.put({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })


def drain_worker_messages(output_queue, partial_samples):
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
            continue
        elif status in {"ok", "error", "partial"}:
            final_payload = payload
        else:
            final_payload = {
                "status": "error",
                "error_type": "RuntimeError",
                "error": f"Unknown worker message: {status}",
            }
    return final_payload


def benchmark_pattern_with_timeout(pattern, *, n, seed, validate_generator, timeout_seconds):
    output_queue = multiprocessing.Queue()
    partial_samples = []
    final_payload = None
    process = multiprocessing.Process(
        target=benchmark_pattern_worker,
        args=(output_queue, pattern, n, seed, validate_generator),
    )
    process.start()
    deadline = time.monotonic() + timeout_seconds
    while process.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        process.join(min(0.05, remaining))
        payload = drain_worker_messages(output_queue, partial_samples)
        if payload is not None:
            final_payload = payload

    payload = drain_worker_messages(output_queue, partial_samples)
    if payload is not None:
        final_payload = payload

    if process.is_alive():
        process.join(WORKER_RESULT_GRACE_SECONDS)
        payload = drain_worker_messages(output_queue, partial_samples)
        if payload is not None:
            final_payload = payload

    if process.is_alive():
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join()
        drain_worker_messages(output_queue, partial_samples)
        result = summarize_timeout_partial_samples(pattern, partial_samples)
        result.update({
            "partial": True,
            "partial_examples": [summarize_example(sample) for sample in partial_samples[:5]],
        })
        raise BenchmarkTimeoutError(f"Pattern timed out after {timeout_seconds:.1f}s", result)

    if final_payload is None:
        if process.exitcode == 0:
            raise RuntimeError("Worker exited without returning a result")
        raise RuntimeError(f"Worker exited with code {process.exitcode}")

    if final_payload["status"] == "ok":
        return final_payload["result"]
    if final_payload["status"] == "partial":
        raise BenchmarkTimeoutError(final_payload.get("error", "Pattern timed out"), final_payload["result"])
    raise BenchmarkWorkerError(final_payload["error_type"], final_payload["error"])


def run(paths, *, limit, n, seed, validate_generator, jsonl_path, timeout_seconds):
    patterns = load_patterns(paths, limit)
    attempted = len(patterns)
    ok = 0
    partial = 0
    ignored = 0
    generated = 0
    rejected = 0
    matched = 0
    errors = Counter()
    first_error = ""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_handle = open(jsonl_path, "w", encoding="utf-8") if jsonl_path else None
    try:
        iterator = enumerate(patterns)
        if tqdm is not None:
            iterator = tqdm(iterator, total=attempted, desc="Benchmarking negative patterns", unit="pattern")

        for index, (source_path, pattern) in iterator:
            row = {
                "index": index,
                "source_path": source_path,
                "pattern": pattern,
                "requested": n,
                "timeout_seconds": timeout_seconds,
            }

            try:
                result = benchmark_pattern_with_timeout(
                    pattern,
                    n=n,
                    seed=seed + index,
                    validate_generator=validate_generator,
                    timeout_seconds=timeout_seconds,
                )
                generated += result["generated"]
                rejected += result["rejected"]
                matched += result["matched"]
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
                generated += exc.result["generated"]
                rejected += exc.result["rejected"]
                matched += exc.result["matched"]
                status = "partial" if exc.result.get("generated", 0) > 0 else "timeout"
                if status == "partial":
                    partial += 1
                else:
                    errors[exc.error_type] += 1
                    if not first_error:
                        first_error = f"{exc.error_type}: {str(exc)[:160]}"
                row.update({
                    "status": status,
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
        "partial": partial,
        "ignored": ignored,
        "failed": attempted - ok - partial - ignored,
        "requested_per_pattern": n,
        "generated": generated,
        "rejected": rejected,
        "matched": matched,
        "errors": dict(errors.most_common()),
        "first_error": first_error,
        "jsonl_path": str(jsonl_path) if jsonl_path else "",
    }


def main():
    parser = argparse.ArgumentParser(description="Validate generated negative samples against their regexes.")
    parser.add_argument("paths", nargs="*", default=DEFAULT_CORPUS_PATHS)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--validate-generator",
        dest="validate_generator",
        action="store_true",
        default=False,
        help="Validate generated negatives before reporting them; disabled by default to measure raw candidate correctness.",
    )
    parser.add_argument(
        "--no-validate-generator",
        dest="validate_generator",
        action="store_false",
        help="Report raw heuristic candidates, including candidates that may still match; this is the default.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--jsonl", default=str(OUTPUT_DIR / "regex_negative_correctness_benchmark_results.jsonl"))
    args = parser.parse_args()

    result = run(
        args.paths,
        limit=args.limit,
        n=args.n,
        seed=args.seed,
        validate_generator=args.validate_generator,
        jsonl_path=Path(args.jsonl) if args.jsonl else None,
        timeout_seconds=args.timeout,
    )
    print(
        "attempted={attempted} ok={ok} partial={partial} ignored={ignored} failed={failed} requested={requested_per_pattern} "
        "generated={generated} rejected={rejected} matched={matched} errors={errors} "
        "first_error={first_error} jsonl_path={jsonl_path}".format(**result)
    )


if __name__ == "__main__":
    main()
